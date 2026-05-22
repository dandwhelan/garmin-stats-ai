# Contributing

Thanks for the interest! A few honest caveats before you spend time on this:

- **This is a hobby project.** I can't promise I'll review, accept, or fix
  anything in a timely fashion — or at all. If you need a fix urgently, fork
  it.
- **Issues are welcome but unprioritised.** Bug reports with a reproducer are
  much more likely to get attention than feature requests.
- **PRs**: small, focused PRs against `main` are easiest to review. For
  anything bigger than ~50 lines, open an issue first so we can agree on the
  approach before you write the code.

## Reporting a bug

Please include:

- Python version (`python --version`)
- Your Garmin region (US / EU / other) — Garmin's regional endpoints differ
- The exact command you ran
- Relevant log lines (redact email, tokens, and any biometric numbers you'd
  rather not share)
- For data-shape bugs: the date range and which metric

## Development setup

See [CLAUDE.md](CLAUDE.md) for the architecture overview and file map.

```bash
pip install -e garmin-grafana
pip install -e garmin-insights
```

## Code style

Match the surrounding code. No formatter is enforced.
