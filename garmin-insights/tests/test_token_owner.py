"""Token-store ownership guard — the fetcher's cross-user data-contamination guard.

Stored OAuth tokens carry no visible account identity. If TOKEN_DIR points at
another user's tokens, the fetcher would download THAT account's health data
into this user's database, silently and indefinitely. ``verify_token_owner``
is what stops that, and it is designed to fail CLOSED: anything it cannot
positively verify must raise, sending the caller down the credential-login
path rather than proceeding with unknown tokens.

Every branch is pinned here, because the failure is invisible in production —
there is no error, just the wrong person's data.
"""

from __future__ import annotations

import pytest

pytest.importorskip("garmin_grafana", reason="garmin-grafana not installed")

from garmin_grafana.token_owner import (  # noqa: E402
    GarminConnectAuthenticationError,
    TOKEN_OWNER_FILE,
    read_token_owner,
    token_owner_path,
    verify_token_owner,
    write_token_owner,
)

OWNER = "alice@example.com"
OTHER = "bob@example.com"


class FakeGarmin:
    """Stand-in for the garminconnect client.

    ``username`` is what /userprofile-service/socialProfile reports: the login
    email on password accounts, an opaque handle on others. ``raises`` models
    the endpoint being unavailable.
    """

    def __init__(self, username: str | None = None, raises: bool = False):
        self._username = username
        self._raises = raises
        self.calls = 0

    def connectapi(self, path):
        self.calls += 1
        if self._raises:
            raise RuntimeError("profile endpoint unavailable")
        if self._username is None:
            return {}
        return {"userName": self._username}


@pytest.fixture
def token_dir(tmp_path):
    d = tmp_path / "tokens"
    d.mkdir()
    return str(d)


# ------------------------------------------------------------------
# Marker file round-trip
# ------------------------------------------------------------------
def test_owner_marker_roundtrip_is_case_and_space_insensitive(token_dir):
    write_token_owner(token_dir, "  Alice@Example.COM  ")
    assert read_token_owner(token_dir) == OWNER


def test_read_owner_is_none_when_marker_absent(token_dir):
    assert read_token_owner(token_dir) is None


def test_read_owner_is_none_when_marker_is_blank(token_dir):
    with open(token_owner_path(token_dir), "w") as f:
        f.write("   \n")
    assert read_token_owner(token_dir) is None


def test_marker_path_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert token_owner_path("~/.garminconnect") == str(
        tmp_path / ".garminconnect" / TOKEN_OWNER_FILE
    )


def test_write_owner_creates_missing_token_dir(tmp_path):
    fresh = str(tmp_path / "not-yet-there")
    write_token_owner(fresh, OWNER)
    assert read_token_owner(fresh) == OWNER


def test_write_owner_swallows_unusable_token_dir(tmp_path):
    """An unusable TOKEN_DIR must warn, not crash the fetcher.

    Uses a regular file where the directory should be — makedirs then raises
    OSError regardless of the running user, unlike a chmod'd directory, which
    root would sail straight through.
    """
    not_a_dir = tmp_path / "token_dir_is_a_file"
    not_a_dir.write_text("oops")

    write_token_owner(str(not_a_dir), OWNER)  # must not raise
    assert read_token_owner(str(not_a_dir)) is None


# ------------------------------------------------------------------
# verify_token_owner — the accept paths
# ------------------------------------------------------------------
def test_no_expected_email_skips_verification(token_dir):
    """Interactive single-user mode has no configured identity to check."""
    garmin = FakeGarmin(username=OTHER)
    verify_token_owner(garmin, token_dir, None)
    verify_token_owner(garmin, token_dir, "")
    assert garmin.calls == 0, "should not even call the profile endpoint"


def test_email_form_username_matching_expected_is_accepted(token_dir):
    verify_token_owner(FakeGarmin(username=OWNER), token_dir, OWNER)


def test_username_match_is_case_insensitive(token_dir):
    verify_token_owner(FakeGarmin(username="ALICE@EXAMPLE.COM"), token_dir, OWNER)


def test_accepting_via_username_stamps_the_marker(token_dir):
    """So a later login can still verify when the profile is unavailable."""
    assert read_token_owner(token_dir) is None
    verify_token_owner(FakeGarmin(username=OWNER), token_dir, OWNER)
    assert read_token_owner(token_dir) == OWNER


def test_opaque_username_falls_back_to_marker(token_dir):
    """Non-email userNames are handles, not identities — use the marker."""
    write_token_owner(token_dir, OWNER)
    verify_token_owner(FakeGarmin(username="cyclist_42"), token_dir, OWNER)


def test_unavailable_profile_falls_back_to_marker(token_dir):
    write_token_owner(token_dir, OWNER)
    verify_token_owner(FakeGarmin(raises=True), token_dir, OWNER)


# ------------------------------------------------------------------
# verify_token_owner — the refuse paths (the whole point of the guard)
# ------------------------------------------------------------------
def test_mismatched_email_username_is_refused(token_dir):
    with pytest.raises(GarminConnectAuthenticationError, match="owner mismatch"):
        verify_token_owner(FakeGarmin(username=OTHER), token_dir, OWNER)


def test_mismatched_marker_is_refused(token_dir):
    """Opaque username + a marker naming someone else = another user's tokens."""
    write_token_owner(token_dir, OTHER)
    with pytest.raises(GarminConnectAuthenticationError, match="owner mismatch"):
        verify_token_owner(FakeGarmin(username="cyclist_42"), token_dir, OWNER)


def test_unverifiable_tokens_fail_closed(token_dir):
    """No email-form userName AND no marker: ownership is unknowable, so the
    tokens must be discarded rather than trusted."""
    with pytest.raises(GarminConnectAuthenticationError, match="unverifiable"):
        verify_token_owner(FakeGarmin(username="cyclist_42"), token_dir, OWNER)


def test_unavailable_profile_with_no_marker_fails_closed(token_dir):
    with pytest.raises(GarminConnectAuthenticationError, match="unverifiable"):
        verify_token_owner(FakeGarmin(raises=True), token_dir, OWNER)


def test_empty_profile_response_with_no_marker_fails_closed(token_dir):
    with pytest.raises(GarminConnectAuthenticationError, match="unverifiable"):
        verify_token_owner(FakeGarmin(username=None), token_dir, OWNER)


def test_refusal_does_not_overwrite_the_existing_marker(token_dir):
    """The marker records who the tokens belong to. A failed verification must
    not relabel someone else's tokens with our email."""
    write_token_owner(token_dir, OTHER)
    with pytest.raises(GarminConnectAuthenticationError):
        verify_token_owner(FakeGarmin(username="cyclist_42"), token_dir, OWNER)
    assert read_token_owner(token_dir) == OTHER


# ------------------------------------------------------------------
# The scenario the guard exists for
# ------------------------------------------------------------------
def test_two_users_sharing_one_token_dir_are_caught(tmp_path):
    """The documented misconfiguration: both user envs point at the same
    TOKEN_DIR. The second user must be refused, not silently fed the first
    user's data."""
    shared = str(tmp_path / "shared-tokens")

    # Alice logs in with credentials first and stamps the marker.
    write_token_owner(shared, OWNER)
    verify_token_owner(FakeGarmin(username=OWNER), shared, OWNER)

    # Bob's fetcher, pointed at the same directory, must refuse.
    with pytest.raises(GarminConnectAuthenticationError, match="owner mismatch"):
        verify_token_owner(FakeGarmin(username="opaque_handle"), shared, OTHER)

    # And Alice's marker survives, so her next run still verifies.
    assert read_token_owner(shared) == OWNER
