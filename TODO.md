# TODO

## Scale: Fitdays target weight / weight control

Reproduce Fitdays' "Recommended target weight", "Weight control", "Fat control"
and "Muscle control" in the scan readout (`scales/standards.py`). Formula not yet
identified — need a third person's (or a different-weight) Fitdays report to fit it.

Samples so far (Fitdays app, 2026-09-28):

| | Dan (M) | Helen (F) |
|---|---|---|
| Height (from BMI) | ~185 cm | ~175 cm |
| Weight | 156.6 lb / 71.05 kg | 133.9 lb / 60.75 kg |
| Body fat | 7.4 % | 18.8 % |
| Fat mass | 11.7 lb | 25.1 lb |
| Fat-free mass | 144.9 lb / 65.79 kg | 108.5 lb / 49.21 kg |
| Muscle mass | 135.2 lb | 101.2 lb |
| Ideal body weight | 166.0 lb | 139.1 lb |
| Recommended target weight | 170.0 lb | 142.0 lb |
| Weight control | +13.2 lb | +7.9 lb |
| Fat control | +13.2 lb | +7.5 lb |
| Muscle control | 0.0 lb | +0.4 lb |
| WHR | 0.81 | 0.80 |

Observations: ideal weight = BMI 22 (men) / (h−70)×0.6 (women) — already
implemented. Target weight implies a target body fat of ~14.7 % (Dan) and
~23.6 % (Helen) if fat-free mass is held constant; weight control isn't
simply target − current (Dan: 170.0 − 156.6 = 13.4, shown 13.2).

## Scale: WHR

Find WHR's offset in the WLA37 output: on the Pi run
`.venv/bin/python scripts/wla37_dump.py --user dan --find 0.81` (Helen: `--find 0.80`),
then add the offset to `_decode` in `scales/wla37.py`.
