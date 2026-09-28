"""Ratings/derived fields checked against real Fitdays app output."""

from garmin_insights.scales.standards import enrich, ideal_weight_kg


def test_ideal_weight_matches_fitdays():
    assert abs(ideal_weight_kg(185, "Male") * 2.20462 - 166.0) < 0.3   # Dan
    assert abs(ideal_weight_kg(175, "Female") * 2.20462 - 139.1) < 0.5  # Helen


def test_dan_scan_ratings_match_app():
    metrics = {"bmi": 20.8, "body_fat_pct": 7.4, "body_water_pct": 67.8,
               "muscle_mass_kg": 61.32, "visceral_fat": 1.0, "metabolic_age": 35}
    extras = {"skeletal_muscle_pct": 52.9, "protein_pct": 18.5, "subcutaneous_fat_pct": 5.3,
              "segments": {"left_arm": {"muscle_pct": 112.2, "fat_pct": 17.4},
                           "left_leg": {"muscle_pct": 114.6, "fat_pct": 49.8},
                           "trunk": {"muscle_pct": 106.9, "fat_pct": 62.5}}}
    out = enrich(metrics, extras, 71.05, 185, "Male", 35)
    assert out["muscle_rate_pct"] == 86.3
    r = out["ratings"]
    assert r["body_fat_pct"] == "Standard"
    assert r["subcutaneous_fat_pct"] == "Low"
    assert r["metabolic_age"] == "Excellent"
    seg = out["segments"]
    assert seg["left_arm"]["muscle_rating"] == "Standard"
    assert seg["left_leg"]["muscle_rating"] == "High"
    assert seg["trunk"]["muscle_rating"] == "Standard"
    assert seg["left_arm"]["fat_rating"] == "Low"


def test_helen_scan_ratings_match_app():
    metrics = {"bmi": 19.8, "body_fat_pct": 18.8, "body_water_pct": 59.5,
               "muscle_mass_kg": 45.9, "visceral_fat": 3.0, "metabolic_age": 33}
    extras = {"skeletal_muscle_pct": 45.3, "protein_pct": 16.2, "subcutaneous_fat_pct": 13.5}
    r = enrich(metrics, extras, 60.74, 175, "Female", 35)["ratings"]
    assert all(r[k] == "Standard" for k in
               ("bmi", "body_fat_pct", "body_water_pct", "muscle_rate_pct",
                "skeletal_muscle_pct", "protein_pct", "visceral_fat"))
    assert r["subcutaneous_fat_pct"] == "Low"


def test_enrich_tolerates_sparse_reading():
    out = enrich({}, {}, 70.0, None, "", None)
    assert "ratings" not in out and "ideal_weight_kg" not in out
