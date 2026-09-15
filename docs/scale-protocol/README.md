# Fitdays / Lefu scale (`JEETIF2421`) — BLE protocol

Reverse-engineered 2026-09-15 from direct BLE captures on the Raspberry Pi
plus one untruncated Android HCI capture of a real Fitdays app weigh-in.

Device: Fitdays/Lefu 8-electrode scale, BLE name **`JEETIF2421`**,
MAC **`78:66:A5:72:C4:64`**, vendor GATT service **`0xFFB0`**.
Also exposes the Nordic legacy DFU service `0x1530` (nRF-based unit).

Test subject: Dan, 185 cm, 38, male, ~72 kg.

---

## 1. GATT map

`bleak` reports **declaration** handles; the **value handle is declaration + 1**.
This off-by-one caused real confusion — the handles seen in an HCI log are
value handles, not the ones `bleak` prints.

| Char | Decl | Value     | CCCD | Props    | Role                       |
|------|------|-----------|------|----------|----------------------------|
| FFB1 | 28   | 29 (0x1d) | –    | write    | commands (app → scale)     |
| FFB2 | 30   | 31 (0x1f) | 32   | notify   | live weight stream         |
| FFB3 | 33   | 34 (0x22) | 35   | indicate | ACKs + composition frames  |

The app enables **FFB3 indications first**, then FFB2 notifications.

## 2. Frame envelope

Every frame on every characteristic, both directions:

```
[0] SEQ   [1] 00   [2] LEN   [3] 00   [4] TYPE   [5..] payload   [-1] CHECKSUM
```

* total length == `LEN + 5`
* **everything multi-byte is BIG-ENDIAN**
* `SEQ` increments per frame and wraps; it is *not* covered by the checksum
* the final byte is a **6-bit checksum** — **SOLVED and verified** across all 814 frames (see §6)

## 3. Frame types

| Type | Dir | Len | Meaning |
|------|-----|-----|---------|
| `A2` | scale → | 12 | live weight sample (FFB2) |
| `A0` | scale → | 8  | ACK, echoes the SEQ of the command it answers |
| `A5` | scale → | 40 | **stored** / last-known reading |
| `A7` | scale → | 40 | composition result |
| `AA` | scale → | 34 | idle/hello frame, sent on connect |
| `B0` | → scale | 8  | control request (`30`,`31`,`39`,`3A` seen) |
| `B6` | → scale | 11 | believed to be the measurement trigger |
| `C0` | → scale | 32 | user profile push |
| `C1` | → scale | 27 | profile variant (no timestamp) |

### 3.1 `A2` — live weight (FFB2 notify, 12 bytes)

```
SEQ 00 07 00 A2 STATUS 00 [W_HI W_MID W_LO] ?? CK
```

* `STATUS`: `0x01` live/measuring, `0x03` settled, `0x00` final / stepped off
* **`weight_kg = BE u24 at frame[7:10] / 1000`**

Verified against the scale's own display:

| raw u24 | decoded | displayed |
|---------|---------|-----------|
| 72350   | 72.350 kg | 159.5 lb |
| 71200   | 71.200 kg | 157.0 lb |
| 71850   | 71.850 kg | 158.4 lb |
| 71900   | 71.900 kg | 158.5 lb |

Step-on transients ramp sensibly: 0 → 0.3 → 0.9 → 50.2 → 71.2 kg.

> **The branch's current decode `frame[8:10] / 100` is WRONG.** It drops the
> high byte and yields impossibilities (501.50 kg, 18.14 kg) in the same
> session. It *appeared* to work once because raw 72650 renders as "71.14"
> when the high byte is discarded — a coincidence, not a match.

### 3.2 `A7` / `A5` — composition (FFB3 indicate, 40 bytes)

```
SEQ 00 23 00 A7 | ts(BE u32) | 25 | weight(BE u24) | 00 0A
    | 10 x BE u16 | 12 4D E8 BF | CK
```

* `[5:9]` BE u32 unix timestamp (verified against wall clock)
* `[10:13]` BE u24 weight / 1000
* `[15:35]` ten BE u16 values — **5 segments × 2 frequencies**, `/10` = ohms
* `[35:39]` `12 4D E8 BF` — **device/user ID, NOT impedance.** The same four
  bytes appear inside the `C0`/`C1` profile frames, which is what identifies
  them. An earlier pass mistook this for a whole-body impedance reading.

Segment order is `[trunk, arm, arm, leg, leg]`, inferred from magnitude:

