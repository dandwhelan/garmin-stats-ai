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
* the final byte is a checksum whose algorithm is **still unknown** — see §6

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

### 3.3 `C0` — user profile push (FFB1 write, 32 bytes)

```
01 00 1B 00 C0 | 6AA9A5CD | 003C | 01 | B91C | 16A6 | 1C25 | 1D6A | 0F
   | 124DE8BF | 01 01 03 | 44 61 6E | 28
     ^device id            ^^^^^^^^ "Dan" (ASCII)
```

* `6AA9A5CD` — BE u32 timestamp
* `0xB9` = **185** = height in cm
* `44 61 6E` = **"Dan"** — this is how the scale greets the user by name
* `16A6` → `1BA6` **changed between the two profile pushes in one session**,
  so that field is measurement-dependent, not static config
* age (38 / `0x26`) has **not** been positively located

---

## 4. What is confirmed working

* **No handshake is needed for weight.** Subscribe to FFB2 and send zero
  bytes — the scale streams weight immediately. (The five `ac02…` handshake
  frames in the current `LEFU_DESCRIPTOR` are not used by this unit.)
* A **literal byte replay** of the app's 10 FFB1 writes is *accepted and
  ACKed* by the scale, and makes it emit `A7` composition frames on demand.
  See `tools/replay_ffb1.py`.

## 5. What is NOT working — the open blocker

**The replay does not trigger a new impedance sweep.**

Evidence:

* Across 5 A7 frames spanning 4 hours and two different people, only
  **2 distinct impedance blocks** were ever observed.
* The block stayed byte-identical even when the scale greeted the user by
  name and performed its "extra measurements".
* After the Windows app capture, the replay returned `011B0BF4…` — which is
  **exactly the block the app's own measurement produced**. So the scale
  serves its most recent *stored* composition; weight and timestamp update,
  the impedance does not.

A second user (Helen) confirmed the mechanism: the scale did **not** greet
her, did **not** run the extra measurements, and returned weight only — so
the sweep only runs for a **recognised profile**.

Best hypothesis: the `B6` frame (`04 00 06 00 B6 00 00 03 40 00 39`) is the
sweep trigger, and a verbatim replay is insufficient because it carries a
stale timestamp — which would require a **correct checksum** to update.

## 6. The checksum — unsolved

The final byte of every frame. Ruled out over 144 unique `A2` frames:

* `sum` and `xor` over **every** contiguous byte range, with and without an
  additive/xor constant
* CRC-8 across 8 polynomials (`0x07,0x31,0x1D,0x9B,0x2F,0xD5,0x39,0x49`)
  × init `0x00/0xFF` × refin × refout × xorout

None matched. Note the checksum is **independent of SEQ**: frames with
different SEQ but identical content carry the same final byte.

A useful partial observation on `B0` frames: payload `0x30,0x31,0x39,0x3A`
map to checksums `0x20,0x21,0x29,0x2A` — exactly `payload − 0x10`. That does
not generalise to the longer frames.

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

A previous `btsnooz_hci.log` from `adb bugreport` is **deliberately not kept**:
that format truncates every ACL payload to ~15 bytes and is unusable.

### `tools/`

Standalone scripts; need `bleak` (`pip install bleak`) in a venv.

| Script | Purpose |
|--------|---------|
| `gatt_map.py` | Connect and dump services/characteristics/handles |
| `ble_watch.py` | Auto-reconnecting passive capture of FFB2 + FFB3 |
| `replay_ffb1.py` | Replay the app's 10 FFB1 writes, then log the response |
| `decode_pcapng.py` | Decode the pcapng to ATT PDUs (handle, direction, hex) |

**Gotcha:** the scale only holds a BLE connection while it is *awake*. Connect
attempts against a sleeping scale fail at service discovery. It also accepts
**one connection at a time** — the phone app and the Pi cannot both be
connected, so the app must be fully closed (or phone Bluetooth off) when
capturing from the Pi.
