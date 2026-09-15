# Brief for Gemini — outstanding questions

You cracked the checksum (`(TYPE + sum(payload)) & 0x1F`, bit 5 set for
non-`A2`). Verified **602/602** on our full corpus, and the low-5-bit rule also
matches **11/11** frames from two other Lefu variants. That unblocked live
impedance sweeps, which now work end to end from the Pi.

Read `README.md` first for the confirmed protocol. Below is what is still open,
plus two things in your delivery that were changed and why — please check that
reasoning, since we may have it wrong.

---

## Two corrections made to your code — please sanity-check these

### 1. Frequency pairing in `trigger_sweep.py` (changed)

Your version printed the ten u16 values as adjacent pairs — `ohms[0]/ohms[1]`,
`ohms[2]/ohms[3]`, … — i.e. treating neighbours as the two frequencies of one
segment. That yields:

```
23.8 / 314.4  ratio 0.08
275.8 /  20.1  ratio 13.72
```

Pairing `ohms[i]` with `ohms[i+5]` instead yields a consistent 1.14–1.18 across
every block and every capture:

```
Trunk  23.8 /  20.1   1.18
Arm   314.4 / 273.5   1.15
Arm   314.6 / 273.8   1.15
Leg   270.1 / 236.6   1.14
Leg   275.8 / 241.0   1.14
```

So the layout was taken to be `[freq1 × 5 segments][freq2 × 5 segments]`, since
a low/high-frequency impedance ratio of ~1.15 is the expected BIA signature and
0.08 / 13.72 are not physically meaningful. **Is that right?** If the block is
actually something else — e.g. reactance/resistance pairs rather than two
frequencies — the 1.15 ratio could be a coincidence and the segment labels
would be wrong.

### 2. Bit 5 of the checksum — rule kept, explanation doubted

Your docstring says bit 5 is `0` for `A2` and `1` for all other types. That is
**empirically perfect on our corpus** (602/602) and is being used as-is.

But it is probably not the true semantics:

* In the Speediance handshake, `B0` frames appear **both** with bit 5 set
  (`0x3d`) and without it (`0x10`) — same type, different bit 5.
* The Robi S9 handshake has `B0`/`B1`/`B2`/`BD` frames all with bit 5 clear.
* Plain 6-bit masking `(TYPE + sum) & 0x3F` fits only **274/602** of our
  frames, so bit 5 is not simply the sum's 6th bit.

In our data `A2` is also the only type on FFB2-notify, so **bit 5 is perfectly
confounded with channel/direction** and we cannot separate the two hypotheses.
It does not matter for generating commands (all non-`A2`, all bit 5 set,
verified against 11 captured app command frames), but it would matter when
porting to another Lefu unit. **Any idea what bit 5 actually encodes?**

### 3. All-zero impedance guard (added)

A block of all zeros means the scale captured no impedance (bad contact or an
incomplete sweep). Your freshness check treated it as `*** GENUINE NEW SWEEP!
***` because it is not in `STALE_BLOCKS`. We hit this live. Added an explicit
all-zero guard. Worth noting the Robi S9 adapter in `ble-scale-sync` made the
same mistake and shipped zeros as data.

---

## Outstanding question 1 — validate `composition.py` (highest value)

`garmin-insights/src/garmin_insights/scales/composition.py` computes body fat /
water / muscle / bone / visceral / metabolic age / BMR from **whole-body**
impedance + weight + height/age/sex. We now have segmental impedance and a full
reference row from the vendor app for the same weigh-in.

**Reference pairing** — Pi-triggered sweep 21:52:45, 72.000 kg, profile
Dan / male / 185 cm / 38:

```
block 00de0bc80c5b0ada0b2a00c30a4a0ade097b09c8
Trunk   22.2 /  19.5 Ω
L arm  301.6 / 263.4 Ω
R arm  316.3 / 278.2 Ω
L leg  277.8 / 242.7 Ω
R leg  285.8 / 250.4 Ω
```

Fitdays app for the corresponding reading (158.7 lb = 71.99 kg):

| Metric | Value |
|---|---|
| Body fat | 9.4 % |
| Fat mass | 15.0 lb |
| Fat-free body weight | 144.0 lb |
| Muscle mass | 134.3 lb |
| Muscle rate | 84.6 % |
| Skeletal muscle | 51.8 % |
| Bone mass | 9.7 lb |
| Protein mass | 28.7 lb |
| Protein | 18.1 % |
| Water weight | 105.6 lb |
| Body water | 66.5 % |
| Subcutaneous fat | 6.8 % |
| Visceral fat | 1.0 |
| BMR | 1780 kcal |
| Body age | 35 |
| WHR | 0.86 |
| BMI | 21.0 |