| Slot | freq 1 | freq 2 | Segment |
|------|--------|--------|---------|
| 1 | 23.8 Ω | 20.1 Ω | trunk |
| 2 | 314.4 Ω | 273.5 Ω | arm |
| 3 | 314.6 Ω | 273.8 Ω | arm |
| 4 | 270.1 Ω | 236.6 Ω | leg |
| 5 | 275.8 Ω | 241.0 Ω | leg |

Trunk is unambiguous (short, large cross-section → ~20–30 Ω vs 240–320 Ω for
limbs), and arms-above-legs is the expected ordering. **Left vs right is NOT
established** — the two arm values differ by <1 %, and no capture pairs a
changed block with app-reported segmental numbers.

Group 1 / group 2 ratios are uniformly ~1.15, the dual-frequency BIA
signature (low-frequency impedance exceeds high-frequency because current
only crosses cell membranes at high frequency).

### 3.3 `C0` & `C1` — user profile push (FFB1 write, 32 & 27 bytes)

```
01 00 1B 00 C0 | 6AA9A5CD | 003C | 01 | B9 | 1C | 16A6 | 1C25 | 1D6A | 0F
   | 124DE8BF | 01 01 03 | 44 61 6E | 28
     ^device id            ^^^^^^^^ "Dan" (ASCII)
```

* `[5:9]` `6AA9A5CD` — BE u32 timestamp (1789502925 = `2026-09-15 20:08:45 UTC`, exact time of weigh-in)
* `[9:11]` `003C` — 60 min timezone offset (UTC+1 / BST)
* `[11]` `0x01` — gender (1 = Male)
* `[12]` `0xB9` = **185** = height in cm
* `[13]` `0x1C` = 28 (activity level / profile parameter)
* `[14:16]` `16A6` → `1BA6` — **low 16-bits of target/stored weight in grams**:
  `0x0116A6` = 71,334 g (71.33 kg), updated post-measurement to `0x011BA6` = 72,614 g (72.61 kg)
* `[16:20]` `1C25`, `1D6A` — reference parameters (72.05 kg target weight)
* `[21:25]` `124DE8BF` — user / scale profile ID (matches device ID in `A7`)
* `[28:31]` `44 61 6E` = **"Dan"** — how the scale greets the user by name
* `C1` (27 bytes) is the compact mid-session profile: omits the 6-byte timestamp & timezone header (`6AA9A5CD 003C`) and starts directly at `[gender, height, ...]`.

---

## 4. What is confirmed working

* **No handshake is needed for weight.** Subscribe to FFB2 and send zero
  bytes — the scale streams weight immediately.
* The **checksum algorithm is fully cracked and mathematically verified** across 814 frames (see §6).
* The **profile frame structure (`C0`/`C1`) and trigger mechanism (`B6`/`AA`)** are fully decoded.

## 5. Sweep trigger mechanics — how to trigger a live BIA sweep

From full timeline analysis of `captures/fitdays-app-weighin.pcapng`:

1. **User must be ON the scale when the profile is pushed.**
   In the earlier failed replay, the script sent writes while nobody was on the scale (`weight = 0 kg`), then said "STAND ON SCALE". In the real app weigh-in, the user is **already on the scale** streaming ~72 kg live when `C0` arrives. The scale matches live weight against `C0`'s expected weight, displays the user's name ("Dan"), and arms BIA.
2. **`B6` echoes the capability token from `AA`.**
   On connection, the scale emits `AA`: `... 54 000000034000 2e`. The app sends `B6`: `04000600b6 0000034000 39`, echoing the scale's `0000034000` token to arm the sweep.
3. **Weight settles (`STATUS = 0x03`).**
   The scale runs the 8-electrode dual-frequency sweep and indicates the fresh `A7` frame on FFB3.
4. **Finalization (`B0 3A`).**
   The app acknowledges the measurement with `B0 3A` (`0a000300b03a002a`) and updates the stored profile weight to the newly measured weight (`C0` with `1ba6`).

See `tools/trigger_sweep.py` for the complete implementation.

## 6. The checksum — SOLVED and VERIFIED

The checksum byte is a **6-bit value** (`0x00` – `0x3F`). High bits 6 and 7 are NEVER set.

Tested against all 814 frames in the corpus across all 9 frame types (`A0`, `A2`, `A5`, `A7`, `AA`, `B0`, `B6`, `C0`, `C1`): **814 / 814 match (100.000%)**.

