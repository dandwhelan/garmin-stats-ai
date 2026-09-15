# Handover — Fitdays/Lefu scale HCI capture (Windows session)

## Your job in this session
Capture an **untruncated Bluetooth HCI log** of a real Fitdays weigh-in on a
Windows PC, then extract the **FFB1 command sequence** the app sends. That
sequence is the one missing piece blocking body-composition decoding.

Do NOT re-derive the protocol below — it is already confirmed from direct
BLE captures on a Raspberry Pi. Start from it.

---

## Background

Project: `garmin-data` monorepo (`dandwhelan/garmin-stats-ai`), Raspberry Pi.
Scale integration lives in `garmin-insights/src/garmin_insights/scales/`.
Work branch: `claude/bluetooth-scales-garmin-sync-ooha7t` (draft PR #74).

Device: Fitdays/Lefu 8-electrode scale, BLE name **`JEETIF2421`**,
MAC **`78:66:A5:72:C4:64`**, vendor GATT service **`0xFFB0`**.
Also exposes Nordic legacy DFU service `0x1530` (nRF-based).

User: Dan, 185 cm, age 38, male, ~71.9 kg / 158.5 lb.

---

## CONFIRMED protocol (verified on-device — do not re-investigate)

### GATT map
bleak reports DECLARATION handles; **value handle = declaration + 1**.

| Char | Decl | Value     | CCCD | Props    | Role                    |
|------|------|-----------|------|----------|-------------------------|
| FFB1 | 28   | 29 (0x1d) | -    | write    | commands  <-- THE TARGET |
| FFB2 | 30   | 31        | 32   | notify   | live weight stream      |
| FFB3 | 33   | 34 (0x22) | 35   | indicate | composition frame       |

### Frame envelope (all frames, both characteristics)
    [0] SEQ  [1] 00  [2] LEN  [3] 00  [4] TYPE  [5..] payload
Total length == LEN + 5. **Everything multi-byte is BIG-ENDIAN.**
SEQ increments and wraps. The final byte is NOT a checksum over the frame
(exhaustive sum/xor over all ranges + CRC-8 sweeps over 144 frames all failed).

### Type 0xA2 — live weight (FFB2 notify, 12 bytes)
    SEQ 00 07 00 A2 STATUS 00 [W_HI W_MID W_LO] ?? 
  STATUS: 0x01 = live, 0x03 = settled, 0x00 = final/stepped-off
  **weight = BE u24 at frame[7:10] / 1000**
  Verified: 72.350 / 71.200 / 71.850 / 71.900 kg == displayed
  159.5 / 157.0 / 158.4 / 158.5 lb.

### Type 0xA7 — composition (FFB3 INDICATE, 40 bytes)
    SEQ 00 23 00 A7 | ts(BE u32) | 25 | weight(BE u24) | 00 0A | 10x BE u16 | 124D E8BF ck
  [5:9]   BE u32 unix timestamp (verified against wall clock)
  [10:13] BE u24 weight / 1000
  [15:35] ten BE u16 values = 5 segments x 2 frequencies, /10 = ohms
          order [trunk, arm, arm, leg, leg], trunk ~20-29, arms ~270-315,
          legs ~240-282 -- correct physiological ordering
  [35:39] `124D E8BF` constant in every frame
  [39]    varies (likely checksum)

### KEY BLOCKER (why this capture is needed)
No handshake is required for **weight** — subscribing and sending zero bytes
streams weight fine. But the **impedance block never refreshes**: across 5 A7
frames spanning 4 hours and two different people, only **2 distinct blocks**
were ever seen, byte-identical even when the scale greeted the user by name
and ran its "extra measurements", and even across a full Fitdays app session.

=> The live BIA result is NOT in the frames an unprompted subscriber receives.
It is gated behind commands the app writes to **FFB1**.

A phone HCI log showed the app writing **7 frames to FFB1 (handle 0x1d)**
immediately after connecting, with value lengths **8, 32, 27, 32, 11, 32, 27**
and first bytes `00 00 03`, `01 00 1b`, `02 00 16`, `03 00 1b`, `04 00 06`,
`05 00 1b`, `06 00 16` (SEQ 00..06). Payloads were TRUNCATED and are unknown.
**Recovering those 7 payloads in full is the goal.**

---

## Capture procedure (Windows)

### Prerequisites
1. Install **Wireshark**, ticking the **extcap** plugins during setup.
2. Install **Android platform-tools** (adb).
3. On the phone (Pixel 9 Pro XL), Developer options:
   - **Enable Bluetooth HCI snoop log** -> set to **Enabled/Full** (or "Socket").
     **NOT "Filtered"** — filtered silently strips the payload bytes needed.
   - **USB debugging** -> on.
   - Toggle Bluetooth **off then on** (or reboot) so the snoop mode takes effect.
4. Connect phone by **USB cable**, tap **Allow** on the prompt.
5. Verify:  `adb devices`        (phone must be listed, not "unauthorized")
            `adb shell settings get global bluetooth_btsnoop_log_mode`

### The capture
1. Open Wireshark. Find interface **"Android Bluetooth Btsnoop Net <serial>"**.
2. Start capture on it.
3. **Force-stop Fitdays first**, then open it — the FFB1 handshake is only sent
   on a fresh connection.
4. Step on the scale **barefoot**, stand still, and wait for the **full
   body-composition screen to populate live**.
5. Stop the capture only after the numbers appear.
6. File -> Save As -> `.pcapng`.

### CRITICAL pitfalls (both previous attempts died on these)
- **Do NOT use `adb bugreport` / btsnooz.** That format truncates every ACL
  payload to ~15 bytes. A previous log had 74 truncated records and was
  unusable. Verify: original_length must equal included_length.
- **The measurement must happen INSIDE the recording window.** A previous
  capture contained only a stale report screen pulled from history — 130+
  seconds with no measurement in it.
- `adb pull /data/misc/bluetooth/logs/btsnoop_hci.log` usually fails with
  permission denied on stock Android (no root). Use the Wireshark socket.

---

## What to extract

Filter in Wireshark:  `btatt`
Look for **writes to handle 0x001d (29)** from the phone to the scale.

Report, for each of the ~7 writes, in order:
- the full value bytes as hex
- the value length
- timing relative to connection

Also capture, for cross-referencing against the app's displayed numbers:
- any **FFB3 indications from handle 0x0022 (34)** — especially a 40-byte
  0xA7 frame whose `[15:35]` block DIFFERS from the known-stale
  `01260c050c550ac20b0300dd0a6b0ad709600998`
- the exact composition values Fitdays displays for that weigh-in
  (weight, body fat %, water %, muscle, bone, visceral, BMR, metabolic age,
  and the per-limb segmental breakdown if shown)

A changed `[15:35]` block is the proof that the FFB1 commands trigger a live
impedance sweep.

---

## Deliverable back to the Pi session
- the `.pcapng` file
- the decoded FFB1 write payloads
- the app's displayed numbers for that weigh-in

## Separately ready to land (independent of all the above)
The weight decode fix: branch currently uses `frame[8:10] / 100`, which is
WRONG and yields impossibilities (501.50 kg, 18.14 kg in the same session).
Correct is **BE u24 `frame[7:10]` / 1000**. Also: the adapter never subscribes
to FFB3, and its `_decode_impedance` gates on `len==40 and frame[0]==0xFF`,
which never matches the real frame (frame[0] is SEQ; frame[4]==0xA7).
