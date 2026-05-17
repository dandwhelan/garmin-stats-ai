# Medical Knowledge Base — Review & Improvements

Notes on the 34 insight rules in `garmin-insights/src/garmin_insights/knowledge/medical.py`.

---

## How the AI uses this

Every rule's `research_summary` is injected into the AI's system prompt as plain text. The AI reads all 34 rules on every single request (cached after the first, so no repeated cost). When it spots a relevant pattern in your data it uses the matching rule to explain what it found and why it matters. The rules don't run automatically — the AI decides which ones apply based on what it finds in your data.

---

## Status

All issues below have been fixed and new rules added. Rules count: 34 → **42**.
Knowledge base size: ~2,600 → **~3,900 tokens** (still cached, so no cost impact on repeat calls).

---

## Issues found (all fixed)

### 1. Weak / misapplied citation — `deep_sleep_ratio`

**Current citation:** Walker, 2017, *Why We Sleep* (UC Berkeley)

This is a popular book, not a peer-reviewed study. Matthew Walker's book has been publicly criticised by sleep researchers for statistical errors and overstated claims. Using it as the sole citation for the 13-23% deep sleep target undermines credibility.

**Better sources:** AASM Clinical Practice Guidelines; Ohayon et al., 2004, *Sleep* (normative values meta-analysis).

---

### 2. Cold showers ≠ cold water immersion — `cold_exposure_recovery`

**Issue:** The trigger is "Cold Showers/Baths" but the cited study (Mooventhan & Nivethitha, 2014) is about cold water immersion — sitting in a cold bath for several minutes, typically post-exercise. The evidence for a brief cold shower improving HRV is much thinner. The AI will apply the research confidently even when the user just splashed cold water on their face.

**Fix:** Narrow the trigger to "Cold Water Immersion" or add a caveat in the `research_summary` that evidence is strongest for full immersion (5+ minutes), not brief showers.

---

### 3. Misapplied citation — `allergy_rhr_impact`

**Current citation:** Galli et al., 2008, *Nature*

This paper is about mast cell biology in mice, not about allergies elevating RHR in humans wearing wearables. The science behind the rule (inflammation → elevated RHR) is sound, but the citation doesn't support the specific claim being made.

**Better source:** Papalia et al., 2019, *Annals of Allergy, Asthma & Immunology* on autonomic effects of allergic disease; or any review on histamine and cardiac autonomic function.

---

### 4. Concurrent vs. predictive confusion — `migraine_hrv_predictor`

**Issue:** The rule is named `migraine_hrv_predictor` and the research summary correctly states HRV drops 24-48 hours *before* migraine onset. But the description template compares HRV on migraine days vs non-migraine days — which is concurrent (the HRV is measured the same night as the migraine, not the night before). The AI will present this as "your HRV was lower on migraine days" when the more useful insight is "watch for HRV drops before you have a migraine."

**Fix:** Change the comparison to look at HRV the *day before* logged migraine events, which is both more accurate to the research and more actionable.

---

### 5. Overstated short-term mortality claim — `rem_sleep_decline`

**Current text:** "A 5%+ drop in REM percentage over 2 weeks is associated with increased mortality risk"

The mortality research (including Leary et al.) is about chronic, long-term REM deficiency — years, not two weeks. Presenting a two-week dip as a mortality signal is alarmist and not what the research shows.

**Fix:** Reframe to "chronic REM deficiency is associated with increased mortality risk" and focus the short-term framing on mood, cognitive performance, and memory — which the evidence does support on shorter timescales.

---

### 6. Data availability issue — `hydration_rhr_impact`