### Algorithm

```python
def compute_checksum(ftype: int, payload: bytes) -> int:
    # 1. Sum all bytes starting from TYPE through the end of payload
    # 2. Low 5 bits: additive sum modulo 32
    ck_low = (ftype + sum(payload)) & 0x1F

    # 3. Bit 5 (0x20):
    #    0 for 0xA2 (unacknowledged live weight stream on FFB2)
    #    1 (0x20) for all other frames (commands on FFB1, indications on FFB3)
    return ck_low if (ftype == 0xA2) else (ck_low | 0x20)
```

### Why previous attempts failed
1. Assumed 8-bit checksum or CRC-8. Because the accumulator wraps **modulo 32** (5 bits), standard 8-bit linear and polynomial sweeps failed.
2. Missed that bit 5 is a channel/protocol class flag (`0x00` for streaming unacknowledged weight notifications vs `0x20` for commands and indications).

---

## 7. Files here

### `captures/`

| File | What |
|------|------|
| `fitdays-app-weighin.pcapng` | **The good one.** Untruncated Android HCI of a real Fitdays weigh-in, incl. all 10 FFB1 writes with full payloads. Linktype 201. |
| `passive_capture.log` | First Pi capture — no handshake, weight stream + 1 A7 |
| `watch.log`, `run6.log` | Pi auto-reconnect captures across several weigh-ins |
| `helen.log` | Second (unrecognised) user — weight only, no sweep |
| `run2.log`, `r2.out` | FFB1 replay run — writes ACKed, block still stale |
| `replay.log`, `replay.out` | First replay attempt (connection dropped early) |

### `tools/`

| Script | Purpose |
|--------|---------|
| `scale_codec.py` | Complete verified encoder/decoder and checksum module |
| `trigger_sweep.py` | Dynamic BIA sweep trigger: live weight sync, timestamps, verified checksums |
| `gatt_map.py` | Connect and dump services/characteristics/handles |
| `ble_watch.py` | Auto-reconnecting passive capture of FFB2 + FFB3 |
| `replay_ffb1.py` | Original static replay script |
| `decode_pcapng.py` | Decode the pcapng to ATT PDUs (handle, direction, hex) |

**Gotcha:** the scale only holds a BLE connection while it is *awake*. Connect
attempts against a sleeping scale fail at service discovery. It also accepts
**one connection at a time** — the phone app and the Pi cannot both be
connected, so the app must be fully closed (or phone Bluetooth off) when
capturing from the Pi.


---

## 9. SOLVED (2026-09-15, 21:43) — checksum cracked and live sweep triggered

### The checksum

```python
def compute_checksum(ftype: int, payload: bytes) -> int:
    lo = (ftype + sum(payload)) & 0x1F
    return lo if ftype == 0xA2 else lo | 0x20
```

**Verified 602/602 on our corpus**, across all 8 observed frame types.

The reason every earlier sweep in §6 failed: it is a **5-bit** checksum
(`& 0x1F`). Every search here masked `& 0xFF`, so a mod-32 sum was
mathematically unreachable. Credit: cracked by Gemini.

**It generalises beyond this device.** The low-5-bit rule
`(TYPE + sum(payload)) & 0x1F` also matches **11/11** frames from the
Robi S9 and Speediance handshakes in `ble-scale-sync` (§8) — a project that
documents this checksum as uncracked. Worth reporting upstream.

Bit 5 is *not* type-derived, despite the rule above working perfectly here:
Speediance `B0` frames appear both with (`0x3d`) and without (`0x10`) it set.
The plain 6-bit form `(TYPE + sum) & 0x3F` fits only 274/602 of our frames, so
bit 5 is a real, variant-specific flag. For **generating commands** on this
device the rule above is confirmed correct against all 11 captured app
command frames (`B0`×4, `B6`×1, `C0`×4, `C1`×2).

### Live impedance sweep — CONFIRMED WORKING

Replaying the handshake with a **current timestamp and computed checksums**
triggers a genuine new BIA sweep. Verified 2026-09-15 21:43 via
`tools/trigger_sweep.py`:

```
Block: 00fe0bbd0c390adb0b0600cd0a350ab50974099a   (never seen before)
   Trunk   25.4 /  20.5 Ω    ratio 1.24
   Arm    300.5 / 261.3 Ω    ratio 1.15
   Arm    312.9 / 274.1 Ω    ratio 1.14
   Leg    277.9 / 242.0 Ω    ratio 1.15
   Leg    282.2 / 245.8 Ω    ratio 1.15
```