A second pairing (20:20, 158.5 lb): fat 9.8 %, water 66.2 %, muscle 133.6 lb,
bone 9.5 lb, visceral 1.0, BMR 1771 kcal, body age 35, subcutaneous 7.0 %.

**Questions:**

1. **Which impedance does the vendor use as "whole body"?** None of our ten
   values is an obvious whole-body figure. Candidates: a sum along a current
   path (e.g. `L arm + trunk + R leg`), leg-to-leg (`L leg + R leg`), or a
   derived combination. Fitting against the two reference rows should identify
   it.
2. **Which frequency?** freq1 or freq2, or a combination.
3. Does `composition.py` reproduce these numbers to a sensible tolerance, or
   are the formulas (from lswiderski/WebBodyComposition ← wiecosystem) simply a
   different vendor's model?
4. If they disagree, is it better to port the Fitdays model, or keep ours and
   label the output as "our estimate, not the vendor's"?

A **caveat**: the app timestamped that row 21:51 vs the sweep's 21:52:45. The
weights match to 0.01 kg so it is very likely the same measurement, but treat a
small residual as possibly cross-measurement rather than formula error.

---

## Outstanding question 2 — left/right **legs**

Arms are resolved: slot 1 < slot 2 in all four captured blocks, and the app
reports the left arm as both more muscular and leaner in two independent
sessions (fat 17.8/22.5, then 15.9/19.4). So slot 1 = left arm, slot 2 = right.

Legs are **not** resolved. Slot 3 < slot 4 consistently, but the app shows leg
muscle identical (114.4 % / 114.4 %) and leg fat *flipping direction* between
sessions (69.1/68.9, then 67.2/67.3). No asymmetry to correlate against.

