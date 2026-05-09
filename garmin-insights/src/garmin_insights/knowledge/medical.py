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