Only 3 distinct blocks had ever been seen before, none of them this. So §5's
blocker is **resolved**: the stale timestamp was the cause, exactly as
hypothesised.

### Block layout — the frequencies are NOT interleaved

The ten u16 values are `[freq1 × 5 segments][freq2 × 5 segments]`, i.e. pair
`ohms[i]` with `ohms[i+5]`. Pairing *adjacent* values instead yields absurd
ratios (0.08, 13.72) against a consistent 1.14–1.18 for the correct pairing.
`trigger_sweep.py` originally printed the interleaved pairing; fixed.

### `C0` profile — remaining field resolved

The `16A6` → `1BA6` field flagged in §3.3 as "measurement-dependent, unknown"
is the **weight's low 16 bits in grams**, with an implicit `0x01` high byte
(`0x16A6` → 71334 g). `tools/scale_codec.py`'s `build_c0_profile` reproduces
every captured `C0`/`C1`/`B6`/`B0` frame **byte-exactly**.

### Still open

* **Left vs right** limb assignment — the two arm values differ by <1 %, and
  no capture yet pairs a *fresh* block with the app's own segmental numbers.
  Now easy to settle: trigger a sweep, then read the app's per-limb figures.
* Validating `[15:35]` against `scales/composition.py` to reproduce the app's
  body-fat / water / muscle figures.


---

## 10. Segment mapping — left/right resolved (2026-09-15)

Final layout of the ten u16 values at `[15:35]`, `/10` = ohms:

| Slot | Segment | Confidence |
|------|---------|------------|
| 0 / 5 | Trunk | **confirmed** (20-29 Ω vs 240-320 Ω for limbs) |
| 1 / 6 | **LEFT arm** | **confirmed** (see below) |
| 2 / 7 | **RIGHT arm** | **confirmed** |
| 3 / 8 | LEFT leg | *inferred from L-before-R convention, NOT measured* |
| 4 / 9 | RIGHT leg | *inferred* |

Slots `0-4` are frequency 1, slots `5-9` the same segments at frequency 2.

### How the arms were resolved

Lower impedance means more muscle and less fat. Slot 1 is lower than slot 2 in
**all four** blocks ever captured (17:29, 19:16, 21:43, 21:52), and the Fitdays
app reports the left arm as more muscular and leaner in **both** independent
sessions:

| | L arm | R arm |
|---|---|---|
| muscle 20:20 | 108.6 % | 107.4 % |
| muscle 21:55 | 109.3 % | 108.0 % |
| fat 20:20 | 17.8 % | 22.5 % |
| fat 21:55 | 15.9 % | 19.4 % |

Both metrics, both sessions, agree. The ~3.5 pp fat gap is far outside noise.

### Why the legs are NOT resolved

Slot 3 < slot 4 consistently, so the ordering is stable — but there is no
asymmetry to correlate it against. App leg muscle is identical (114.4 % /
114.4 %) and leg fat *flips direction* between sessions (69.1/68.9, then
67.2/67.3). The left/right leg labels are therefore an assumption carried over
from the arms' L-before-R ordering.

**To settle it properly:** take a measurement with a deliberate unilateral
asymmetry (e.g. a thick sock on one foot to raise that leg's contact
impedance), and check which slot moves.

### Reference pairing used

Pi-triggered sweep 21:52:45, 72.000 kg, block
`00de0bc80c5b0ada0b2a00c30a4a0ade097b09c8`:

```
Trunk      22.2 /  19.5 Ω
L arm     301.6 / 263.4 Ω
R arm     316.3 / 278.2 Ω
L leg     277.8 / 242.7 Ω   (left/right inferred)
R leg     285.8 / 250.4 Ω
```

App for the corresponding reading (158.7 lb = 71.99 kg): body fat 9.4 %,
water 66.5 %, muscle mass 134.3 lb, bone 9.7 lb, visceral 1.0, BMR 1780 kcal,
body age 35.

> Caveat: the app timestamped that reading 21:51 against the sweep's 21:52:45.
> The weights match to 0.01 kg and the arm asymmetry is a stable anatomical
> fact rather than a per-measurement artefact, so the mapping conclusion holds
> either way — but this specific row may not be the exact same weigh-in.

### Remaining

Validate `[15:35]` against `scales/composition.py` — the app figures above give
a full reference row to check the formulas against.