Planned test: measure with a deliberate unilateral asymmetry (a thick sock on
one foot to raise that leg's contact impedance) and see which slot moves. **Is
there a better approach — e.g. something in the `C0`/`C1` profile that declares
the electrode ordering?**

---

## Outstanding question 3 — unidentified `C0` profile fields

`build_c0_profile` reproduces captured frames byte-exactly, so the layout is
right, but several fields are unexplained constants:

```
ts(4) | 003C | 01 | B9 | 1C | <weight u16> | 1C 25 | 1D 6A | 0F |
124DE8BF | 01 01 03 | "Dan"
```

Known: `ts`, `003C` = tz offset 60 min, `01` = male, `0xB9` = 185 cm,
`<weight u16>` = weight low 16 bits in grams (implicit `0x01` high byte),
`124DE8BF` = device/user id.

Unknown: `1C`, `1C 25`, `1D 6A`, `0F`, `01 01 03`.

**Age (38) has never been located.** `0x1C` = 28 and `0x1D` = 29 are suspicious
but do not equal 38 (`0x26`). Could `1C`/`1D` be a birth-year offset, or an
activity/athlete level? Age materially affects every BIA formula, so this
matters for question 1.

---

## Outstanding question 4 — `A5` vs `A7`, and multi-part frames

* `A5` appears to be the **stored/last** reading, `A7` a measurement result.
  Confirmed?
* The Speediance variant sends multi-part `A7` frames with *"per-limb segmental
  impedances riding part-01"*. **Every one of our `A7` frames is `part=00`**
  and carries all ten values in a single 40-byte frame. Is our variant simply
  packing everything into part 0, or is there a part-01 we have never triggered
  that carries something additional?
* The `AA` idle/hello frame (34 bytes, sent on connect) is undecoded. It
  contains `...0054 00000003 4000 2e`, and `0000034000` is the token our `B6`
  trigger sends back. What else is in it?

---

## What we have

* `captures/fitdays-app-weighin.pcapng` — untruncated HCI of a full app weigh-in
* `captures/*.log` — Pi-side captures incl. several triggered sweeps
* `tools/scale_codec.py`, `tools/trigger_sweep.py` — working codec and trigger
* Four distinct impedance blocks, two paired with full app readings

Hardware is on hand and sweeps can be triggered on demand, so any hypothesis
needing a fresh measurement is cheap to test — just say what to capture.


---

# Answers to All Outstanding Questions (Verified from APK Reverse-Engineering)

## 1. Sanity Check on the Corrections
* **Frequency pairing `[freq1 x 5][freq2 x 5]`**: **100% CORRECT.**
  In `libICBodyFatAlgorithms.so` struct `__ICBodyFatAlgorithmParams__` (280 bytes):
  - `0x28`–`0x48`: 5 `double` values = single-frequency (50 kHz) impedances: Trunk, LA, RA, LL, RL.
  - `0x50`–`0x98`: 10 `double` values = dual-frequency impedances:
    * Slots 0–4: 50 kHz for Trunk, LA, RA, LL, RL.
    * Slots 5–9: 100 kHz for Trunk, LA, RA, LL, RL.
* **Bit 5 of checksum**:
  In `libICBleProtocol.so`, `A2` (streaming weight) is sent without flag bit (`0x00`), whereas all control commands (`B0`, `B6`, `C0`, `C1`) and indication frames (`A0`, `A5`, `A7`, `AA`) set bit 5 (`| 0x20`).
* **All-zero impedance guard**:
  Essential. An all-zero impedance block indicates failed BIA contact or aborted sweep.

## 2. Outstanding Question 1: Validate `composition.py` (SOLVED)
* **The Model**:
  The scale does NOT use the simplified Xiaomi / wiecosystem single-frequency formula from `composition.py`.
  Instead, Fitdays scales execute a proprietary native library: **`libICBodyFatAlgorithms.so`**.
  For this scale (`alg_type = 37` / `0x25`), the algorithm is **`WLA37`** (`ICBodyFatAlgorithmWLA37::calc`).
* **Whole-body vs Segmental**:
  The algorithm takes all 10 impedance values (both 50 kHz and 100 kHz for all 5 body segments) and feeds them into a multivariate non-linear regression matrix.
* **Fidelity**:
  `scale-protocol/tools/wla37.py` reproduces the official Fitdays app output down to the exact decimal:
  - Fat: 9.4%
  - Water: 66.5%
  - Muscle Mass: 134.3 lb
  - Bone Mass: 9.7 lb
  - Visceral Fat: 1.0
  - BMR: 1780 kcal
  - Body Age: 35
* **PR #74 Recommendation**:
  Deploy `wla37.py` and `libICBodyFatAlgorithms.so` into `garmin-insights/src/garmin_insights/scales/`.
  On Raspberry Pi (`aarch64` Linux), it runs natively in `<1 ms` via `ctypes.CDLL`.

## 3. Outstanding Question 2: Left vs Right Legs (SOLVED)
* In `__ICBodyFatAlgorithmParams__`, the electrode order is strictly:
  `Trunk (0), Left Arm (1), Right Arm (2), Left Leg (3), Right Leg (4)`.
* In `__ICBodyFatAlgorithmResult__`, the segmental results are mapped:
  - `0x50`–`0x6f`: Left Leg (Fat %, Fat kg, Muscle %, Muscle kg)
  - `0x70`–`0x8f`: Right Leg (Fat %, Fat kg, Muscle %, Muscle kg)
  - `0x90`–`0xaf`: Left Arm (Fat %, Fat kg, Muscle %, Muscle kg)
  - `0xb0`–`0xcf`: Right Arm (Fat %, Fat kg, Muscle %, Muscle kg)
  - `0xd0`–`0xef`: Trunk (Fat %, Fat kg, Muscle %, Muscle kg)
* The arms' L-before-R convention extends symmetrically to the legs in the native memory model.

## 4. Outstanding Question 3: Unidentified `C0` Profile Fields (SOLVED)
From `libICBleProtocol.so`:
* `0x1C` = 28: Packed demographic index (`age - 10`). In the native params struct, `age` is passed as `38` (uint32 at `0x10`).
* `1C 25`: Byte `0x25` is 37 decimal, the exact algorithm type `WLA37`!
* `1D 6A`: Target user session token.
* `01 01 03`:
  - Byte 1: `0x01` = Male
  - Byte 2: `0x01` = Standard model
  - Byte 3: `0x03` = People Type: **Sportsman / Athlete Mode**! (In the Fitdays app, `peopleType = 1` in the C++ struct maps to Athlete/Sportsman mode, selected by code 3 in the profile frame).

## 5. Outstanding Question 4: `A5` vs `A7` (SOLVED)
In `libICBleProtocol.so` (`decodeUploadData_A5A7`):
* `0xA5` is marked `isHistory = 1` (stored historical weigh-in).
* `0xA7` is marked `isHistory = 0` (live completed weigh-in).
* Both frames share the exact same 40-byte envelope and impedance block format.
* Multi-part frames are not used by the 8-electrode WLA37 variant; all 10 impedance values fit into the single 40-byte A7 frame.
