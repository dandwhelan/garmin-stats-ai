"""settings_for_user must never leak the web-server owner's identity onto
another user's data.

Regression: in multi-user mode the single web server runs under the
START_WEB user's env (name / email / biological sex). When users/<id>.env
couldn't be found (relative users_dir + cron launching from $HOME), the
overlay silently no-op'd and every other user's agent inherited the server
owner's identity — e.g. Dan's portable prompt opened with "You are speaking
with **Helen**" and applied female reference ranges to his data.
"""

from __future__ import annotations

import pytest

from garmin_insights.config import Settings


@pytest.fixture
def multi_user_settings(tmp_path):
    """Settings as the web server sees them when launched from helen.env,
    with USERS mapping both users and only helen's per-user file present."""
    users_dir = tmp_path / "users"
    users_dir.mkdir()
    (users_dir / "helen.env").write_text(
        "DISPLAY_NAME=Helen\n"
        "GARMINCONNECT_EMAIL=helen@example.com\n"
        "BIOLOGICAL_SEX=Female\n"
    )
    (users_dir / "dan.env").write_text(
        "DISPLAY_NAME=Daniel\n"
        "GARMINCONNECT_EMAIL=dan@example.com\n"
        "BIOLOGICAL_SEX=Male\n"
    )
    return Settings(
        _env_file=None,
        users=f"dan:{tmp_path}/dan.db,helen:{tmp_path}/helen.db",
        users_dir=str(users_dir),
        display_name="Helen",
        garminconnect_email="helen@example.com",
        biological_sex="Female",
        sqlite_db_path=f"{tmp_path}/helen.db",
    )


def test_each_user_gets_own_identity_and_db(multi_user_settings, tmp_path):
    dan = multi_user_settings.settings_for_user("dan")
    assert dan.sqlite_db_path == f"{tmp_path}/dan.db"
    assert dan.display_name == "Daniel"
    assert dan.garminconnect_email == "dan@example.com"
    assert dan.biological_sex == "Male"

    helen = multi_user_settings.settings_for_user("helen")
    assert helen.sqlite_db_path == f"{tmp_path}/helen.db"
    assert helen.display_name == "Helen"
    assert helen.biological_sex == "Female"


def test_missing_user_env_does_not_inherit_server_owner_identity(
    multi_user_settings, tmp_path, caplog
):
    """The core regression: users/dan.env unfindable → dan must get a BLANK
    identity (and a warning), never Helen's name/sex."""
    (tmp_path / "users" / "dan.env").unlink()
    with caplog.at_level("WARNING", logger="garmin_insights.config"):
        dan = multi_user_settings.settings_for_user("dan")
    assert dan.sqlite_db_path == f"{tmp_path}/dan.db"  # data still his own
    assert dan.display_name == ""
    assert dan.garminconnect_email == ""
    assert dan.biological_sex == ""
    assert any("dan.env not found" in r.message for r in caplog.records)


def test_single_user_default_keeps_base_identity(tmp_path):
    """Single-user mode (USERS empty): the base env IS the user, so identity
    must still pass through untouched."""
    s = Settings(
        _env_file=None,
        users="",
        display_name="Solo",
        garminconnect_email="solo@example.com",
        biological_sex="Male",
        sqlite_db_path=f"{tmp_path}/solo.db",
    )
    solo = s.settings_for_user("default")
    assert solo.display_name == "Solo"
    assert solo.biological_sex == "Male"
    assert solo.sqlite_db_path == f"{tmp_path}/solo.db"


def test_relative_users_dir_resolves_from_repo_root(tmp_path, monkeypatch):
    """users_dir is 'users' by default; when cwd is elsewhere (cron runs from
    $HOME) the lookup must fall back to the repo root of an editable install
    rather than silently finding nothing."""
    from pathlib import Path
    import garmin_insights.config as config_mod

    repo_root = Path(config_mod.__file__).resolve().parents[3]
    real_users = repo_root / "users"
    # Only meaningful when running from the source tree (editable install).
    if not real_users.is_dir():
        pytest.skip("not an editable install — no repo-root users/ dir")

    probe = real_users / "_cfgtest_probe.env"
    probe.write_text("DISPLAY_NAME=Probe\nBIOLOGICAL_SEX=Male\n")
    try:
        monkeypatch.chdir(tmp_path)  # cwd without a users/ dir
        s = Settings(
            _env_file=None,
            users=f"_cfgtest_probe:{tmp_path}/p.db",
            users_dir="users",
            display_name="Helen",
            biological_sex="Female",
        )
        probe_user = s.settings_for_user("_cfgtest_probe")
        assert probe_user.display_name == "Probe"
        assert probe_user.biological_sex == "Male"
    finally:
        probe.unlink()
