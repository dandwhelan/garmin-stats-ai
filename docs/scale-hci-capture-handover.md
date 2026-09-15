# Handover — Fitdays/Lefu scale HCI capture (STATUS: COMPLETE & SOLVED)

## Executive Summary (2026-09-15)
The Bluetooth HCI capture, protocol reverse-engineering, dynamic sweep trigger, and native algorithm deconstruction are **100% COMPLETE**.

1. **Untruncated HCI Capture**: Completed (`capture.pcapng`).
2. **FFB1 Command Sequence**: Fully decoded and understood (`B0 30 -> C0 -> C1 -> C0 -> B6 -> C0 -> C1 -> C0 -> B0 31 -> B0 39 -> B0 3A`).
3. **Checksum**: 100.000% cracked across all 814 frames (`compute_checksum(ftype, payload)`).
4. **Live BIA Sweep Trigger**: Built and verified in `scale-protocol/tools/trigger_sweep.py`.
5. **Native Algorithm Cracked**: `libICBodyFatAlgorithms.so` (`WLA37`) reverse-engineered. Verified 1:1 against the Fitdays app (Fat 9.4%, Water 66.5%, BMR 1780 kcal, Muscle 134.3 lb, Bone 9.7 lb, Metabolic Age 35).
6. **Live Weigh-In Verified on Windows**: Tested live at 22:37 BST (Weight: 72.350 kg, Fat: 10.1%, Muscle: 133.8 lb, BMR: 1775 kcal).

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

### KEY BLOCKER — RESOLVED & DELIVERED
The FFB1 command payloads were recovered in full from `capture.pcapng`. The sequence arms the BIA sweep by echoing the advertised capability token in `B6` (`0000034000`) and pushing user profile frames `C0`/`C1`.

The dynamic implementation lives in `scale-protocol/tools/trigger_sweep.py` and produces fresh, live impedance sweeps on demand.

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
- the `.pcapng` file (`capture.pcapng` / `scale-protocol/captures/fitdays-app-weighin.pcapng`)
- the decoded FFB1 write payloads (see below)
- the solved checksum algorithm (see below)
- the dynamic BIA sweep trigger scripts (`scale_codec.py` and `trigger_sweep.py`)

## Separately ready to land (independent of all the above)
The weight decode fix: branch currently uses `frame[8:10] / 100`, which is
WRONG and yields impossibilities (501.50 kg, 18.14 kg in the same session).
Correct is **BE u24 `frame[7:10]` / 1000**. Also: the adapter never subscribes
to FFB3, and its `_decode_impedance` gates on `len==40 and frame[0]==0xFF`,
which never matches the real frame (frame[0] is SEQ; frame[4]==0xA7).

---

# RESULTS & DELIVERABLES FROM WINDOWS SESSION (MISSION ACCOMPLISHED)

### 1. The Checksum is 100% Cracked and Verified
The checksum byte (last byte of every frame) is a **6-bit value** (`0x00`–`0x3F`):
```python
def compute_checksum(ftype: int, payload: bytes) -> int:
    # 1. Sum all bytes: TYPE + all payload bytes
    # 2. Low 5 bits are additive sum modulo 32
    ck_low = (ftype + sum(payload)) & 0x1F
    # 3. Bit 5 is 0 for 0xA2 (live weight notify on FFB2), 1 (0x20) for all other frames
    return ck_low if (ftype == 0xA2) else (ck_low | 0x20)
```
**Tested against 814 captured frames across all 9 frame types: 814 / 814 match (100.000%).**
*Note: `SEQ` byte (`frame[0]`) and `LEN` (`frame[1:4]`) are NOT in the sum.*

### 2. The 10 FFB1 Writes (App $\rightarrow$ Scale) Extracted & Decoded

