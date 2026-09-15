# Next steps

Two independent tracks. **(C) is ready to land now and does not depend on (B).**
Read `README.md` first — it has the confirmed protocol. Do not re-derive it.

---

# (B) Trigger a live impedance sweep [CHECKSUM SOLVED]

## Status: Checksum cracked and verified (100.000% over 814 frames)

The checksum is a 6-bit value (`0x00`–`0x3F`):
```python
def compute_checksum(ftype: int, payload: bytes) -> int:
    ck_low = (ftype + sum(payload)) & 0x1F
    return ck_low if (ftype == 0xA2) else (ck_low | 0x20)
```

Both `scale_codec.py` and `trigger_sweep.py` are implemented in `tools/`.

## Key insights from timeline analysis of `capture.pcapng`

1. **User must be standing barefoot on the scale BEFORE profile push:**
   The scale only matches the user and arms the impedance sweep if live weight on the platform (~72 kg) matches the expected weight in `C0`.
2. **`B6` echoes the capability token from `AA`:**
   Scale advertises `...54 000000034000 2e` in `AA`. The app sends `04000600b6 0000034000 39` to arm the sweep.
3. **Weight settles (`STATUS = 0x03`):**
   Scale displays "Dan", settles weight, sweeps dual-frequency impedance across 8 electrodes, and emits `A7` indication on FFB3.
4. **Finalization:**
   App acknowledges with `B0 3A` (`0a000300b03a002a`) and updates stored weight in `C0`/`C1`.

## How to run the live sweep test

```bash
# In scale-protocol directory (with bleak installed in python env):
python tools/trigger_sweep.py
```

Follow the prompts:
1. Ensure Fitdays app is force-stopped / Phone Bluetooth is off.
2. Step barefoot onto the scale to wake it up.
3. Stand still as it connects, streams live weight, triggers the BIA sweep, and prints the fresh `A7` block!

Success criterion: An `A7` frame whose `[15:35]` block differs from both:
- `01260c050c550ac20b0300dd0a6b0ad709600998` (old stale)
- `011b0bf40c520ab90af500da0a5e0ad10959098e` (app weigh-in)

---

# (C) Land the weight-decode fix

Self-contained, verified, and independent of (B). This is a genuine bug fix:
the current code produces impossible weights.

## Target

Branch `claude/bluetooth-scales-garmin-sync-ooha7t` (draft PR #74), file
`garmin-insights/src/garmin_insights/scales/lefu.py`.

**The branch is well behind `main`** — check whether to rebase it or open a
fresh branch off `main` before starting.

## C1 — fix the weight decode (the actual bug)

Current `_decode_a2` uses:

```python
_A2_WEIGHT_SCALE = 100.0
weight_kg = int.from_bytes(frame[8:10], "big") / _A2_WEIGHT_SCALE
```

This drops the high byte. Correct:

```python
# weight is a BE u24 at frame[7:10], in grams
weight_kg = int.from_bytes(frame[7:10], "big") / 1000.0
```

Note the byte currently named `FLAG` in the `_A2_HEADER` comment is actually
the **weight's high byte** — fix the docstring layout too:

```
SEQ 00 07 00 A2 STATUS 00 [W_HI W_MID W_LO] ?? CK
```

Status semantics are confirmed and unchanged: `0x01` live, `0x03` settled,
`0x00` final.

**Regression test (use real captured frames):**

| frame (hex) | expected |
|---|---|
| `68000700a20100011ad0000e` | 72.400 kg, live |
| `86000700a20300011a9e001e` | 72.350 kg, stable |
| `23000700a20100000384000a` | 0.900 kg, live |
| `24000700a2010000c3e6000c` | 50.150 kg, live |

Assert no frame in `captures/passive_capture.log` decodes above 200 kg — the
old code yielded 501.50 kg.

## C2 — subscribe to FFB3

Composition frames arrive on **FFB3 (indicate)**, which the adapter never
subscribes to. `AdapterDescriptor` has no notion of an indicate channel — add
one (e.g. `indicate_uuid`) and have the browser/BLE client subscribe to it
alongside the notify channel.

FFB3 UUID: `0000ffb3-0000-1000-8000-00805f9b34fb`

## C3 — fix the impedance frame gate

`_decode_impedance` currently gates on:

```python
len(frame) == 40 and frame[0] == 0xFF
```

`frame[0]` is the **SEQ** byte, so this never matches. The real test is
`frame[4] in (0xA5, 0xA7)`. Replace the brute-force "first plausible u16
anywhere in the frame" scan with the known offsets:

* weight: BE u24 `[10:13]` / 1000
* timestamp: BE u32 `[5:9]`
* impedance block: ten BE u16 at `[15:35]`, `/10` = ohms,
  order `[trunk, arm, arm, leg, leg]` × 2 frequencies
* `[35:39]` is the device ID — **never** treat as impedance

## C4 — be honest about what is stored

Until (B) lands, the impedance block may be a **stale cached reading** rather
than a live measurement. Do **not** feed it into `composition.py` and present
body-fat numbers as current.

Store the raw block in `ScaleReading.extras` (e.g. `impedance_block_hex`,
plus the decoded per-segment ohms) and leave the composition fields `None`.
A stale-but-labelled reading is fine; a fabricated body-fat percentage is not
— an earlier decode of this frame produced 0.4 % body fat.

Consider carrying the frame's own timestamp `[5:9]` through, so a stale block
is detectable: if it predates the weigh-in, the composition is not current.

## C5 — optional cleanup

The five `ac02…` handshake frames and the 1 Hz poll in `LEFU_DESCRIPTOR` are
not used by this unit — weight streams with zero bytes written. Leave them if
other Lefu variants may need them, but they are dead weight for `JEETIF2421`.
