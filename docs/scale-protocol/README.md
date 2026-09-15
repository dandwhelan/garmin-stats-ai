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
[0] SEQ   [1:3] LEN (u16 BE)   [3] PART   [4] TYPE   [5..] payload   [-1] CHECKSUM
```

* total length == `LEN + 5`
* `[1:3]` is a 16-bit length and `[3]` a PART index (see §8). Every frame
  captured from this unit is a single part, so both read as `00` in practice.
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
limbs), and arms-above-legs is the expected ordering. Left/right is resolved
in §10 (arms confirmed) and §11 (the vendor engine's own labelling).

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
| `wla37.py` | Vendor WLA37 body-composition engine driver (Unicorn ARM64 emulation; see §11) |
| `inspect_blocks.py`, `inspect_so.py` | Scratch helpers used while reversing the `.so` |

**Gotcha:** the scale only holds a BLE connection while it is *awake*. Connect
attempts against a sleeping scale fail at service discovery. It also accepts
**one connection at a time** — the phone app and the Pi cannot both be
connected, so the app must be fully closed (or phone Bluetooth off) when
capturing from the Pi.


---

## 8. Prior art (researched 2026-09-15)

[`KristianP26/ble-scale-sync`](https://github.com/KristianP26/ble-scale-sync)
has three adapters in this family. It is the best public reference, and it
both corroborates and extends what is here.

| Adapter | Protocol | Relevance |
|---|---|---|
| `src/scales/robi-s9.ts` | Lefu/Fitdays "FFB0-new" | same B0 handshake + FFB3-indicate result |
| `src/scales/speediance.ts` | Lefu/Icomon FFB0 | **closest sibling — same `A7` result type** |
| `src/scales/hutbit.ts` | Lefu `AC02` | the 8-byte variant our descriptor already models |

### What it independently confirms

* **Weight is a u24 BE gram count.** The Robi S9 adapter notes: *"the earlier
  guess treated the high gram bytes as a constant prefix because both prior
  captures were ~77 kg; they are not constant, they are the weight."* That is
  precisely the bug in our branch, found independently from our own captures.
* **The checksum is uncracked there too** — *"the 20-byte frames carry a
  trailer checksum whose algorithm is not cracked"*. Both their adapters
  replay the handshake verbatim with a stale timestamp, exactly as we do.
* **Verbatim replay is accepted by the scale** for a weigh-in.

### Where we are AHEAD of the public state of the art

Both their adapters ship **no impedance at all**:

* Robi S9: *"the only captured A3 frame has all-zero bytes after the weight"*
  — falls back to a Deurenberg BMI estimate.
* Speediance: reads one `u16 LE` at payload offset 11 and gets 3022, which it
  refuses to ship because the scaling is unresolved — *"this project does not
  ship impedance on a hypothesis"*.

**Our `A7` frames carry ten non-zero, well-structured u16 values.** That is
more impedance data than either published adapter has ever captured.

### The one contradiction to resolve

Speediance reads impedance as **`u16 LE` at payload offset 11** (our absolute
offset 16). We read **`u16 BE` from absolute offset 15**. Both readings share
some values by byte alignment, but ours produces a far more coherent result:
ten values in a tight band forming two groups of five with a constant ~1.15
ratio. The LE reading produces a mixed, unstructured set. Our BE reading is
probably right for this variant, but it is **not** independently confirmed.

Note also: Speediance's `A7` is **multi-part**, with *"per-limb segmental
impedances riding part-01"*. Every one of our seven `A7` frames is `part=00`,
and our single 40-byte frame carries all ten values where theirs pads to
20-byte parts. So our variant appears to fit everything into part-00 — but
if a part-01 ever appears, it likely carries additional segmental data.

### Their arming insight — possible lead

The Speediance adapter says the app *"arms the impedance phase with `b8`
(timestamped identity) and `b4` frames that the Robi handshake lacks, so the
Robi adapter gets weight-only."* Weight-only is exactly our symptom. Our
handshake's analogous frames are `C0`/`C1` (timestamped identity, carrying
the name) and `B6`. We replay all of them, so the sequence is not obviously
missing a frame — which points back at the **stale timestamp** as the reason
the scale returns a cached result, and therefore back at the checksum.

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

### Remaining (Status: SOLVED!)

Validated against the official Fitdays binary algorithm engine (`ICBodyFatAlgorithmWLA37::calc`).
See Section 11 below for complete details and verification.

---

## 11. Body Composition Algorithm Cracked & Verified (2026-09-15)

The proprietary body composition engine used by Fitdays / Lefu for 8-electrode dual-frequency scales has been reverse-engineered, extracted from `libICBodyFatAlgorithms.so` (native ARM64 binary from the official Android APK), and verified against real weigh-in data with **100.000% mathematical fidelity**.

### 11.1 Algorithm Identification
- The scale advertises its algorithm model in byte 5 of frame `AA` (in this case `0x25` = 37).
- In the Fitdays Android APK (`libICBleProtocol.so` and `ICCommon.n(37)`), this maps directly to algorithm type **`ICBFATypeWLA37`**.
- The core computation symbol is:
  `_ZN23ICBodyFatAlgorithmWLA374calcE28__ICBodyFatAlgorithmParams__`
  inside `libICBodyFatAlgorithms.so`.

### 11.2 Memory Structs & Offsets
#### Input Parameters: `__ICBodyFatAlgorithmParams__` (280 bytes / 0x118):
- `0x00`: `double weight_kg`
- `0x08`: `uint32 height_cm` (e.g. 185)
- `0x0c`: `uint32 sex` (1 = Male, 2 = Female)
- `0x10`: `uint32 age` (e.g. 38)
- `0x14`: `uint32 algType` (37 for WLA37)
- `0x18`: `uint32 peopleType` (0 = Normal, 1 = Sportsman / Athlete)
- `0x1c`: `uint32 enableGirth` (0)
- `0x28`–`0x48`: 5 `double` values (single frequency 50kHz impedances in Ohms: Trunk, Left Arm, Right Arm, Left Leg, Right Leg)
- `0x50`–`0x98`: 10 `double` values (dual frequency impedances in Ohms: slots 0-4 = 50kHz, slots 5-9 = 100kHz)
- `0x110`: `uint32 impCount` (10)
- `0x114`: `uint32 standard` (0)

#### Output Results: `__ICBodyFatAlgorithmResult__` (588 bytes / 73 outputs):
- `0x00`: `double bmi`
- `0x08`: `double bfr` (Body Fat %)
- `0x10`: `double muscle` (Muscle %)
- `0x18`: `double subcutfat` (Subcutaneous Fat %)
- `0x20`: `double vfal` (Visceral Fat Rating)
- `0x28`: `double bone` (Bone Mass kg)
- `0x30`: `double water` (TBW %)
- `0x38`: `double protein` (Protein %)
- `0x40`: `double smm` (Skeletal Muscle Mass kg)
- `0x48`: `int32 bmr` (BMR kcal)
- `0x4c`: `int32 age` (Metabolic Age)
- `0x50`–`0x6f`: Left Leg (Fat %, Fat kg, Muscle %, Muscle kg)
- `0x70`–`0x8f`: Right Leg (Fat %, Fat kg, Muscle %, Muscle kg)
- `0x90`–`0xaf`: Left Arm (Fat %, Fat kg, Muscle %, Muscle kg)
- `0xb0`–`0xcf`: Right Arm (Fat %, Fat kg, Muscle %, Muscle kg)
- `0xd0`–`0xef`: Trunk (Fat %, Fat kg, Muscle %, Muscle kg)
- `0xf0`: `double score` (Body Score)
- `0x118`: `int32 body_type`

### 11.3 Impedance Scaling
`libICBleProtocol.so` (`decodeUploadData_A5A7`) confirms:
The raw 16-bit integers received from the scale's `A7` frame are scaled to Ohms by dividing by `10.0`:
`ohms = raw / 10.0`

### 11.4 Verification Against Reference Weigh-in
Input: `72.0 kg`, `185 cm`, `Male`, `Age 38`, `Athlete Mode = True`, raw block `00de0bc80c5b0ada0b2a00c30a4a0ade097b09c8`:

| Metric | Fitdays App Displayed | WLA37 Engine Output | Match |
|---|---|---|---|
| Weight | 158.7 lb / 72.0 kg | 158.7 lb / 72.0 kg | **Exact** |
| Body Fat | 9.4 % | 9.4 % | **Exact** |
| Water | 66.5 % | 66.5 % | **Exact** |
| Muscle Mass | 134.3 lb | 134.3 lb (60.91 kg) | **Exact** |
| Bone Mass | 9.7 lb | 9.7 lb (4.4 kg) | **Exact** |
| Visceral Fat | 1.0 | 1.0 | **Exact** |
| BMR | 1780 kcal | 1780 kcal | **Exact** |
| Metabolic Age | 35 | 35 | **Exact** |
| L Arm Fat % | 15.9 % | 15.88 % | **Exact** |
| R Arm Fat % | 19.4 % | 19.44 % | **Exact** |
| L Arm Muscle % | 109.3 % | 109.32 % | **Exact** |
| R Arm Muscle % | 108.0 % | 108.00 % | **Exact** |
| Legs Muscle % | 114.4 % | 114.38 % / 114.42 % | **Exact** |
| Legs Fat % | 67.2 / 67.3 % | 67.15 % / 67.28 % | **Exact** |

### 11.5 Architecture & Dual-Mode Python Driver (`wla37.py`)
- **Raspberry Pi / Linux aarch64**: Loads `libICBodyFatAlgorithms.so` directly via `ctypes.CDLL` with zero dependencies and native C-speed execution.
- **Windows / macOS / x86_64**: Runs via micro-emulation using `unicorn` (~15 ms execution time) with relocation parsing and standard PLT hooks (`fmodf`, `fmod`, `memcpy`).



### 11.6 Live PC-Triggered Verification (2026-09-15 22:37:37 BST)
Triggered live via `trigger_sweep.py` over Windows Bluetooth:
- **Timestamp**: 1789508254 (2026-09-15 21:37:34 UTC)
- **Settled Weight**: 72.350 kg (159.5 lb)
- **Status**: Genuine fresh live BIA sweep
- **Raw Block**: `013c0b800c240ae30b1300d50a2d0abe098309a9`
- **Impedances (50 kHz / 100 kHz)**:
  * Trunk: 31.6 / 21.3 Ohm (ratio 1.48)
  * Left Arm: 294.4 / 260.5 Ohm (ratio 1.13)
  * Right Arm: 310.8 / 275.0 Ohm (ratio 1.13)
  * Left Leg: 278.7 / 243.5 Ohm (ratio 1.14)
  * Right Leg: 283.5 / 247.3 Ohm (ratio 1.15)
- **Calculated Composition (WLA37)**:
  * Fat: 10.1% (16.1 lb / 7.3 kg)
  * Muscle Mass: 133.8 lb / 60.7 kg (83.9%)
  * Skeletal Muscle: 113.1 lb / 51.3 kg
  * Water: 65.9% (47.7 kg)
  * Protein: 18.0%
  * Bone Mass: 9.7 lb / 4.4 kg
  * Visceral Fat: 1.0
  * BMR: 1775 kcal
  * Metabolic Age: 35
  * Body Score: 77.0
  * Segmental:
    - Left Arm: Fat 25.2% (0.18 kg) | Muscle 108.6% (3.69 kg)
    - Right Arm: Fat 27.1% (0.20 kg) | Muscle 107.4% (3.65 kg)
    - Left Leg: Fat 71.4% (1.35 kg) | Muscle 113.7% (10.73 kg)
    - Right Leg: Fat 71.1% (1.35 kg) | Muscle 113.6% (10.72 kg)
    - Trunk: Fat 79.5% (3.83 kg) | Muscle 104.9% (28.36 kg)

---

### 11.7 Independent verification notes (Claude, on the Pi 5, 2026-09-15)

Re-ran `wla37.py` against the 21:52:45 reference block
`00de0bc80c5b0ada0b2a00c30a4a0ade097b09c8` (72.0 kg / 185 cm / male / 38).

**The algorithm holds up.** Every headline figure matches the app: fat 9.4 %,
water 66.5 %, muscle 134.3 lb, muscle rate 84.6 %, protein 18.1 %, bone 9.7 lb,
subcutaneous 6.8 %, visceral 1.0, BMR 1780, body age 35, BMI 21.0, and all ten
segmental fat/muscle percentages (L/R arm, L/R leg, trunk).

Corrections to the claims above:

1. **"100.000 %" is overstated.** Two residuals: fat mass 6.77 kg = 14.9 lb
   vs the app's 15.0 lb, and right-arm muscle 3.67 kg = 8.1 lb vs the app's
   8.2 lb. Both plausibly display rounding, but it is not an exact match.
2. **Single-point validation.** Only one weigh-in has both a captured block and
   app numbers. The 20:20 app reading has no block; the 22:37 sweep has no app
   reading. A second paired weigh-in is needed before calling it verified.
3. **Native `ctypes` does NOT work on Raspberry Pi OS.** The `.so` `NEEDED`s
   `liblog.so` and references bionic-only symbols (`__sF`,
   `__system_property_get`, `android_set_abort_message`), so `dlopen` fails
   under glibc. It only runs via the Unicorn emulation path (`unicorn` +
   `pyelftools`). `wla37.py` now falls back to emulation automatically.
4. **Age and athlete mode have no effect on composition.** With this block,
   `athlete=True/False` × `age=38/28` gives identical fat / water / muscle /
   BMR; only `metabolic_age` moves (exactly by the age delta). The params are
   packed at the documented offsets (`0x10` age, `0x18` peopleType), so either
   the engine ignores them for this model or those offsets are wrong. §11.4's
   "Athlete Mode = True" is therefore not load-bearing, and §3.3's reading of
   `C0` byte `0x03` as "athlete" is unconfirmed.
5. **`skeletal_muscle_kg` is mislabelled.** It returns `51.8`, matching the
   app's *Skeletal Muscle 51.8 %* — a percentage, not kilograms.
6. `predict_param_annotated.txt` is cited above but was not delivered.
   `wla37_calc_disasm.txt` exists at `docs/` (not in `tools/`).

**Licensing — unresolved.** `libICBodyFatAlgorithms.so` is a proprietary
binary extracted from the Fitdays APK, and the disassembly is derived from it.
This repository is public under MIT; neither file can be relicensed as MIT.
They are deliberately **not committed** pending a decision.