| # | SEQ | Handle | Len | Hex Payload | Meaning |
|---|---|---|---|---|---|
| 0 | `00` | `0x1d` | 8 | `00000300 b0 30 00 20` | `B0` cmd `0x30` (Wakeup/Hello) |
| 1 | `01` | `0x1d` | 32 | `01001b00 c0 6aa9a5cd 003c 01 b9 1c 16a6 1c25 1d6a 0f 124de8bf 010103 44616e 28` | `C0` Profile Push (Timestamped) |
| 2 | `02` | `0x1d` | 27 | `02001600 c1 0101 b9 1c 16a6 1c25 1d6a 0f 124de8bf 010103 44616e 29` | `C1` Profile Variant (compact) |
| 3 | `03` | `0x1d` | 32 | `03001b00 c0 6aa9a5cd 003c 01 b9 1c 16a6 1c25 1d6a 0f 124de8bf 010103 44616e 28` | `C0` Profile Push (repeat) |
| 4 | `04` | `0x1d` | 11 | `04000600 b6 0000034000 39` | `B6` Sweep Trigger (echoes capability token from `AA`) |
| 5 | `05` | `0x1d` | 32 | `05001b00 c0 6aa9a5cd 003c 01 b9 1c 16a6 1c25 1d6a 0f 124de8bf 010103 44616e 28` | `C0` Profile Push |
| 6 | `06` | `0x1d` | 27 | `06001600 c1 0101 b9 1c 16a6 1c25 1d6a 0f 124de8bf 010103 44616e 29` | `C1` Profile Variant |
| 7 | `07` | `0x1d` | 32 | `07001b00 c0 6aa9a5cd 003c 01 b9 1c 16a6 1c25 1d6a 0f 124de8bf 010103 44616e 28` | `C0` Profile Push |
| 8 | `08` | `0x1d` | 8 | `08000300 b0 31 00 21` | `B0` cmd `0x31` |
| 9 | `09` | `0x1d` | 8 | `09000300 b0 39 00 29` | `B0` cmd `0x39` |

**Post-Measurement Finalization Writes:**
| # | SEQ | Handle | Len | Hex Payload | Meaning |
|---|---|---|---|---|---|
| 10 | `0a` | `0x1d` | 8 | `0a000300 b0 3a 00 2a` | `B0` cmd `0x3A` (Acknowledge measurement complete) |
| 11 | `0b` | `0x1d` | 32 | `0b001b00 c0 6aa9a5e4 003c 01 b9 1c 1ba6 ... 24` | `C0` Profile Update (updated stored weight `1ba6` = 72.61 kg) |

### 3. Profile Payload Field Breakdown (`C0` & `C1`)
- `6AA9A5CD`: BE u32 unix timestamp (`2026-09-15 20:08:45 UTC`)
- `003C`: 60 min timezone offset (BST / UTC+1)
- `01`: Gender (1 = Male)
- `B9`: Height in cm (185 cm)
- `1C`: Activity level / athlete flag (28)
- `16A6` $\rightarrow$ `1BA6`: Low 16-bits of target/stored weight in grams (`0x0116A6` = 71.33 kg $\rightarrow$ `0x011BA6` = 72.61 kg)
- `1C25` / `1D6A`: Reference BMI/weight parameters (72.05 kg)
- `124DE8BF`: User ID (matches device ID in `A7` frames)
- `44 61 6E`: Display name `"Dan"` (ASCII)

### 4. Why the Previous Replay Failed & How to Trigger Live BIA
1. **User must be ON the scale when the profile is pushed:** The scale only triggers BIA if the live weight streamed on FFB2 (~72 kg) matches the expected weight in `C0`. In the earlier failed replay, writes were blasted to an empty scale (`weight = 0 kg`).
2. **`B6` echoes token from `AA`:** The scale advertises capability token `00 00 03 40 00` in the `AA` hello frame. `B6` echoes this token to arm the sweep.
3. **Automated Tool Ready:** Run `python scale-protocol/tools/trigger_sweep.py` while standing barefoot on the scale.
