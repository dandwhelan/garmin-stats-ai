"""
trigger_sweep.py - Dynamic BIA impedance sweep trigger for Fitdays / Lefu scale (JEETIF2421).
Uses the cracked 6-bit checksum algorithm to dynamically generate valid timestamps,
user profile pushes (C0/C1), capability echo (B6), and control frames (B0).
"""

import asyncio
import time
import struct
from bleak import BleakClient, BleakScanner
from scale_codec import (
    compute_checksum,
    make_frame,
    parse_frame,
    build_c0_profile,
    build_c1_profile,
    build_b6_trigger,
    build_b0_cmd,
)

ADDR = "78:66:A5:72:C4:64"
FFB1 = "0000ffb1-0000-1000-8000-00805f9b34fb"  # write commands
FFB2 = "0000ffb2-0000-1000-8000-00805f9b34fb"  # weight notify
FFB3 = "0000ffb3-0000-1000-8000-00805f9b34fb"  # indicate (A0, A5, A7, AA)

# Known stale blocks to compare against:
STALE_BLOCKS = {
    "01260c050c550ac20b0300dd0a6b0ad709600998": "Old stale reading",
    "011b0bf40c520ab90af500da0a5e0ad10959098e": "App weigh-in stored reading",
}

class ScaleSession:
    def __init__(self, client: BleakClient, user_weight_kg: float = 72.0, height_cm: int = 185, name: str = "Dan"):
        self.client = client
        self.weight_kg = user_weight_kg
        self.height_cm = height_cm
        self.name = name
        self.current_live_weight = 0.0
        self.current_status = 0
        self.scale_token = bytes.fromhex("0000034000")
        self.fresh_a7_received = asyncio.Event()
        self.handshake_done = False
        self.seq_out = 0

    def next_seq(self) -> int:
        s = self.seq_out
        self.seq_out = (self.seq_out + 1) & 0xFF
        return s

    async def write_cmd(self, frame: bytes, desc: str = ""):
        ck = frame[-1]
        print(f"[{time.strftime('%H:%M:%S')}] TX FFB1 -> {desc} len={len(frame)}: {frame.hex()} (ck={ck:02x})")
        await self.client.write_gatt_char(FFB1, frame, response=True)
        await asyncio.sleep(0.12)

    def on_ffb2_notify(self, sender, data: bytearray):
        raw = bytes(data)
        try:
            parsed = parse_frame(raw)
            if parsed['type'] == 0xA2:
                self.current_live_weight = parsed.get('weight_kg', 0.0)
                self.current_status = parsed.get('status', 0)
                status_str = {1: "measuring", 3: "SETTLED", 0: "stepped-off"}.get(self.current_status, f"0x{self.current_status:02x}")
                if self.current_live_weight > 10.0:
                    print(f"[{time.strftime('%H:%M:%S')}] LIVE WEIGHT: {self.current_live_weight:.3f} kg ({status_str})")
        except Exception as e:
            print(f"FFB2 parse error: {e} raw={raw.hex()}")

    def on_ffb3_indicate(self, sender, data: bytearray):
        raw = bytes(data)
        try:
            parsed = parse_frame(raw)
            ftype = parsed['type']
            if ftype in (0xA5, 0xA7):
                blk = parsed['impedance_block_hex']
                ts = parsed['timestamp']
                w = parsed['weight_kg']
                ohms = parsed['impedance_ohms']
                # An all-zero block means the scale captured NO impedance this
                # measurement (poor contact, or the sweep did not complete). It
                # is not a fresh reading - the Robi S9 adapter in ble-scale-sync
                # hit exactly this and mistook it for data.
                all_zero = (set(blk) == {"0"})
                if all_zero:
                    label = "NO IMPEDANCE (all-zero: bad contact / sweep incomplete)"
                else:
                    label = STALE_BLOCKS.get(blk, "*** GENUINE NEW SWEEP! ***")
                is_fresh = (not all_zero) and (blk not in STALE_BLOCKS)
                print("=" * 70)
                print(f"[{time.strftime('%H:%M:%S')}] COMPOSITION FRAME (Type 0x{ftype:02X}):")
                print(f"   Timestamp: {ts} ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts))} UTC)")
                print(f"   Weight:    {w:.3f} kg")
                print(f"   Status:    {label}")
                print(f"   Block hex: {blk}")
                # Block layout is [freq1 x 5 segments][freq2 x 5 segments], NOT
                # interleaved: pairing ohms[i] with ohms[i+5] gives a consistent
                # 1.14-1.18 low/high-frequency ratio, whereas pairing adjacent
                # values gives absurd ratios (0.08, 13.72).
                seg = ["Trunk", "Arm", "Arm", "Leg", "Leg"]
                print("   Segmental Ohms (freq1 / freq2):")
                for i, nm in enumerate(seg):
                    r = f"{ohms[i]/ohms[i+5]:.2f}" if ohms[i+5] else "n/a"
                    print(f"      {nm:<5} {ohms[i]:6.1f} / {ohms[i+5]:6.1f}   ratio {r}")
                print("=" * 70)
                if is_fresh and ftype == 0xA7:
                    self.fresh_a7_received.set()
            elif ftype == 0xAA:
                token = parsed.get('scale_token')
                if token:
                    self.scale_token = token
                    print(f"[{time.strftime('%H:%M:%S')}] Scale Hello (AA) captured token: {token.hex()}")
            elif ftype == 0xA0:
                ack_seq = parsed['payload'][0] if len(parsed['payload']) > 0 else 0
                print(f"[{time.strftime('%H:%M:%S')}] Scale ACK (A0) for SEQ {ack_seq:02x}")
        except Exception as e:
            print(f"FFB3 parse error: {e} raw={raw.hex()}")

    async def execute_handshake(self):
        """
        Executes the exact Fitdays sequence with current timestamp and verified checksums.
        """
        now = int(time.time())
        tz_offset = 60  # BST (+01:00)
        target_w = self.current_live_weight if self.current_live_weight > 30.0 else self.weight_kg

        print(f"[{time.strftime('%H:%M:%S')}] Starting Handshake (User={self.name}, Weight={target_w:.2f}kg, Height={self.height_cm}cm, TS={now})")

        # 1. B0 30 (Wakeup/Hello)
        await self.write_cmd(build_b0_cmd(self.next_seq(), 0x30), "B0 cmd 0x30")
        await asyncio.sleep(0.15)

        # 2. C0 Profile Push (Timestamped)
        await self.write_cmd(build_c0_profile(self.next_seq(), now, self.height_cm, target_w, name=self.name, tz_offset_min=tz_offset), "C0 profile push (timestamped)")
        await asyncio.sleep(0.15)

        # 3. C1 Profile Push
        await self.write_cmd(build_c1_profile(self.next_seq(), self.height_cm, target_w, name=self.name), "C1 profile push")
        await asyncio.sleep(0.15)

        # 4. C0 Profile Push (repeat)
        await self.write_cmd(build_c0_profile(self.next_seq(), now, self.height_cm, target_w, name=self.name, tz_offset_min=tz_offset), "C0 profile push (repeat)")
        await asyncio.sleep(0.15)

        # 5. B6 Sweep Trigger (with advertised token)
        await self.write_cmd(build_b6_trigger(self.next_seq(), self.scale_token), "B6 sweep trigger")
        await asyncio.sleep(0.15)

        # 6. C0 Profile Push
        await self.write_cmd(build_c0_profile(self.next_seq(), now, self.height_cm, target_w, name=self.name, tz_offset_min=tz_offset), "C0 profile push")
        await asyncio.sleep(0.15)

        # 7. C1 Profile Push
        await self.write_cmd(build_c1_profile(self.next_seq(), self.height_cm, target_w, name=self.name), "C1 profile push")
        await asyncio.sleep(0.15)

        # 8. C0 Profile Push
        await self.write_cmd(build_c0_profile(self.next_seq(), now, self.height_cm, target_w, name=self.name, tz_offset_min=tz_offset), "C0 profile push")
        await asyncio.sleep(0.15)

        # 9. B0 31
        await self.write_cmd(build_b0_cmd(self.next_seq(), 0x31), "B0 cmd 0x31")
        await asyncio.sleep(0.15)

        # 10. B0 39
        await self.write_cmd(build_b0_cmd(self.next_seq(), 0x39), "B0 cmd 0x39")
        self.handshake_done = True
        print(f"[{time.strftime('%H:%M:%S')}] Handshake complete! Stand firmly on electrodes and hold still...")

    async def acknowledge_completion(self, final_weight: float):
        """
        Post-measurement finalization (B0 3A + updated C0/C1) as seen in Fitdays HCI capture.
        """
        now = int(time.time())
        print(f"[{time.strftime('%H:%M:%S')}] Finalizing measurement with B0 0x3A and updated weight {final_weight:.3f}kg...")
        await self.write_cmd(build_b0_cmd(self.next_seq(), 0x3A), "B0 cmd 0x3A (finalize)")
        await asyncio.sleep(0.2)
        await self.write_cmd(build_c0_profile(self.next_seq(), now, self.height_cm, final_weight, name=self.name), "C0 update stored weight")
        await asyncio.sleep(0.2)
        await self.write_cmd(build_c1_profile(self.next_seq(), self.height_cm, final_weight, name=self.name), "C1 update stored weight")
        print(f"[{time.strftime('%H:%M:%S')}] Scale session fully completed.")