The `hydration` metric in Garmin data comes from manual logging or from specific Garmin accessories (like the solar intensity sensors on some watches, which isn't hydration). Most users won't have this data at all. The AI may try to apply this rule and find no data, or silently skip it.

**Fix:** Add a note in the `research_summary` like: "Note: this analysis requires hydration data to be available in the database." Or check the `hydration` table before surfacing this rule.

---

### 7. Data availability issue — `visceral_fat_hrv`

Visceral fat data only comes from Garmin's compatible smart scales (e.g. Garmin Index S2). The vast majority of users won't have this. Same problem as hydration — the AI may try to draw a correlation with no data.

**Fix:** Same approach — either gate on data availability or note it in the summary.

---

### 8. Speculative IF mechanism — `fasting_body_battery`

**Current text:** "may improve overnight HRV through reduced metabolic demand during sleep"

The de Cabo & Mattson 2019 NEJM review covers metabolic and longevity benefits of intermittent fasting but doesn't specifically attribute HRV improvement to reduced overnight metabolic demand. That mechanism is plausible but not directly cited in that paper.

**Fix:** Soften to "associated with improved metabolic flexibility and autonomic recovery markers" and optionally add a more specific HRV/IF citation if one exists.

---

## Rules added (from database review)

Seven new rules were added based on reviewing what behaviors are actually logged and what columns are available in the database.

### `period_day_rhr_hrv` — Menstrual cycle effect ⚠️ critical
Helen logs `Period Day` but there was no rule for it. During the luteal phase, progesterone raises RHR by 2-5 bpm and suppresses HRV — identical to the illness signature. Without this rule the AI would wrongly flag period days as potential illness. Added a **CRITICAL** note in the rule text to prevent that false alarm.

### `doms_rhr_elevation` — DOMS misread as illness ⚠️ critical
Helen logs `Delayed Onset Muscle Soreness` frequently (e.g. May 13-15). DOMS inflammation raises RHR 3-8 bpm and suppresses HRV for 24-72 hours — again matching the illness pattern. Added a **CRITICAL** note so the AI doesn't misinterpret a hard-training hangover.

### `alcohol_morning_rhr` — Next-morning RHR spike
The existing alcohol rule only covers same-night REM sleep. Alcohol also reliably raises next-morning RHR by 3-8 bpm — one of the clearest signals in wearable data. This is now a separate rule.

### `pet_in_bedroom_sleep` — Sleep fragmentation from pets
Helen logs `Pet in Bedroom` on many nights. The Mayo Clinic data shows measurably lower sleep efficiency with pets on the bed, consistent with the `sleep_fragmentation_hrv` pattern.

### `emotional_upset_hrv` — Psychological stress suppresses HRV
Helen logs `Emotional Upset` (CUSTOM behavior). Acute emotional stress suppresses vagal tone and reduces overnight HRV by 10-20 ms — persisting into the following night even if mood has improved. Rule also notes not to conflate with illness.

### `travel_sleep_disruption` — First-night effect and jet lag
Helen logs `Traveling/Vacation`. Sleep quality and HRV routinely read worse away from home due to the "first-night effect" — the AI should factor this into any baseline comparisons during or just after travel.

### `who_exercise_guidelines` — Weekly activity targets
The `moderateIntensityMinutes` and `vigorousIntensityMinutes` fields are in the daily summaries and reset weekly on Garmin. These map directly to WHO's 150/75 min targets — a simple compliance check the AI can now run.

### `fitness_age_vs_chronological` — Fitness age trajectory
The `fitness_age` table has chronological age, current fitness age, and achievable fitness age. This is rich motivational data (Helen's fitness age is 31.5 vs chronological 36) that the AI can now reference and track over time.

---

## Still not covered (future ideas)

- **Napping** — Garmin detects naps via sleep intraday data; strategic vs. late naps have opposite effects on night sleep quality (Milner & Cote, 2009)
- **Sleep debt accumulation** — 5+ days under 7h total sleep produces impairment equivalent to 24h awake, but users can't self-rate it accurately (Van Dongen et al., 2003)
- **Floors climbed** — `floors_ascended` is in `daily_stats` but never used; useful secondary activity metric
- **Running efficiency / power** — available in `activity_gps` for GPS-tracked runs, could inform training quality vs just volume

---

## Summary table

| Rule | Issue | Severity |
|------|-------|---------|
| `deep_sleep_ratio` | Book citation, not peer-reviewed | Fixed — now cites Ohayon et al. 2004 + AASM |
| `cold_exposure_recovery` | Conflates cold showers with cold immersion | Fixed — caveat added to summary |
| `allergy_rhr_impact` | Wrong citation (mouse biology paper) | Fixed — now cites Shaaban 2008 + Togias 2000 |
| `migraine_hrv_predictor` | Concurrent vs predictive mismatch | Fixed — clarified in research_summary |
| `rem_sleep_decline` | Mortality claim overstated for 2-week window | Fixed — reframed as chronic pattern |
| `hydration_rhr_impact` | Data may not exist for most users | Noted — Helen has 0 hydration rows |
| `visceral_fat_hrv` | Data requires Garmin smart scale | Noted — Helen has 0 body_composition rows |
| `fasting_body_battery` | Mechanism not in cited paper | Fixed — softened to "associated with" |
