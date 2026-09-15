# Next steps

Two independent tracks. **(C) is ready to land now and does not depend on (B).**
Read `README.md` first — it has the confirmed protocol. Do not re-derive it.

---

# (B) Crack the checksum, then trigger a live impedance sweep

## Why this is the blocker

The scale only runs its impedance sweep for a **recognised profile**, and a
verbatim replay of the app's FFB1 commands does **not** trigger a fresh one —
it returns the last *stored* composition (see README §5). The prime suspect is
the `B6` frame carrying a stale timestamp. To send a current timestamp we must
be able to compute the checksum byte, and we can't yet.

**Solve the checksum first. Everything else in (B) is blocked behind it.**

## Step B1 — build the corpus

`captures/fitdays-app-weighin.pcapng` contains ~20 app-generated frames with
known-good checksums, in both directions, across 5 frame types and lengths
8/11/12/27/32/34/40. That is a far better dataset than the `A2`-only set the
earlier failed sweep used.

```bash
cd docs/scale-protocol
python3 tools/decode_pcapng.py > /tmp/frames.txt
```

Extract every frame with its final byte as the target. Include the
**app→scale writes** — earlier attempts only used scale→app frames, which is
likely why nothing fit.

## Step B2 — search space

Already ruled out over a **602-frame** corpus (lengths 8/11/12/27/32/34/40,
both directions, incl. app→scale writes). **Do not repeat any of this:**

* `sum` / `xor` / negated-sum over every start offset 0–5 × every end offset,
  with every additive/xor constant 0–255
* CRC-8 over **all 256 polynomials** × init `0x00/0xFF/0x10` × refin × refout
  × xorout `0x00/0xFF/0x10` × start offsets `0,1,3,4,5`

Two other projects have also failed on this checksum (README §8), so treat it
as genuinely hard rather than something a quick sweep will catch.

Try next, in rough order of likelihood:

1. **Non-contiguous / subset sums** — the contiguous space is exhausted. Try
   e.g. payload-only excluding the timestamp, or every-other-byte.
2. **CRC-16 truncated to 8 bits** (take the low or high byte). Not yet tried.
3. **A table-driven vendor checksum** — Lefu firmware is nRF-based; a lookup
   table rather than a polynomial would defeat every sweep run so far.
4. **Decompile the Fitdays APK.** Given two projects and an exhaustive
   numerical search have all failed, static analysis of the app is now the
   *most likely* route to succeed. Pull the APK, decompile (jadx), and search
   for the FFB1 write path and its checksum helper. This is the recommended
   next move, not another brute-force sweep.

Note the one partial rule found: `checksum == payload[0] ^ 0x10` holds for
**every** `B0` frame across both our capture and two other projects' Lefu
handshakes. It fails on `B6` (predicts `0x53`, actual `0x39`), so it is a
short-payload coincidence — but it may hint the real algorithm is
xor-with-a-derived-key rather than a polynomial.

Validate any candidate against **all** frame types and both directions, not
just one type. A rule that fits only `A2` is almost certainly a coincidence.

## Step B3 — if the checksum falls

1. Rebuild the `B6` trigger with a **current** timestamp and correct checksum.
   Also rebuild `C0`/`C1` with a current timestamp — the profile carries one.
2. Run `tools/replay_ffb1.py` with the regenerated frames, standing on the
   scale barefoot.
3. **Success criterion:** an `A7` frame whose `[15:35]` block differs from
   **both** known blocks:
   * `01260c050c550ac20b0300dd0a6b0ad709600998` (old stale)
   * `011b0bf40c520ab90af500da0a5e0ad10959098e` (app's last measurement)

   Comparing against only one of these is how the last run produced a false
   "FRESH BLOCK" — the script flagged success when the scale had merely
   started caching the app's result. **Compare against both.**

## Step B4 — if the checksum does NOT fall

Fall back to capturing more app sessions. Each Fitdays weigh-in yields a fresh
`A7` block plus the app's displayed numbers. With **3+ paired (block, app
numbers) samples** you can:

* fit `[15:35]` against `scales/composition.py` to validate the formulas
* resolve left-vs-right arm assignment by correlating with the app's
  segmental breakdown

Capture procedure is in `../scale-hci-capture-handover.md`. Record the app's
displayed values for **the same weigh-in** every time — that pairing is the
whole point and was missing from every capture so far.

## Known dead ends — do not repeat

* `adb bugreport` / `btsnooz.py` — truncates every ACL payload to ~15 bytes
* `adb pull /data/misc/bluetooth/logs/btsnoop_hci.log` — permission denied on
  stock Android without root. Use the Wireshark **androiddump** socket
* Assuming `124DE8BF` is impedance — it is the device/user ID
* Expecting the phone app and the Pi to be connected simultaneously — the
  scale accepts **one** connection

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
