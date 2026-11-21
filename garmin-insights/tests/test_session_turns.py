"""SessionManager turn guard — one in-flight chat turn per session."""

import time

from garmin_insights.web.sessions import SessionManager


def test_second_turn_is_refused_until_the_first_ends():
    mgr = SessionManager()
    sid, _, _ = mgr.get_or_create(None)
    assert mgr.try_begin_turn(sid)
    assert not mgr.try_begin_turn(sid)
    mgr.end_turn(sid)
    assert mgr.try_begin_turn(sid)


def test_sessions_are_independent():
    mgr = SessionManager()
    a, _, _ = mgr.get_or_create(None)
    b, _, _ = mgr.get_or_create(None)
    assert mgr.try_begin_turn(a)
    assert mgr.try_begin_turn(b)


def test_stale_turn_stops_blocking(monkeypatch):
    mgr = SessionManager()
    sid, _, _ = mgr.get_or_create(None)
    assert mgr.try_begin_turn(sid)
    later = time.time() + SessionManager.TURN_STALE_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert mgr.try_begin_turn(sid)