async def main():
    print("=" * 70)
    print("Fitdays / Lefu 8-Electrode Scale Dynamic BIA Sweep Trigger")
    print(f"Target: {ADDR}")
    print("=" * 70)
    print("INSTRUCTIONS:")
    print("  1. Make sure the Fitdays phone app is CLOSED / Phone BT OFF.")
    print("  2. Step onto the scale BAREFOOT to wake it up and begin weighing.")
    print("  3. Stay standing on the scale during connection and handshake.")
    print("=" * 70)

    scanner = BleakScanner()
    deadline = time.time() + 180
    while time.time() < deadline:
        print("Scanning for scale...")
        dev = await BleakScanner.find_device_by_address(ADDR, timeout=8.0)
        if not dev:
            print("Scale not found or asleep. Tap scale to wake up...")
            await asyncio.sleep(1)
            continue

        print(f"Found {dev.name} ({dev.address}). Connecting...")
        try:
            async with BleakClient(dev, timeout=30.0) as client:
                print(f"Connected to {dev.name}!")
                session = ScaleSession(client)

                # Subscriptions: FFB3 first, then FFB2 (matching app order)
                await client.start_notify(FFB3, session.on_ffb3_indicate)
                await client.start_notify(FFB2, session.on_ffb2_notify)
                print("Subscribed to FFB3 (Indicate) and FFB2 (Notify).")

                # Wait for user to be on scale or timeout 20s
                wait_start = time.time()
                while time.time() - wait_start < 25:
                    if session.current_live_weight > 25.0:
                        print(f"User detected on scale: {session.current_live_weight:.2f} kg!")
                        break
                    await asyncio.sleep(0.25)

                # Trigger handshake while user is on scale
                await session.execute_handshake()

                # Wait for settled measurement and A7 frame
                print("Waiting for weight settlement and BIA impedance sweep...")
                try:
                    await asyncio.wait_for(session.fresh_a7_received.wait(), timeout=35.0)
                    print("\n SUCCESS! Fresh live BIA impedance sweep captured!")
                    await session.acknowledge_completion(session.current_live_weight)
                except asyncio.TimeoutError:
                    print("\n Timeout waiting for fresh A7 frame. (Did you step off or did weight not settle?)")

                print("Done. Disconnecting...")
                return

        except Exception as e:
            print(f"Connection error: {type(e).__name__}: {e}")
            await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
