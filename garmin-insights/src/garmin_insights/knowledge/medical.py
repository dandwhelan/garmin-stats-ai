"""Medical evidence knowledge base — structured insight rules with research citations.

These rules is injected into the LLM system prompt and also used by the
proactive insight scanner to detect and explain patterns.
"""

from __future__ import annotations

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
        research_citation="Drake et al., 2013, Journal of Clinical Sleep Medicine",
        research_summary=(
            "Caffeine has a half-life of 5-7 hours. Consuming caffeine within 6 hours "
            "of bedtime reduces total sleep time by approximately 1 hour and significantly "
            "impairs sleep quality."
        ),
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
            "cortisol response to caffeine."
        ),
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
        research_citation="Ebrahim et al., 2013, Alcoholism: Clinical & Experimental Research",
        research_summary=(
            "Alcohol suppresses REM sleep in the first half of the night and causes "
            "rebound sleep fragmentation in the second half. Even moderate consumption "
            "(1-2 drinks) reduces REM by 9-17%."
        ),
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
            "A declining HRV trend over 5+ days indicates accumulated physiological stress "
            "and incomplete recovery. Athletes showing this pattern are at higher risk "
            "of non-functional overreaching and illness."
        ),
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
        research_citation="Radin et al., 2020, The Lancet Digital Health",
        research_summary=(
            "Elevated resting heart rate (>5 bpm above personal baseline for 3+ days) "
            "is an early indicator of infection, illness, or overtraining. "
            "Wearable-detected RHR elevations preceded COVID-19 symptom onset by 1-4 days."
        ),
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
        research_citation="Adam et al., 2017, Psychoneuroendocrinology",
        research_summary=(
            "Chronic high cortisol (reflected in high stress duration) impairs sleep quality, "
            "immune function, and cognitive performance. Days with >30% high stress show "
            "significantly reduced overnight recovery."
        ),
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
        research_citation="Stutz et al., 2019, Sports Medicine",
        research_summary=(
            "Vigorous exercise completed less than 1 hour before bedtime can delay "
            "sleep onset and reduce sleep quality due to elevated core body temperature "
            "and sympathetic nervous system activation."
        ),
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
            "Cold water immersion activates the parasympathetic nervous system, "
            "increases vagal tone, and has been shown to enhance post-exercise recovery "
            "and improve HRV markers."
        ),
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
            "and normalizes cortisol patterns."
        ),
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
        research_citation="Galli et al., 2008, Nature",
        research_summary=(
            "Allergic inflammation triggers systemic immune responses that can elevate "
            "resting heart rate, increase stress markers, and impair sleep quality "
            "through histamine-mediated arousal pathways."
        ),
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
        research_citation="Miglis, 2018, Current Pain & Headache Reports",
        research_summary=(
            "Autonomic nervous system dysfunction often precedes migraines by 24-48 hours. "
            "A drop in HRV and increase in resting stress can serve as early warning signals "
            "for migraine onset."
        ),
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
            "Regular stretching activates the parasympathetic nervous system, reduces "
            "muscle tension and cortisol levels, and may improve sleep quality through "
            "enhanced relaxation."
        ),
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
            "Dietary quality significantly affects perceived energy and fatigue levels. "
            "Diets high in processed food are associated with higher inflammation markers "
            "and greater fatigue."
        ),
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
            "with 15-20% higher overnight stress levels."
        ),
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
            "Intermittent fasting improves metabolic flexibility, enhances "
            "autophagy, and may improve overnight HRV through reduced metabolic "
            "demand during sleep."
        ),
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
            "Your deep sleep percentage is {value:.0f}% — "
            "{assessment} the recommended 13-23% range."
        ),
        research_citation="Walker, 2017, Why We Sleep (UC Berkeley)",
        research_summary=(
            "Deep (NREM stage 3) sleep is critical for memory consolidation, "
            "immune function, and growth hormone release. Adults should aim for "
            "13-23% of total sleep as deep sleep. Deficit impairs next-day cognition."
        ),
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
            "Multi-signal illness pattern detected: RHR elevated {rhr_z:+.1f}σ, "
            "HRV depressed {hrv_z:+.1f}σ, respiration {resp_z:+.1f}σ from baseline."
        ),
        research_citation="Quer et al., 2021, Nature Medicine",
        research_summary=(
            "The combination of elevated RHR (>3 bpm above baseline), depressed HRV "
            "(>10% below baseline), and elevated respiration rate (>1 br/min above) "
            "for 2+ consecutive days is 80%+ specific for impending illness — including "
            "respiratory infection — 1-3 days before symptom onset. Single-signal anomalies "
            "are far less specific."
        ),
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
        research_citation="Natarajan et al., 2020, BMJ Open",
        research_summary=(
            "Resting respiration rate above 16 br/min, or >1 br/min above personal "
            "baseline for 2+ nights, is a sensitive marker for systemic inflammation, "
            "infection, or overtraining. It often rises before subjective symptoms appear "
            "and tracks alongside HRV decline."
        ),
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
            "Your acute:chronic workload ratio is in the danger zone "
            "(ACWR factor {value:.0f}%) — recent training load is outpacing "
            "your fitness base."
        ),
        research_citation="Gabbett, 2016, British Journal of Sports Medicine",
        research_summary=(
            "Athletes whose 7-day training load exceeds 1.5× their 28-day average have "
            "a 4-5× higher injury risk in the following 1-2 weeks. The 'sweet spot' is "
            "an ACWR between 0.8 and 1.3. Garmin reports this as a percentage factor in "
            "training readiness."
        ),
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
            "the body is failing to absorb the training stimulus. Continuing in this state "
            "leads to non-functional overreaching, performance decline, and elevated injury "
            "risk. The fix is a 3-7 day deload, not more training."
        ),
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
            "REM sleep is trending down — averaging {pct_of_total:.0f}% of total "
            "sleep vs the recommended 20-25%."
        ),
        research_citation="Leary et al., 2020, JAMA Neurology",
        research_summary=(
            "REM sleep is essential for emotional processing, memory consolidation, and "
            "cognitive performance. A 5%+ drop in REM percentage over 2 weeks is associated "
            "with increased mortality risk and impaired mood. Common causes include alcohol, "
            "late meals, SSRIs, and inconsistent sleep timing."
        ),
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
            "indicates allostatic load: cumulative stress that recovery sleep is failing "
            "to clear. Sustained for 2+ weeks, this predicts burnout, immune suppression, "
            "and HPA-axis dysregulation."
        ),
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
            "Polarized training (~80% easy/Z1-Z2, ~20% hard/Z4-Z5) consistently "
            "outperforms 'grey zone' training (mostly Z3). Excessive Z3 produces fatigue "
            "without optimal aerobic adaptation, plateauing VO2 max and increasing injury risk."
        ),
    ),

    # ===== HYDRATION + RHR =====
    InsightRule(
        name="hydration_rhr_impact",
        category="lifestyle",
        trigger_behavior=None,
        trigger_metric="hydration",
        comparison_metric="restingHeartRate",
        direction="correlation",
        description_template=(
            "Lower hydration days show RHR of {mean_low:.0f} bpm vs "
            "{mean_high:.0f} bpm on well-hydrated days."
        ),
        research_citation="Watso & Farquhar, 2019, Nutrients",
        research_summary=(
            "Even mild dehydration (1-2% body mass loss) elevates resting heart rate "
            "by 3-5 bpm and reduces cardiovascular efficiency. Chronic underhydration "
            "is associated with elevated cortisol and lower HRV."
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
        research_citation="Bacon et al., 2013, PLOS ONE (meta-analysis)",
        research_summary=(
            "VO2 max typically plateaus 3-4 months into a consistent training routine. "
            "Breaking through requires a new stimulus: structured intervals, increased "
            "volume, or strength training. Continued same-stimulus training won't restart "
            "adaptation."
        ),
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
            "Days with fewer than 5,000 steps show measurably higher physiological stress "
            "markers — independent of formal exercise. The mechanism is reduced parasympathetic "
            "tone from sedentary behavior; brief walking breaks every 1-2 hours partially restore it."
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
        research_citation="Cooney et al., 2010, American Journal of Cardiology",
        research_summary=(
            "An RHR drift upward of 3-5 bpm over weeks, without a matching VO2 max "
            "change, is an early signal of reduced cardiovascular fitness, accumulated "
            "fatigue, or systemic inflammation. RHR is one of the most prognostic vital "
            "signs for all-cause mortality."
        ),
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
        research_citation="Felber Dietrich et al., 2006, European Heart Journal",
        research_summary=(
            "Visceral adipose tissue is metabolically active and directly suppresses "
            "parasympathetic (vagal) tone, lowering HRV. The relationship is dose-dependent "
            "and reversible — every 1% reduction in visceral fat produces measurable HRV "
            "improvements within 6-8 weeks."
        ),
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
            "consider screening for sleep-disordered breathing."
        ),
        research_citation="Berry et al., 2017, AASM Clinical Practice Guidelines",
        research_summary=(
            "Sustained overnight SpO2 below 92%, especially combined with frequent "
            "awakenings, is suggestive of sleep apnea or other disordered breathing. "
            "These patterns are strongly associated with daytime fatigue, hypertension, "
            "and elevated cardiovascular risk if left unaddressed."
        ),
    ),
]


def get_rules_by_category(category: str) -> list[InsightRule]:
    """Return all rules matching a category."""
    return [r for r in INSIGHT_RULES if r.category == category]


def get_behavior_rules() -> list[InsightRule]:
    """Return rules that are triggered by a lifestyle behavior."""
    return [r for r in INSIGHT_RULES if r.trigger_behavior is not None]


def get_rules_summary_for_llm() -> str:
    """Format all rules as a concise text block for the LLM system prompt."""
    lines = ["## Medical Evidence Knowledge Base\n"]
    current_cat = ""
    for rule in INSIGHT_RULES:
        if rule.category != current_cat:
            current_cat = rule.category
            lines.append(f"\n### {current_cat.title()}")
        lines.append(
            f"- **{rule.name}**: {rule.research_summary} "
            f"(Source: {rule.research_citation})"
        )
    return "\n".join(lines)
