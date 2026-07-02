"""Medical evidence knowledge base — structured insight rules with research citations.

These rules is injected into the LLM system prompt and also used by the
proactive insight scanner to detect and explain patterns.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class InsightRule:
    """A single evidence-backed health insight rule."""
    name: str
    category: str  # sleep, stress, exercise, lifestyle, body_comp, recovery
    trigger_behavior: str | None  # Lifestyle behavior that triggers this rule (or None)
    trigger_metric: str  # Primary metric to check
    comparison_metric: str | None  # Secondary metric to correlate with
    direction: str  # "higher_is_worse", "lower_is_worse", "correlation"
    description_template: str  # Template with {placeholders}
    research_citation: str
    research_summary: str
    # Evidence-tier metadata. Defaults keep existing call sites working.
    # A = meta-analysis / guideline; B = wearable-validated, context-dependent;
    # C = plausible but mixed evidence; D = reserved (experimental / preprint /
    # company source — no rules currently use D; preprints have been pruned in
    # favour of peer-reviewed alternatives).
    evidence_tier: str = "B"
    # causal | strong_association | weak_association | hypothesis
    claim_strength: str = "strong_association"
    # high | medium | low — how trustworthy is the Garmin signal itself
    measurement_confidence: str = "high"
    # Other plausible explanations the agent should consider before naming a cause.
    confounders: list[str] = field(default_factory=list)
    # True when rule only fires if user has logged the trigger behavior / context.
    requires_user_context: bool = False
    # "female" when the rule only applies to menstruating users (cycle-phase
    # physiology). None = applies to everyone. Filtered out of the LLM summary
    # for non-female users so a male persona never receives cycle rules.
    sex_specific: str | None = None


# ---------------------------------------------------------------------------
# All rules — the LLM receives these as context
# ---------------------------------------------------------------------------
INSIGHT_RULES: list[InsightRule] = [
    # ===== SLEEP & CAFFEINE =====
    InsightRule(
        name="late_caffeine_sleep",
        category="sleep",
        trigger_behavior="Late Caffeine",
        trigger_metric="sleepScore",
        comparison_metric=None,
        direction="higher_is_worse",
        description_template=(
            "On days with late caffeine, your sleep score averages {mean_with:.0f} "
            "vs {mean_without:.0f} without — a {difference:+.0f} point difference."
        ),
        research_citation=(
            "Drake et al., 2013, J Clin Sleep Med; "
            "Gardiner et al., 2023, Sleep Medicine Reviews (meta-analysis)"
        ),
        research_summary=(
            "Caffeine has a half-life of 5-7 hours. Consuming caffeine within 6 hours "
            "of bedtime reduces total sleep time by approximately 1 hour and significantly "
            "impairs sleep quality. The 2023 meta-analysis confirms this across multiple "
            "studies. Sensitivity varies — compare against the user's own baseline."
        ),
        evidence_tier="A",
        claim_strength="causal",
        confounders=["alcohol", "stress", "screen_time"],
    ),
    InsightRule(
        name="morning_caffeine_dose",
        category="lifestyle",
        trigger_behavior="Morning Caffeine",
        trigger_metric="stressPercentage",
        comparison_metric=None,
        direction="correlation",
        description_template=(
            "Your morning caffeine intake (avg {mean_with:.1f} cups) correlates with "
            "a stress percentage of {metric_mean:.1f}%."
        ),
        research_citation="Lovallo et al., 2005, Psychosomatic Medicine",
        research_summary=(
            "Caffeine increases cortisol secretion in a dose-dependent manner. "
            "Regular consumers develop partial tolerance but still show elevated "
            "cortisol response to caffeine. Note that Garmin 'stress' is HRV-derived "
            "autonomic strain, not a validated measure of mental stress."
        ),
        evidence_tier="A",
        confounders=["sleep_debt", "emotional_stress", "exercise"],
    ),

    # ===== SLEEP & ALCOHOL =====
    InsightRule(
        name="alcohol_rem_sleep",
        category="sleep",
        trigger_behavior="Alcohol",
        trigger_metric="remSleepSeconds",
        comparison_metric="sleepScore",
        direction="higher_is_worse",
        description_template=(
            "Alcohol nights show {pct_change:+.0f}% REM sleep change "
            "(avg {mean_with:.0f}s vs {mean_without:.0f}s without alcohol)."
        ),
        research_citation=(
            "Ebrahim et al., 2013, Alcoholism: Clin & Exp Res; "
            "PLOS Digital Health, 2026 (~21k-adult wearable cohort)"
        ),
        research_summary=(
            "Alcohol suppresses REM sleep in the first half of the night and causes "
            "rebound sleep fragmentation in the second half. Even moderate consumption "
            "(1-2 drinks) reduces REM by 9-17%. The 2026 wearable cohort confirms "
            "dose-dependent reductions in sleep duration and HRV with alcohol intake."
        ),
        evidence_tier="A",
        claim_strength="causal",
        confounders=["illness", "heat", "late_exercise", "stress", "luteal_phase"],
    ),

    # ===== SCREENS & SLEEP =====
    InsightRule(
        name="screens_sleep_quality",
        category="sleep",
        trigger_behavior="Screens Before Bed",
        trigger_metric="sleepScore",
        comparison_metric="avgSleepStress",
        direction="higher_is_worse",
        description_template=(
            "Screen use before bed correlates with a {difference:+.0f} point "
            "sleep score change."
        ),
        research_citation="Chang et al., 2015, Proceedings of the National Academy of Sciences",
        research_summary=(
            "Blue light from screens suppresses melatonin production by up to 50%, "
            "delays circadian rhythm, and reduces subjective sleepiness. "
            "Effects persist even with night-mode filters."
        ),
        confounders=["late_caffeine", "stress", "late_meals"],
    ),

    # ===== HRV TRENDS =====
    InsightRule(
        name="hrv_declining_trend",
        category="recovery",
        trigger_behavior=None,
        trigger_metric="avgOvernightHrv",
        comparison_metric=None,
        direction="lower_is_worse",
        description_template=(
            "Your overnight HRV has been {direction} over the last {days_analyzed} days "
            "(slope: {slope_per_day:+.1f} ms/day, R²={r_squared:.2f})."
        ),
        research_citation="Plews et al., 2013, International Journal of Sports Physiology & Performance",
        research_summary=(
            "A declining HRV trend over 5+ days indicates accumulated physiological strain "
            "and incomplete recovery. Athletes showing this pattern are at higher risk "
            "of non-functional overreaching and illness. Requires ≥21 days of HRV baseline "
            "for the trend slope to be reliable; below that, treat as noise."
        ),
        confounders=["alcohol", "illness", "luteal_phase", "travel", "poor_sleep", "heat"],
    ),

    # ===== RHR ELEVATION =====
    InsightRule(
        name="rhr_elevated",
        category="stress",
        trigger_behavior=None,
        trigger_metric="restingHeartRate",
        comparison_metric=None,
        direction="higher_is_worse",
        description_template=(
            "Your resting heart rate ({value:.0f} bpm) is {z_score:.1f}σ above "
            "your 30-day baseline ({baseline_mean:.0f} bpm)."
        ),
        research_citation=(
            "Radin et al., 2020, Lancet Digital Health; "
            "Aune et al., 2017, Nutr Metab Cardiovasc Dis (dose-response meta-analysis on RHR and mortality)"
        ),
        research_summary=(
            "Elevated resting heart rate (>5 bpm above personal baseline for 3+ days) "
            "is associated with infection, illness, or overtraining-like strain. "
            "Wearable-detected RHR elevations preceded COVID-19 symptom onset by 1-4 days. "
            "The 2017 CMAJ meta-analysis links higher RHR to all-cause and CV mortality. "
            "Same-day causes also include alcohol, late training, heat, poor sleep, and "
            "luteal-phase physiology — treat as a deviation signal, not a diagnosis."
        ),
        evidence_tier="A",
        confounders=["alcohol", "illness", "late_exercise", "heat", "poor_sleep", "luteal_phase", "stress"],
    ),

    # ===== STRESS =====
    InsightRule(
        name="high_stress_day",
        category="stress",
        trigger_behavior=None,
        trigger_metric="highStressPercentage",
        comparison_metric="bodyBatteryDrainedValue",
        direction="higher_is_worse",
        description_template=(
            "High stress time at {value:.0f}% today — above your baseline of {baseline_mean:.0f}%."
        ),
        research_citation=(
            "Adam et al., 2017, Psychoneuroendocrinology; "
            "Garmin HRV-based stress scoring documentation"
        ),
        research_summary=(
            "Garmin's stress score is HRV-derived physiological/autonomic strain, NOT a "
            "validated measure of mental stress. Days with >30% high stress show reduced "
            "overnight recovery, but the underlying cause can be alcohol, illness, heat, "
            "pain, caffeine, late exercise, dehydration, or emotional stress. Chronic "
            "high cortisol does impair sleep, immunity, and cognition — but link a high "
            "score to context (logged behaviours, cycle phase) before naming a cause."
        ),
        confounders=["alcohol", "illness", "heat", "pain", "caffeine", "late_exercise", "emotional_stress", "dehydration"],
    ),

    # ===== EXERCISE =====
    InsightRule(
        name="exercise_sleep_benefit",
        category="exercise",
        trigger_behavior="Moderate Exercise",
        trigger_metric="sleepScore",
        comparison_metric="avgOvernightHrv",
        direction="correlation",
        description_template=(
            "Exercise days show a {difference:+.0f} point sleep score difference "
            "({mean_with:.0f} vs {mean_without:.0f})."
        ),
        research_citation="Kredlow et al., 2015, Journal of Behavioral Medicine (meta-analysis)",
        research_summary=(
            "Regular moderate exercise improves sleep quality, reduces sleep onset latency, "
            "and increases total sleep time. Effects are most pronounced with consistent "
            "exercise 4-6 hours before bedtime."
        ),
        evidence_tier="A",
    ),
    InsightRule(
        name="late_exercise_sleep",
        category="exercise",
        trigger_behavior="Vigorous Exercise Before Bed",
        trigger_metric="sleepScore",
        comparison_metric=None,
        direction="higher_is_worse",
        description_template=(
            "Vigorous exercise before bed nights show a {difference:+.0f} point "
            "sleep score change."
        ),
        research_citation=(
            "Stutz et al., 2019, Sports Medicine (review); "
            "Leota et al., 2025, Nature Communications (~4M nights of wearable data)"
        ),
        research_summary=(
            "Vigorous exercise within 4 hours of bedtime is associated with delayed sleep "
            "onset, shorter sleep, higher overnight RHR, and lower HRV (2025 large-scale "
            "wearable cohort). Easy evening activity may be fine; the risk signal is "
            "strongest for strenuous sessions close to bedtime. Mechanism: elevated core "
            "body temperature and sympathetic activation."
        ),
        evidence_tier="A",
        confounders=["alcohol", "caffeine", "heat", "stress"],
    ),

    # ===== COLD EXPOSURE =====
    InsightRule(
        name="cold_exposure_recovery",
        category="lifestyle",
        trigger_behavior="Cold Showers/Baths",
        trigger_metric="bodyBatteryChange",
        comparison_metric="avgOvernightHrv",
        direction="correlation",
        description_template=(
            "Cold shower days show {pct_change:+.0f}% difference in overnight "
            "body battery recovery ({mean_with:.0f} vs {mean_without:.0f})."
        ),
        research_citation="Mooventhan & Nivethitha, 2014, North American Journal of Medical Sciences",
        research_summary=(
            "Cold water immersion may activate the parasympathetic nervous system and "
            "transiently raise vagal tone, but the evidence base is broad hydrotherapy "
            "data — not enough to make strong personalised HRV claims. Individual "
            "response varies widely. Brief cold showers show much weaker effects than "
            "full 5+ minute immersion. Use as a personal tracking hypothesis, not a rule."
        ),
        evidence_tier="C",
        claim_strength="weak_association",
        requires_user_context=True,
        confounders=["exercise", "stress", "sleep"],
    ),

    # ===== SUNLIGHT =====
    InsightRule(
        name="sunlight_stress_reduction",
        category="lifestyle",
        trigger_behavior="Sunlight",
        trigger_metric="stressPercentage",
        comparison_metric=None,
        direction="correlation",
        description_template=(
            "Sunlight exposure days show {pct_change:+.0f}% lower stress "
            "({mean_with:.0f}% vs {mean_without:.0f}%)."
        ),
        research_citation="Figueiro et al., 2017, Sleep Health",
        research_summary=(
            "Morning sunlight exposure (especially within 2 hours of waking) helps "
            "regulate the circadian rhythm, suppresses melatonin at the right time, "
            "and normalises cortisol patterns. The link to a Garmin 'stress' score "
            "specifically is indirect; treat as a lifestyle hypothesis."
        ),
        evidence_tier="C",
        claim_strength="weak_association",
        requires_user_context=True,
    ),

    # ===== ALLERGIES =====
    InsightRule(
        name="allergy_rhr_impact",
        category="lifestyle",
        trigger_behavior="Allergy Symptoms",
        trigger_metric="restingHeartRate",
        comparison_metric="stressPercentage",
        direction="higher_is_worse",
        description_template=(
            "Allergy symptom days show RHR of {mean_with:.0f} bpm "
            "vs {mean_without:.0f} bpm without ({difference:+.1f} bpm)."
        ),
        research_citation="Shaaban et al., 2008, The Lancet; Togias, 2000, Journal of Allergy and Clinical Immunology",
        research_summary=(
            "Allergic inflammation can elevate RHR and impair sleep via histamine-mediated "
            "arousal and nasal congestion. Garmin signals alone cannot distinguish allergy "
            "from infection, poor sleep, alcohol, heat, or asthma — this rule should only "
            "fire when the user has logged concurrent congestion / hay-fever symptoms."
        ),
        evidence_tier="C",
        claim_strength="weak_association",
        requires_user_context=True,
        confounders=["illness", "poor_sleep", "alcohol", "heat", "asthma"],
    ),

    # ===== MIGRAINES =====
    InsightRule(
        name="migraine_hrv_predictor",
        category="lifestyle",
        trigger_behavior="Migraines",
        trigger_metric="avgOvernightHrv",
        comparison_metric="stressPercentage",
        direction="lower_is_worse",
        description_template=(
            "Migraine days show overnight HRV of {mean_with:.0f} ms "
            "vs {mean_without:.0f} ms on migraine-free days."
        ),
        research_citation="Miglis, 2018, Current Pain & Headache Reports (review — autonomic findings in migraine are significant but conflicting)",
        research_summary=(
            "The migraine/autonomic literature is mixed: some studies find HRV drops 24-48h "
            "before onset, others do not. Treat as a personal-pattern rule only — fires "
            "when the user has logged ≥3 migraine episodes coinciding with this HRV/RHR "
            "pattern. Do NOT use as a general migraine predictor."
        ),
        evidence_tier="C",
        claim_strength="hypothesis",
        requires_user_context=True,
        confounders=["sleep_loss", "stress", "alcohol", "luteal_phase", "dehydration"],
    ),

    # ===== STRETCHING =====
    InsightRule(
        name="stretching_recovery",
        category="lifestyle",
        trigger_behavior="Stretching",
        trigger_metric="bodyBatteryChange",
        comparison_metric="stressPercentage",
        direction="correlation",
        description_template=(
            "Stretching days show {pct_change:+.0f}% difference in overnight "
            "body battery recovery."
        ),
        research_citation="Corey et al., 2012, PM&R Journal",
        research_summary=(
            "Regular stretching may activate the parasympathetic nervous system and reduce "
            "muscle tension, with possible knock-on effects on sleep and recovery. Effect "
            "sizes on Garmin daily recovery markers are individual; treat as a lifestyle "
            "hypothesis."
        ),
        evidence_tier="C",
        claim_strength="weak_association",
        requires_user_context=True,
    ),

    # ===== MEAL QUALITY =====
    InsightRule(
        name="healthy_meals_energy",
        category="lifestyle",
        trigger_behavior="Healthy Meals",
        trigger_metric="bodyBatteryHighestValue",
        comparison_metric="stressPercentage",
        direction="correlation",
        description_template=(
            "Healthy meal days show peak body battery of {mean_with:.0f} "
            "vs {mean_without:.0f} on other days."
        ),
        research_citation="Haghighatdoost et al., 2012, Public Health Nutrition",
        research_summary=(
            "Dietary quality affects perceived energy and fatigue levels. Diets high in "
            "processed food are associated with higher inflammation markers and greater "
            "fatigue. The link to a Garmin body-battery score is indirect."
        ),
        requires_user_context=True,
    ),
    InsightRule(
        name="heavy_meals_sleep",
        category="sleep",
        trigger_behavior="Heavy Meals",
        trigger_metric="sleepScore",
        comparison_metric="avgSleepStress",
        direction="higher_is_worse",
        description_template=(
            "Heavy meal days show sleep score of {mean_with:.0f} "
            "vs {mean_without:.0f} on other days ({difference:+.0f} points)."
        ),
        research_citation="Crispim et al., 2011, Journal of Clinical Sleep Medicine",
        research_summary=(
            "Eating large or high-fat meals close to bedtime increases gastric acid "
            "secretion, raises core body temperature, and impairs sleep architecture."
        ),
        confounders=["alcohol", "late_caffeine", "stress"],
    ),
    InsightRule(
        name="late_meals_sleep",
        category="sleep",
        trigger_behavior="Late Meals",
        trigger_metric="sleepScore",
        comparison_metric="avgSleepStress",
        direction="higher_is_worse",
        description_template=(
            "Late eating nights show sleep score of {mean_with:.0f} "
            "vs {mean_without:.0f} ({difference:+.0f} points)."
        ),
        research_citation="Kinsey & Ormsbee, 2015, Nutrients",
        research_summary=(
            "Eating within 2 hours of bedtime disrupts circadian rhythm, raises "
            "overnight glucose, and reduces sleep quality. Late meals are associated "
            "with 15-20% higher overnight autonomic-strain ('stress') readings."
        ),
        confounders=["alcohol", "heavy_meals", "late_caffeine"],
    ),

    # ===== INTERMITTENT FASTING =====
    InsightRule(
        name="fasting_body_battery",
        category="lifestyle",
        trigger_behavior="Intermittent Fasting",
        trigger_metric="bodyBatteryAtWakeTime",
        comparison_metric="avgOvernightHrv",
        direction="correlation",
        description_template=(
            "Fasting days show wake body battery of {mean_with:.0f} "
            "vs {mean_without:.0f} on non-fasting days."
        ),
        research_citation="de Cabo & Mattson, 2019, New England Journal of Medicine",
        research_summary=(
            "Intermittent fasting has strong metabolic-health evidence (improved metabolic "
            "flexibility, autophagy, lower insulin resistance). The link to daily Garmin "
            "recovery markers is indirect — sleep, exercise, and alcohol are far stronger "
            "drivers of body battery on any given day. Use as lifestyle context, not a "
            "direct recovery inference."
        ),
        evidence_tier="C",
        claim_strength="weak_association",
        requires_user_context=True,
    ),

    # ===== DEEP SLEEP =====
    InsightRule(
        name="deep_sleep_ratio",
        category="sleep",
        trigger_behavior=None,
        trigger_metric="deepSleepSeconds",
        comparison_metric="sleepScore",
        direction="lower_is_worse",
        description_template=(
            "Your device-estimated deep sleep is {value:.0f}% — "
            "{assessment} your recent personal baseline."
        ),
        research_citation=(
            "Ohayon et al., 2004, Sleep (normative meta-analysis, polysomnography); "
            "Chinoy et al., 2021, Sleep (consumer wearable vs PSG validation); "
            "Schyvens et al., 2024 (Garmin sleep-stage validation)"
        ),
        research_summary=(
            "Deep (NREM stage 3) sleep matters for memory consolidation, immune function, "
            "and growth hormone release. BUT consumer-wearable sleep-stage estimates "
            "differ meaningfully from polysomnography — Garmin in particular tends to "
            "overestimate light sleep and underestimate deep sleep in some studies. "
            "Treat as a PERSONAL TREND vs your own baseline, not a clinical staging "
            "measurement. Do not flag the 13–23% population norm as a deficit."
        ),
        measurement_confidence="medium",
        confounders=["alcohol", "late_exercise", "stress", "heat", "illness"],
    ),

    # ===== MULTI-SIGNAL ILLNESS DETECTION =====
    InsightRule(
        name="illness_signature_multisignal",
        category="recovery",
        trigger_behavior=None,
        trigger_metric="restingHeartRate",
        comparison_metric="avgOvernightHrv",
        direction="higher_is_worse",
        description_template=(
            "Illness-like recovery strain pattern: RHR {rhr_z:+.1f}σ, "
            "HRV {hrv_z:+.1f}σ, respiration {resp_z:+.1f}σ vs your baseline. "
            "Not diagnostic — consider plausible contributors below."
        ),
        research_citation=(
            "Quer et al., 2021, Nature Medicine; "
            "Radin et al., 2020, Lancet Digital Health; "
            "Natarajan et al., 2020, npj Digital Medicine; "
            "Mishra et al., 2022, Lancet Digital Health (systematic review)"
        ),
        research_summary=(
            "The combination of elevated RHR, depressed HRV, and elevated respiration for "
            "2+ consecutive days has been associated with impending illness 1-3 days before "
            "symptom onset (Quer 2021, Radin 2020). The 2022 Lancet Digital Health "
            "systematic review concluded wearable-based illness detection is promising but "
            "performance varies widely — treat as an ILLNESS-LIKE RECOVERY STRAIN PATTERN, "
            "not a diagnosis. Similar patterns can follow alcohol, heat, poor sleep, "
            "heavy training, travel, emotional stress, or luteal-phase physiology."
        ),
        confounders=["alcohol", "luteal_phase", "heat", "late_exercise", "travel", "poor_sleep", "emotional_stress", "doms"],
    ),

    # ===== RESPIRATION RATE AS EARLY WARNING =====
    InsightRule(
        name="elevated_respiration_rate",
        category="recovery",
        trigger_behavior=None,
        trigger_metric="averageRespirationValue",
        comparison_metric="restingHeartRate",
        direction="higher_is_worse",
        description_template=(
            "Overnight respiration rate is {value:.1f} br/min — "
            "{z_score:+.1f}σ above your baseline of {baseline_mean:.1f}."
        ),
        research_citation="Natarajan et al., 2020, npj Digital Medicine",
        research_summary=(
            "Resting respiration rate above 16 br/min, or >1 br/min above personal "
            "baseline for 2+ nights, can be a marker for systemic inflammation, "
            "infection, or overtraining strain. It often rises before subjective symptoms "
            "and tracks alongside HRV decline. Also rises with altitude, heat, alcohol, "
            "asthma, allergies, and sleep-disordered breathing — not a single-cause signal."
        ),
        confounders=["alcohol", "altitude", "heat", "asthma", "allergies", "illness", "poor_sleep"],
    ),

    # ===== ACUTE:CHRONIC WORKLOAD RATIO =====
    InsightRule(
        name="acwr_injury_risk",
        category="exercise",
        trigger_behavior=None,
        trigger_metric="acwr_factor_percent",
        comparison_metric=None,
        direction="higher_is_worse",
        description_template=(
            "Recent training load has risen sharply vs your 28-day average "
            "(ACWR factor {value:.0f}%) — load-spike context signal, not an injury "
            "prediction."
        ),
        research_citation=(
            "Gabbett, 2016, BJSM (original concept); "
            "Impellizzeri et al., 2020, BJSM (critique of ACWR as injury predictor); "
            "Wang et al., 2024, BJSM (training-load injury research limitations)"
        ),
        research_summary=(
            "Gabbett 2016 popularised the idea that a 7-day load >1.5× the 28-day load "
            "raises injury risk. The 2020 Impellizzeri BJSM critique argued ACWR has "
            "conceptual and methodological problems as a causal injury predictor, and "
            "the 2024 BJSM review concluded training-load research has limitations that "
            "make it unsuitable for prescriptive injury prevention. Treat ACWR as a "
            "LOAD-SPIKE CONTEXT SIGNAL — never an injury prediction model."
        ),
        evidence_tier="C",
        claim_strength="weak_association",
        confounders=["sleep_loss", "illness", "stress", "poor_warmup"],
    ),

    # ===== OVERTRAINING SIGNATURE =====
    InsightRule(
        name="overreaching_pattern",
        category="recovery",
        trigger_behavior=None,
        trigger_metric="avgOvernightHrv",
        comparison_metric="acute_load",
        direction="lower_is_worse",
        description_template=(
            "Overreaching signature: training load rising while HRV trending "
            "{hrv_direction} (slope {slope_per_day:+.1f} ms/day over {days} days)."
        ),
        research_citation="Bellenger et al., 2016, Sports Medicine",
        research_summary=(
            "When training load is rising but HRV is dropping or flat over 7+ days, "
            "the body may be failing to absorb the training stimulus. This pattern is "
            "associated with non-functional overreaching and performance decline. A 3-7 "
            "day deload is the conservative response. Confounded by illness, alcohol, "
            "poor sleep, heat, and luteal-phase physiology — verify before naming a cause."
        ),
        confounders=["illness", "alcohol", "poor_sleep", "heat", "luteal_phase"],
    ),

    # ===== SOCIAL JET LAG =====
    InsightRule(
        name="social_jet_lag",
        category="sleep",
        trigger_behavior=None,
        trigger_metric="sleep_midpoint_variance",
        comparison_metric="avgOvernightHrv",
        direction="higher_is_worse",
        description_template=(
            "Sleep midpoint varies by {variance_hours:.1f}h between weekdays and "
            "weekends — equivalent to flying across {variance_hours:.0f} timezones weekly."
        ),
        research_citation="Wittmann et al., 2006, Chronobiology International",
        research_summary=(
            "A weekday-weekend bedtime drift of more than 1 hour ('social jet lag') "
            "is associated with metabolic dysregulation, weight gain, depression, and "
            "reduced cognitive performance — independent of total sleep duration. "
            "Consistency of sleep timing matters as much as quantity."
        ),
        confounders=["shift_work", "travel", "alcohol"],
    ),

    # ===== REM SLEEP DRIFT =====
    InsightRule(
        name="rem_sleep_decline",
        category="sleep",
        trigger_behavior=None,
        trigger_metric="remSleepSeconds",
        comparison_metric="sleepScore",
        direction="lower_is_worse",
        description_template=(
            "Device-estimated REM sleep is trending down — averaging {pct_of_total:.0f}% "
            "vs your recent personal baseline."
        ),
        research_citation=(
            "Leary et al., 2020, JAMA Neurology (PSG cohort, older adults); "
            "Chinoy et al., 2021, Sleep (wearable vs PSG validation); "
            "Schyvens et al., 2024 (Garmin sleep-stage validation)"
        ),
        research_summary=(
            "REM sleep matters for emotional processing and memory. Lower REM has been "
            "associated with higher mortality in older PSG cohorts (Leary 2020). HOWEVER "
            "Garmin's REM estimate is not a clinical PSG measurement — treat as a personal "
            "trend vs your own baseline, not as a clinical sleep-stage measurement. "
            "Common contributors to lower REM include alcohol, late meals, SSRIs, and "
            "inconsistent sleep timing."
        ),
        measurement_confidence="medium",
        confounders=["alcohol", "late_meals", "medications", "stress", "luteal_phase"],
    ),

    # ===== STRESS RECOVERY (BODY BATTERY FLOOR) =====
    InsightRule(
        name="body_battery_floor_decline",
        category="stress",
        trigger_behavior=None,
        trigger_metric="bodyBatteryLowestValue",
        comparison_metric="stressPercentage",
        direction="lower_is_worse",
        description_template=(
            "Body battery is bottoming out at {value:.0f} (baseline: "
            "{baseline_mean:.0f}) — recovery isn't keeping up with daily demands."
        ),
        research_citation="McEwen, 2007, Physiological Reviews",
        research_summary=(
            "A persistently low body battery floor (lowest-of-day value below 20) "
            "is consistent with allostatic load: cumulative physiological strain that "
            "recovery sleep is failing to clear. Sustained for 2+ weeks, this is "
            "associated with burnout, immune suppression, and HPA-axis dysregulation. "
            "Note Garmin body battery is HRV-derived; the same low floor can follow "
            "alcohol, illness, late training, or sustained poor sleep."
        ),
        confounders=["alcohol", "illness", "late_exercise", "poor_sleep", "emotional_stress"],
    ),

    # ===== AEROBIC TRAINING DISTRIBUTION =====
    InsightRule(
        name="grey_zone_training",
        category="exercise",
        trigger_behavior=None,
        trigger_metric="hr_time_in_zone_3",
        comparison_metric="vo2_max_value",
        direction="correlation",
        description_template=(
            "{pct_zone3:.0f}% of activity time is in HR zone 3 (tempo) "
            "vs only {pct_zone2:.0f}% in zone 2 — classic 'grey zone' pattern."
        ),
        research_citation="Seiler, 2010, International Journal of Sports Physiology",
        research_summary=(
            "Polarized training (~80% easy/Z1-Z2, ~20% hard/Z4-Z5) is generally "
            "associated with better aerobic adaptation than 'grey zone' training "
            "(mostly Z3). Excessive Z3 can produce fatigue without optimal adaptation, "
            "plateauing VO2 max."
        ),
    ),

    # ===== VO2 MAX TRAJECTORY =====
    InsightRule(
        name="vo2_max_plateau",
        category="exercise",
        trigger_behavior=None,
        trigger_metric="vo2_max_value",
        comparison_metric=None,
        direction="lower_is_worse",
        description_template=(
            "VO2 max has been flat at {value:.1f} for {days_analyzed} days — "
            "your current training stimulus may have reached its ceiling."
        ),
        research_citation=(
            "Bacon et al., 2013, PLOS ONE (training meta-analysis); "
            "Han et al., 2024, BJSM (overview of meta-analyses, >20M observations, "
            "199 cohorts on cardiorespiratory fitness and mortality)"
        ),
        research_summary=(
            "VO2 max typically plateaus 3-4 months into a consistent training routine. "
            "Breaking through requires a new stimulus: structured intervals, increased "
            "volume, or strength training. The 2024 BJSM overview confirms CRF is one of "
            "the strongest predictors of all-cause mortality, so plateaus are worth "
            "addressing even at recreational levels."
        ),
        evidence_tier="A",
    ),

    # ===== STEPS-STRESS INVERSE COUPLING =====
    InsightRule(
        name="sedentary_stress_coupling",
        category="stress",
        trigger_behavior=None,
        trigger_metric="totalSteps",
        comparison_metric="stressPercentage",
        direction="correlation",
        description_template=(
            "Lower-step days correlate with {pct_change:+.0f}% higher stress — "
            "stillness, not just absence of exercise, is loading you up."
        ),
        research_citation="Choi et al., 2019, JAMA Internal Medicine",
        research_summary=(
            "Days with fewer than 5,000 steps tend to show higher physiological strain "
            "markers — independent of formal exercise. Likely mechanism: reduced "
            "parasympathetic tone from sedentary behaviour. Brief walking breaks every "
            "1-2 hours partially restore it."
        ),
    ),

    # ===== SLEEP FRAGMENTATION =====
    InsightRule(
        name="sleep_fragmentation_hrv",
        category="sleep",
        trigger_behavior=None,
        trigger_metric="awakeCount",
        comparison_metric="avgOvernightHrv",
        direction="higher_is_worse",
        description_template=(
            "{value:.0f} awakenings per night ({restless_moments:.0f} restless moments) "
            "are reducing your overnight HRV recovery."
        ),
        research_citation="Stein & Pu, 2012, Sleep Medicine Reviews",
        research_summary=(
            "Sleep fragmentation (frequent awakenings or restless moments) prevents the "
            "normal HRV rebound that occurs in deep and REM sleep. Even with adequate total "
            "sleep duration, fragmented sleep produces next-day HRV 10-15% below intact sleep."
        ),
        confounders=["alcohol", "pet_in_bedroom", "noise", "heat", "pain", "stress"],
    ),

    # ===== MORNING HR REBOUND =====
    InsightRule(
        name="cardio_reserve_drift",
        category="recovery",
        trigger_behavior=None,
        trigger_metric="restingHeartRate",
        comparison_metric="vo2_max_value",
        direction="higher_is_worse",
        description_template=(
            "Resting HR has drifted up {bpm_change:+.0f} bpm over {days_analyzed} days "
            "while VO2 max is unchanged — possible cardiovascular reserve loss."
        ),
        research_citation=(
            "Cooney et al., 2010, American Heart Journal; "
            "Aune et al., 2017, Nutr Metab Cardiovasc Dis (dose-response meta-analysis)"
        ),
        research_summary=(
            "An RHR drift upward of 3-5 bpm over weeks, without a matching VO2 max "
            "change, is consistent with reduced cardiovascular fitness, accumulated "
            "fatigue, or systemic inflammation. RHR is one of the most prognostic vital "
            "signs for all-cause and CV mortality (Aune 2017 meta-analysis). Confounded "
            "by chronic stress, illness, alcohol, dehydration, and detraining."
        ),
        confounders=["chronic_stress", "illness", "alcohol", "dehydration", "detraining"],
    ),

    # ===== BODY COMPOSITION + HRV =====
    InsightRule(
        name="visceral_fat_hrv",
        category="body_comp",
        trigger_behavior=None,
        trigger_metric="visceral_fat",
        comparison_metric="avgOvernightHrv",
        direction="higher_is_worse",
        description_template=(
            "Visceral fat trending up correlates with HRV trending down "
            "(r={correlation:.2f} over {days_analyzed} days)."
        ),
        research_citation="Felber Dietrich et al., 2006, Europace",
        research_summary=(
            "Visceral adipose tissue is metabolically active and may suppress "
            "parasympathetic (vagal) tone, lowering HRV. The relationship is dose-dependent "
            "and reversible over months. Single-time-point correlations on consumer scales "
            "can be noisy — interpret long-term trends, not day-to-day swings."
        ),
        evidence_tier="C",
        measurement_confidence="medium",
    ),

    # ===== MENSTRUAL CYCLE =====
    InsightRule(
        name="period_day_rhr_hrv",
        category="lifestyle",
        trigger_behavior="Period Day",
        trigger_metric="restingHeartRate",
        comparison_metric="avgOvernightHrv",
        direction="higher_is_worse",
        description_template=(
            "Period/luteal phase days show RHR of {mean_with:.0f} bpm vs {mean_without:.0f} bpm "
            "and HRV of {hrv_with:.0f} ms vs {hrv_without:.0f} ms on other days."
        ),
        research_citation=(
            "Shilaih et al., 2017, Scientific Reports (wrist wearable, cycle pulse rate); "
            "Alzueta/de Zambotti/Baker, 2022 (Oura: luteal HR↑, skin temp↑, RMSSD↓); "
            "de Jager et al., 2026, Sports Medicine (living systematic review, "
            "wearable-derived HRV across reproductive life stages); "
            "Nakagawa et al., 2020, J Clin Med; Brar et al., 2015, J Clin Diagn Res"
        ),
        research_summary=(
            "In the luteal phase (post-ovulation through menstruation), progesterone "
            "elevates resting heart rate by 2-5 bpm and can suppress overnight HRV. "
            "Most wearable studies report HRV higher earlier in the cycle and declining "
            "toward late luteal/menses. This is a NORMAL PHYSIOLOGICAL RESPONSE — use "
            "cycle phase as a CONFOUNDER/CONTEXT LABEL, not a single explanation. The "
            "same pattern can resemble illness, alcohol, training strain, heat, or poor "
            "sleep. CRITICAL: do not flag luteal RHR↑ / HRV↓ as illness or overtraining "
            "unless other clear symptoms are present."
        ),
        confounders=["alcohol", "illness", "heat", "travel", "training_strain", "poor_sleep"],
        sex_specific="female",
    ),

    InsightRule(
        name="follicular_training_window",
        category="exercise",
        trigger_behavior="Follicular Phase",
        trigger_metric="trainingReadinessScore",
        comparison_metric=None,
        direction="lower_is_worse",
        description_template=(
            "Follicular-phase days show training readiness of {mean_with:.0f} "
            "vs {mean_without:.0f} on other phases."
        ),
        research_citation=(
            "Janse de Jonge 2019, Med Sci Sports Exerc; "
            "J Appl Physiol systematic review 2025 (doi:10.1152/japplphysiol.00223.2025); "
            "de Jager et al., 2026, Sports Medicine (wearable HRV across cycle living SR)"
        ),
        research_summary=(
            "Low-oestrogen follicular days (post-menses through ovulation) are commonly "
            "the preferred window for high-intensity strength and HIIT work — perceived "
            "exertion is lower and recovery faster for many athletes. Late luteal days "
            "favour lower-intensity or skill work. Inter-individual variability is large; "
            "coach to the athlete's OWN phase response, not population norms."
        ),
        confounders=["sleep_loss", "alcohol", "stress", "training_load"],
        requires_user_context=True,
        sex_specific="female",
    ),
    InsightRule(
        name="cycle_sleep_loss_confound",
        category="recovery",
        trigger_behavior="Luteal Phase + Sleep Loss",
        trigger_metric="restingHeartRate",
        comparison_metric="sleepDurationHours",
        direction="higher_is_worse",
        description_template=(
            "Short-sleep luteal nights show RHR {mean_with:.0f} bpm vs {mean_without:.0f} bpm "
            "on well-rested luteal nights."
        ),
        research_citation=(
            "de Jager et al., 2026, Sports Medicine "
            "(living systematic review — wearable HRV across reproductive life stages)"
        ),
        research_summary=(
            "Reduced sleep duration raises RHR INDEPENDENT of cycle phase. Before "
            "attributing elevated RHR or depressed HRV to luteal physiology, check "
            "sleep duration vs the user's baseline — if sleep is short, attribute the "
            "change to sleep debt first; cycle phase is an additive but separate driver."
        ),
        confounders=["alcohol", "illness", "heat", "travel", "training_strain"],
        sex_specific="female",
    ),
    InsightRule(
        name="pms_sleep_architecture",
        category="sleep",
        trigger_behavior="Late Luteal Phase",
        trigger_metric="deepSleepSeconds",
        comparison_metric="sleepScore",
        direction="lower_is_worse",
        description_template=(
            "Late-luteal nights show deep sleep of {mean_with:.0f}s vs {mean_without:.0f}s "
            "on follicular nights."
        ),
        research_citation=(
            "Baker, 2007, Sleep (PMC2266284); "
            "PMS & sleep quality cross-sectional, 2025 (PMC11842786)"
        ),
        research_summary=(
            "Women with significant PMS may show reduced deep and REM sleep, more "
            "awakenings, and lower wearable HRV in the late luteal phase. Because "
            "Garmin sleep-stage estimates differ from PSG, treat as a PERSONAL TREND "
            "in days 21–28 rather than a clinical sleep-stage finding. Use as a "
            "confounder/context label before attributing the same pattern to illness. "
            "Sleep hygiene and stress-reduction interventions are first-line."
        ),
        measurement_confidence="medium",
        confounders=["sleep_loss", "alcohol", "stress", "illness", "heat"],
        sex_specific="female",
    ),

    # ===== DOMS =====
    InsightRule(
        name="doms_rhr_elevation",
        category="recovery",
        trigger_behavior="Delayed Onset Muscle Soreness",
        trigger_metric="restingHeartRate",
        comparison_metric="avgOvernightHrv",
        direction="higher_is_worse",
        description_template=(
            "DOMS days show RHR of {mean_with:.0f} bpm vs {mean_without:.0f} bpm baseline."
        ),
        research_citation="Cheung et al., 2003, Sports Medicine; Twist & Eston, 2005, Journal of Sports Sciences",
        research_summary=(
            "Delayed Onset Muscle Soreness (DOMS) from intense or novel exercise causes "
            "localised inflammation that can elevate resting heart rate by 3-8 bpm and "
            "temporarily suppress HRV for 24-72 hours. This closely mimics the "
            "illness-like recovery strain pattern (elevated RHR + low HRV). CRITICAL: "
            "when DOMS is logged, treat it as the most plausible contributor — not "
            "illness — unless other symptoms are present. Body battery may also read "
            "lower than expected."
        ),
        requires_user_context=True,
    ),

    # ===== PET IN BEDROOM =====
    InsightRule(
        name="pet_in_bedroom_sleep",
        category="sleep",
        trigger_behavior="Pet in Bedroom",
        trigger_metric="awakeCount",
        comparison_metric="sleepScore",
        direction="higher_is_worse",
        description_template=(
            "Pet-in-bedroom nights show sleep score of {mean_with:.0f} vs {mean_without:.0f} "
            "and {awake_with:.1f} vs {awake_without:.1f} awakenings."
        ),
        research_citation="Patel et al., 2017, Mayo Clinic Proceedings",
        research_summary=(
            "The Mayo Clinic study found that having a dog IN THE BEDROOM did not "
            "markedly compromise sleep for most people. Effects depend on whether the "
            "pet is ON THE BED, the number of pets, and the animal's behaviour. "
            "Light sleepers and people with restless pets may see fragmentation; "
            "blanket 'pets fragment sleep' claims are not supported. Use as a "
            "possible contributor when awakenings are high and a pet sleeps on the bed."
        ),
        evidence_tier="C",
        claim_strength="weak_association",
        requires_user_context=True,
    ),

    # ===== EMOTIONAL UPSET =====
    InsightRule(
        name="emotional_upset_hrv",
        category="stress",
        trigger_behavior="Emotional Upset",
        trigger_metric="avgOvernightHrv",
        comparison_metric="stressPercentage",
        direction="lower_is_worse",
        description_template=(
            "Emotional upset days show overnight HRV of {mean_with:.0f} ms "
            "vs {mean_without:.0f} ms on other days ({difference:+.0f} ms)."
        ),
        research_citation="Thayer & Lane, 2009, Neuroscience & Biobehavioral Reviews",
        research_summary=(
            "Acute psychological stress and emotional upset can suppress vagal tone, "
            "reducing overnight HRV. The effect may persist into the following night even "
            "if subjective mood has improved. Elevated daytime autonomic-strain ('stress') "
            "and reduced body battery on emotional upset days are expected — do not "
            "conflate with illness unless RHR is also significantly elevated."
        ),
        evidence_tier="C",
        requires_user_context=True,
        confounders=["alcohol", "poor_sleep", "caffeine"],
    ),

    # ===== TRAVELING =====
    InsightRule(
        name="travel_sleep_disruption",
        category="sleep",
        trigger_behavior="Traveling/Vacation",
        trigger_metric="sleepScore",
        comparison_metric="avgOvernightHrv",
        direction="higher_is_worse",
        description_template=(
            "Travel days show sleep score of {mean_with:.0f} vs {mean_without:.0f} at home."
        ),
        research_citation=(
            "Waterhouse et al., 2007, J Sleep Research; "
            "Willoughby et al., 2025, SLEEP (Oura cohort, ~1.5M nights — sleep takes >1 week "
            "to adjust after time-zone crossings)"
        ),
        research_summary=(
            "Travel — especially across time zones — disrupts circadian rhythm, sleep "
            "timing, and sleep quality. Large 2025 wearable cohort confirms recovery "
            "can take more than a week. The 'first-night effect' (lighter sleep, more "
            "awakenings) applies even within the same time zone. HRV, RHR and body "
            "battery often read worse for 1-3+ nights after arriving somewhere new — "
            "this is expected and should not be compared against at-home baselines."
        ),
        evidence_tier="A",
        requires_user_context=True,
        confounders=["alcohol", "heat", "stress"],
    ),

    # ===== WHO EXERCISE GUIDELINES =====
    InsightRule(
        name="who_exercise_guidelines",
        category="exercise",
        trigger_behavior=None,
        trigger_metric="moderateIntensityMinutes",
        comparison_metric="vigorousIntensityMinutes",
        direction="lower_is_worse",
        description_template=(
            "This week: {moderate_mins:.0f} moderate + {vigorous_mins:.0f} vigorous minutes "
            "vs WHO target of 150 moderate or 75 vigorous (or combination)."
        ),
        research_citation="WHO Physical Activity Guidelines, 2020; Bull et al., 2020, British Journal of Sports Medicine",
        research_summary=(
            "The WHO recommends 150-300 minutes of moderate-intensity or 75-150 minutes of "
            "vigorous-intensity aerobic activity per week for adults. Vigorous minutes count "
            "double (1 min vigorous = 2 min moderate). Consistently below target is associated "
            "with higher all-cause mortality, cardiovascular disease risk, and metabolic syndrome. "
            "Garmin's moderateIntensityMinutes and vigorousIntensityMinutes fields map directly "
            "to these categories and reset weekly."
        ),
        evidence_tier="A",
        claim_strength="causal",
    ),

    # ===== FITNESS AGE =====
    InsightRule(
        name="fitness_age_vs_chronological",
        category="exercise",
        trigger_behavior=None,
        trigger_metric="fitness_age",
        comparison_metric="vo2_max_value",
        direction="correlation",
        description_template=(
            "Fitness age: {fitness_age:.0f} vs chronological age {chronological_age:.0f} "
            "(achievable: {achievable_fitness_age:.0f})."
        ),
        research_citation=(
            "Nes et al., 2013, Medicine & Science in Sports & Exercise; "
            "Han et al., 2024, BJSM (overview of meta-analyses, >20M observations)"
        ),
        research_summary=(
            "Garmin's fitness age is derived from VO2 max relative to age-sex norms. "
            "A fitness age below chronological age indicates above-average cardiovascular "
            "fitness; above indicates below-average. Each 1 ml/kg/min improvement in VO2 max "
            "is associated with ~4% lower all-cause mortality risk. The 2024 BJSM overview "
            "of meta-analyses (199 cohorts, >20M observations) confirms cardiorespiratory "
            "fitness is one of the strongest predictors of morbidity and mortality."
        ),
        evidence_tier="A",
    ),

    # ===== ALCOHOL + NEXT-MORNING RHR =====
    InsightRule(
        name="alcohol_morning_rhr",
        category="lifestyle",
        trigger_behavior="Alcohol",
        trigger_metric="restingHeartRate",
        comparison_metric=None,
        direction="higher_is_worse",
        description_template=(
            "The morning after alcohol nights your RHR averages {mean_with:.0f} bpm "
            "vs {mean_without:.0f} bpm on other mornings ({difference:+.0f} bpm)."
        ),
        research_citation=(
            "Sagawa et al., 2011, Alcohol & Alcoholism; "
            "PLOS Digital Health, 2026 (~21k-adult wearable cohort, dose-dependent)"
        ),
        research_summary=(
            "Alcohol metabolism produces acetaldehyde, which elevates heart rate during "
            "processing and into the following morning. Even 1-2 drinks can raise "
            "next-morning RHR by 3-8 bpm and suppress HRV — effects are clearly visible "
            "in wearable data, with dose-response confirmed in a 2026 ~21k-adult cohort. "
            "Separate from the same-night sleep-quality impact."
        ),
        evidence_tier="A",
        claim_strength="causal",
        confounders=["illness", "heat", "late_exercise", "stress", "luteal_phase"],
    ),

    # ===== SPO2 + SLEEP DISORDER =====
    InsightRule(
        name="overnight_spo2_disordered_breathing",
        category="sleep",
        trigger_behavior=None,
        trigger_metric="lowest_spo2_value",
        comparison_metric="awakeCount",
        direction="lower_is_worse",
        description_template=(
            "Overnight SpO2 dropped to {value:.0f}% with {awake_count:.0f} awakenings — "
            "screening-style signal worth discussing with a clinician."
        ),
        research_citation=(
            "Kapur et al., 2017, J Clin Sleep Med (AASM Clinical Practice Guideline "
            "for Adult Obstructive Sleep Apnoea Diagnostic Testing)"
        ),
        research_summary=(
            "Sustained overnight SpO2 below 92%, especially combined with frequent "
            "awakenings, MAY be worth discussing with a clinician — particularly if "
            "combined with snoring, daytime sleepiness, morning headaches, or "
            "hypertension. Garmin SpO2 is NOT a diagnostic sleep study. The AASM "
            "guideline positions diagnostic testing within a proper sleep evaluation, "
            "not passive consumer-wearable inference alone."
        ),
        evidence_tier="C",
        claim_strength="weak_association",
        measurement_confidence="medium",
        confounders=["altitude", "cold_room", "sensor_drift", "side_sleeping"],
    ),

    # ===== BASELINE RELIABILITY GUARD =====
    InsightRule(
        name="baseline_reliability_guard",
        category="recovery",
        trigger_behavior=None,
        trigger_metric="baseline_days_available",
        comparison_metric=None,
        direction="lower_is_worse",
        description_template=(
            "Only {baseline_days:.0f} days of baseline data — trend insights are "
            "low confidence until ≥21 days are available."
        ),
        research_citation="Plews et al., 2013, Sports Medicine",
        research_summary=(
            "Stable HRV/RHR baselines require ~21-30 days of continuous data. Below "
            "this threshold, day-to-day deviations may be measurement noise rather "
            "than real change. When fewer than 21 days are available, suppress strong "
            "trend language and prepend 'Low-confidence (sparse baseline):' to any "
            "deviation finding."
        ),
        evidence_tier="A",
        claim_strength="causal",
        measurement_confidence="high",
    ),

    # ===== TRAVEL / CIRCADIAN DISRUPTION =====
    InsightRule(
        name="travel_circadian_disruption",
        category="sleep",
        trigger_behavior="Travel",
        trigger_metric="sleepScore",
        comparison_metric="restingHeartRate",
        direction="higher_is_worse",
        description_template=(
            "Travel days correlate with sleep score {difference:+.0f} pts vs your "
            "baseline; circadian readjustment after crossing time zones can take "
            ">7 days."
        ),
        research_citation=(
            "Willoughby et al., 2025, SLEEP (Oura cohort, ~1.5M nights)"
        ),
        research_summary=(
            "Large wearable cohort confirms multi-day disruption to sleep timing, "
            "architecture, and overnight cardiovascular markers after time-zone "
            "crossings. Recovery can take more than a week. Treat post-travel "
            "deviations as expected, not as illness or overtraining."
        ),
        evidence_tier="A",
        confounders=["alcohol", "heat", "stress", "altitude"],
        requires_user_context=True,
    ),

    # ===== MULTI-CAUSE RECOVERY STRAIN (META-RULE) =====
    InsightRule(
        name="multi_cause_recovery_strain",
        category="recovery",
        trigger_behavior=None,
        trigger_metric="composite_strain_pattern",
        comparison_metric=None,
        direction="correlation",
        description_template=(
            "RHR {rhr_delta:+.0f} bpm, HRV {hrv_delta:+.0f}%, respiration "
            "{resp_delta:+.1f} bpm vs your baseline. Ranked plausible contributors: "
            "{ranked_contributors}."
        ),
        research_citation="Composite — see individual contributor citations",
        research_summary=(
            "META-RULE. When multiple recovery markers deviate together, prefer a "
            "RANKED-CONTRIBUTOR view over a single-cause claim. Order: (1) user-logged "
            "behaviours in the last 24-48h (alcohol, late exercise, travel, DOMS), "
            "(2) current cycle phase (luteal RHR↑/HRV↓ is normal), (3) tier-A "
            "physiological pattern fits. Never name 'illness' as a single cause when "
            "logged confounders are present."
        ),
        evidence_tier="B",
        confounders=[
            "alcohol", "illness", "late_exercise", "heat", "luteal_phase",
            "travel", "poor_sleep", "caffeine", "doms", "emotional_stress",
            "high_pm25_air_quality", "high_pollen",
        ],
    ),

    # ===== ENVIRONMENTAL CONTEXT (Open-Meteo weather / AQ / pollen) =====
    InsightRule(
        name="heat_recovery_confounder",
        category="recovery",
        trigger_behavior=None,
        trigger_metric="restingHeartRate",
        comparison_metric="avgOvernightHrv",
        direction="higher_is_worse",
        description_template=(
            "Daytime max apparent temperature was {temp_max_c:.1f}°C — heat can "
            "elevate overnight RHR and suppress HRV regardless of training load."
        ),
        research_citation=(
            "Baniak 2023, Sci Total Environ (n=50 older adults, in-home sensors); "
            "Lechat 2025, SLEEP (n=317,758 wearable cohort, outdoor temperature); "
            "Buguet 2007, Sleep Med Rev; Okamoto-Mizuno 2012, J Physiol Anthropol"
        ),
        research_summary=(
            "Bedroom nighttime temperature outside ~20-25°C is associated with "
            "a clinically relevant 5-10% drop in sleep efficiency; a 317k-person "
            "wearable cohort confirms dose-response sleep loss as outdoor T rises. "
            "Daytime apparent T above ~28°C increases sweat-loss, dehydration risk, "
            "and sympathetic tone, raising RHR and reducing HRV the following night. "
            "Treat as a confounder for recovery deviations: when a heat day precedes "
            "a strain finding, rank heat alongside training load and alcohol."
        ),
        evidence_tier="B",
        claim_strength="strong_association",
        measurement_confidence="medium",
        confounders=["dehydration", "alcohol", "training_load"],
    ),
    InsightRule(
        name="air_quality_recovery_confounder",
        category="recovery",
        trigger_behavior=None,
        trigger_metric="averageRespirationValue",
        comparison_metric="avgOvernightHrv",
        direction="higher_is_worse",
        description_template=(
            "European AQI peaked at {european_aqi:.0f} (PM2.5 {pm25:.1f} µg/m³) — "
            "poor air quality can elevate respiration and lower HRV."
        ),
        research_citation=(
            "Niu 2020, Environ Pollut (PM2.5 & HRV meta-analysis, panel studies); "
            "Lin 2022, Biosensors (wearable environmental-exposure review, 24 studies); "
            "Pope 2004, Circulation; Wu 2024, JAMA Network Open (short-term PM2.5 & sleep)"
        ),
        research_summary=(
            "A 10 µg/m³ increase in short-term PM2.5 exposure is associated with "
            "approximately a 0.9% reduction in SDNN and 1.5% reduction in rMSSD HRV "
            "(Niu 2020 meta-analysis), with elevated nocturnal respiration and worse "
            "self-reported sleep. Effects appear below WHO 24-hour guideline values "
            "and present as a lagged HRV suppression hours after exposure (Lin 2022). "
            "Use as a confounder — not a primary cause — when respiration ↑ and HRV ↓ "
            "coincide with an AQI / PM2.5 spike."
        ),
        evidence_tier="B",
        claim_strength="strong_association",
        measurement_confidence="medium",
        confounders=["heat", "training_load", "allergy_season"],
    ),
    InsightRule(
        name="high_pollen_sleep_confounder",
        category="sleep",
        trigger_behavior=None,
        trigger_metric="sleepScore",
        comparison_metric="awakeCount",
        direction="higher_is_worse",
        description_template=(
            "Pollen peaked at {pollen_peak:.0f} grains/m³ (grass/birch/ragweed) — "
            "allergic rhinitis can fragment sleep and reduce REM."
        ),
        research_citation=(
            "Hadjipanayis 2021, Allergol Immunopathol (allergic rhinitis & sleep SR); "
            "Léger 2017, Allergy"
        ),
        research_summary=(
            "Symptomatic allergic rhinitis during high-pollen days is associated with "
            "more awakenings, reduced device-estimated REM, and lower next-day energy "
            "in susceptible users. The effect requires the user actually to be allergic, "
            "so treat high-pollen days as a candidate explanation only when the user has "
            "logged allergy symptoms or reports seasonal sensitivity."
        ),
        evidence_tier="B",
        claim_strength="weak_association",
        measurement_confidence="medium",
        requires_user_context=True,
        confounders=["heat", "open_window_noise", "air_quality"],
    ),
    InsightRule(
        name="allergy_next_day_rhr_systemic",
        category="recovery",
        trigger_behavior=None,
        trigger_metric="restingHeartRate",
        comparison_metric=None,
        direction="higher_is_worse",
        description_template=(
            "RHR is elevated and pollen peaked at {pollen_peak:.0f} grains/m³ "
            "yesterday — allergic rhinitis exerts a measurable systemic next-day "
            "load, not just nasal symptoms."
        ),
        research_citation=(
            "Buekers 2023, Clin Transl Allergy (n=72 adults, 2,497 person-days, "
            "wearable telemonitoring of allergic rhinitis)"
        ),
        research_summary=(
            "In a prospective wearable cohort of 72 adults with allergic rhinitis "
            "across 2,497 person-days, a one-point increase in symptom score was "
            "associated with a +0.08 bpm next-day resting heart-rate increase, "
            "demonstrating that pollen-driven allergy has measurable systemic "
            "autonomic effects beyond the respiratory tract. The effect is small "
            "per symptom-point but cumulative on heavy-symptom days. Requires the "
            "user to actually be allergic — treat as a candidate confounder only "
            "when the user has logged allergy/hay-fever sensitivity."
        ),
        evidence_tier="B",
        claim_strength="strong_association",
        measurement_confidence="high",
        requires_user_context=True,
        confounders=["heat", "poor_sleep", "alcohol", "training_load", "illness"],
    ),
    InsightRule(
        name="asthma_environmental_hr_marker",
        category="recovery",
        trigger_behavior=None,
        trigger_metric="restingHeartRate",
        comparison_metric="averageRespirationValue",
        direction="higher_is_worse",
        description_template=(
            "RHR + respiration are elevated alongside an air-quality / pollen "
            "spike — heart-rate fluctuations are a research-validated digital "
            "marker preceding asthma exacerbations."
        ),
        research_citation=(
            "Cokorudy 2024, ERJ Open Res (systematic review of 23 studies on "
            "digital markers of asthma exacerbations)"
        ),
        research_summary=(
            "A 2024 systematic review of 23 studies on digital markers of asthma "
            "exacerbations found that heart-rate fluctuations and cough were "
            "positively associated with exacerbations in every reported study, "
            "and frequently preceded symptom onset — suggesting wearables can "
            "act as individual-level early-warning signals when environmental "
            "triggers (PM2.5, ozone, pollen, smoke) are elevated. Requires the "
            "user to have asthma — treat as a context cue only when the user has "
            "logged an asthma diagnosis or inhaler use."
        ),
        evidence_tier="B",
        claim_strength="strong_association",
        measurement_confidence="medium",
        requires_user_context=True,
        confounders=["illness", "training_load", "alcohol", "stress"],
    ),
]


def get_rules_by_category(category: str) -> list[InsightRule]:
    """Return all rules matching a category."""
    return [r for r in INSIGHT_RULES if r.category == category]


def get_behavior_rules() -> list[InsightRule]:
    """Return rules that are triggered by a lifestyle behavior."""
    return [r for r in INSIGHT_RULES if r.trigger_behavior is not None]


# Meta-rules that frame how findings are interpreted (ranked-contributor view,
# baseline-confidence guard). They carry no specific deviating metric, so they
# must be force-kept whenever the KB is subset.
_ALWAYS_KEEP_RULES = frozenset({
    "multi_cause_recovery_strain",
    "baseline_reliability_guard",
})

# The environmental-confounder cluster — kept together when the snapshot has any
# environment data, since heat / air-quality / pollen are cross-cutting
# confounders for RHR / HRV / respiration / sleep.
_ENVIRONMENTAL_RULES = frozenset({
    "heat_recovery_confounder",
    "air_quality_recovery_confounder",
    "high_pollen_sleep_confounder",
    "allergy_next_day_rhr_systemic",
    "asthma_environmental_hr_marker",
})


def select_relevant_rule_names(
    metrics: Iterable[str] = (),
    behaviors: Iterable[str] = (),
    rule_names: Iterable[str] = (),
    include_environmental: bool = False,
    biological_sex: str | None = None,
) -> set[str]:
    """Names of the rules relevant to a snapshot's actual signals.

    Used to subset the knowledge base for the portable prompt so the receiving
    model sees only the evidence tied to what shows up in the data (rather than
    all 52 rules every time). Errs toward inclusion — a rule is kept when:

    * it is an always-keep meta-rule (:data:`_ALWAYS_KEEP_RULES`), or
    * its ``name`` appears in ``rule_names`` (a rule that actually fired), or
    * its ``trigger_metric`` / ``comparison_metric`` is in ``metrics``, or
    * its ``trigger_behavior`` matches one of ``behaviors`` (case-insensitive), or
    * ``include_environmental`` and it belongs to the environmental cluster.

    Cycle rules (``sex_specific="female"``) are excluded for non-female users, to
    stay consistent with :func:`get_rules_summary_for_llm`.
    """
    is_female = (biological_sex or "").strip().lower().startswith("f")
    metric_set = {m for m in metrics if m}
    behavior_set = {b.strip().lower() for b in behaviors if b}
    name_set = set(rule_names)
    keep: set[str] = set()
    for r in INSIGHT_RULES:
        if r.sex_specific == "female" and not is_female:
            continue
        if (
            r.name in _ALWAYS_KEEP_RULES
            or r.name in name_set
            or r.trigger_metric in metric_set
            or (r.comparison_metric and r.comparison_metric in metric_set)
            or (r.trigger_behavior and r.trigger_behavior.strip().lower() in behavior_set)
            or (include_environmental and r.name in _ENVIRONMENTAL_RULES)
        ):
            keep.add(r.name)
    return keep


def count_visible_rules(biological_sex: str | None = None) -> int:
    """How many rules the full KB would render for this user (after the sex
    filter) — lets a caller decide whether a subset is worth applying."""
    is_female = (biological_sex or "").strip().lower().startswith("f")
    return sum(
        1 for r in INSIGHT_RULES
        if not (r.sex_specific == "female" and not is_female)
    )


def _abbrev_citation(citation: str) -> str:
    """Compress a citation string to [Author Year, Author Year] format.

    Drops journal names and 'et al.' suffixes to reduce system-prompt tokens.
    e.g. 'Drake et al., 2013, J Clin Sleep Med; Gardiner et al., 2023, ...'
      -> '[Drake 2013, Gardiner 2023]'
    """
    parts = []
    for segment in citation.split(";"):
        tokens = [t.strip().rstrip(",") for t in segment.split(",") if t.strip()]
        # Tokens: [AuthorName, optional 'et al.', Year, ...JournalWords...]
        author = tokens[0].split()[0] if tokens else ""
        # Find the year — first token that looks like a 4-digit number
        year = next((t for t in tokens if t.isdigit() and len(t) == 4), "")
        if author and year:
            parts.append(f"{author} {year}")
        elif author:
            parts.append(author)
    return f"[{', '.join(parts)}]" if parts else citation


def get_rules_summary_for_llm(
    biological_sex: str | None = None,
    only_rules: set[str] | None = None,
    subset_note: bool = False,
) -> str:
    """Format the rules as a concise text block for the LLM system prompt.

    Each rule is emitted with its evidence tier, claim strength, measurement
    confidence (when not 'high'), and confounder list so the agent can match
    its output language to the strength of the evidence.
    Summaries are truncated to the first sentence; citations are abbreviated.

    ``biological_sex`` filters the knowledge base: for non-female users the
    cycle-phase rules (``sex_specific="female"``) are dropped entirely and the
    ``luteal_phase`` confounder tag is stripped from every remaining rule, so a
    male persona never receives menstrual-cycle context it is told it can't use.

    ``only_rules`` (a set of rule names, e.g. from
    :func:`select_relevant_rule_names`) restricts the output to those rules —
    used to subset the KB for the portable prompt. ``subset_note`` adds a one-line
    header note when the KB has been filtered.
    """
    is_female = (biological_sex or "").strip().lower().startswith("f")
    header = "## Medical Evidence Knowledge Base"
    if only_rules is not None and subset_note:
        header += (
            "\n_(Filtered to the rules relevant to this snapshot's metrics, "
            "behaviours and environment — not the full rule set.)_"
        )
    lines = [header + "\n"]
    current_cat = ""
    for rule in INSIGHT_RULES:
        if rule.sex_specific == "female" and not is_female:
            continue
        if only_rules is not None and rule.name not in only_rules:
            continue
        if rule.category != current_cat:
            current_cat = rule.category
            lines.append(f"\n### {current_cat.title()}")
        _claim_abbrev = {"strong_association": "strong", "weak_association": "weak",
                          "causal": "causal", "hypothesis": "hypothesis"}
        meta_parts = [rule.evidence_tier, _claim_abbrev.get(rule.claim_strength, rule.claim_strength)]
        if rule.measurement_confidence != "high":
            meta_parts.append("medium-conf")
        if rule.requires_user_context:
            meta_parts.append("needs-log")
        meta = ", ".join(meta_parts)
        # First sentence only — mechanism detail is captured in the full rule
        # objects used by InsightScanner; the LLM only needs the key claim.
        # Split only on ". " preceded by a letter so decimal numbers like
        # "+0.08 bpm", "PM2.5", ">1.5×" are never truncated mid-number.
        _parts = re.split(r'(?<=[a-zA-Z])\. ', rule.research_summary, maxsplit=1)
        summary = _parts[0] + "."
        lines.append(
            f"- **{rule.name}** [{meta}]: {summary} "
            f"{_abbrev_citation(rule.research_citation)}"
        )
        confounders = rule.confounders
        if not is_female:
            confounders = [c for c in confounders if c != "luteal_phase"]
        if confounders:
            lines.append(f"  Confounders: {', '.join(confounders)}")
    return "\n".join(lines)
