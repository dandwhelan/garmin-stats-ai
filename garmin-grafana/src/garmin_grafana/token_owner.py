"""Token-store ownership guard.

Stored Garmin OAuth tokens carry no visible account identity, so a TOKEN_DIR
pointed at (or shared with) another user's tokens silently downloads THAT
account's data into this user's database. Ownership is verified after every
token login: against Garmin's own profile userName when it is an email
(password-login accounts), else against an owner marker file written into
TOKEN_DIR whenever we log in with explicit credentials.

Split out of ``garmin_fetch`` so it can be exercised without importing that
module — ``garmin_fetch`` reads its whole configuration into module-level
globals at import time and opens a database connection, which makes the guard
untestable in place. These functions take ``token_dir`` / ``expected_email``
explicitly; ``garmin_fetch`` keeps thin wrappers that pass its globals in.
"""

from __future__ import annotations

import logging
import os

try:  # pragma: no cover - exercised implicitly by whichever branch applies
    from garminconnect import GarminConnectAuthenticationError
except ImportError:  # garminconnect is absent in test/lint environments
    class GarminConnectAuthenticationError(Exception):  # type: ignore[no-redef]
        """Fallback mirroring garminconnect's error when it isn't installed."""


TOKEN_OWNER_FILE = "account_owner.txt"


def token_owner_path(token_dir: str) -> str:
    return os.path.join(os.path.expanduser(token_dir), TOKEN_OWNER_FILE)


def read_token_owner(token_dir: str) -> str | None:
    """The email recorded for this token store, or None if unreadable/absent."""
    try:
        with open(token_owner_path(token_dir)) as f:
            owner = f.read().strip().lower()
        return owner or None
    except OSError:
        return None


def write_token_owner(token_dir: str, email: str) -> None:
    try:
        os.makedirs(os.path.expanduser(token_dir), exist_ok=True)
        with open(token_owner_path(token_dir), "w") as f:
            f.write(email.strip().lower() + "\n")
    except OSError as err:
        logging.warning(f"Could not record token owner marker in '{token_dir}': {err}")


def verify_token_owner(garmin, token_dir: str, expected_email: str | None) -> None:
    """Raise GarminConnectAuthenticationError if the stored tokens belong to a
    different Garmin account than ``expected_email``. The caller's except path
    then performs a fresh credential login for the right account, which
    re-dumps correct tokens (and the owner marker) into ``token_dir``."""
    if not expected_email:
        return  # interactive single-user mode — no expected identity to check
    expected = expected_email.strip().lower()

    # socialProfile.userName is the login email for password accounts; on some
    # accounts it is an opaque handle instead, in which case we fall back to
    # the owner marker written at credential login.
    try:
        profile = garmin.connectapi("/userprofile-service/socialProfile") or {}
        username = str(profile.get("userName") or "")
    except Exception as err:
        logging.debug(f"Could not read profile userName for ownership check: {err}")
        username = ""

    actual = username.strip().lower() if "@" in username else read_token_owner(token_dir)
    if actual is None:
        # Fail CLOSED: unverifiable tokens could belong to anyone, and fetching
        # through them would silently fill this user's DB with another account's
        # data. Raising here sends the caller down the credential-login path,
        # which re-authenticates as expected_email and stamps the owner marker
        # so future token logins are verifiable.
        logging.warning(
            f"Cannot verify which Garmin account the tokens in '{token_dir}' belong to "
            f"(no email-form profile userName and no owner marker) — discarding them and "
            f"re-authenticating as {expected_email}."
        )
        raise GarminConnectAuthenticationError(
            f"Token store ownership unverifiable for '{token_dir}' — re-authentication required"
        )
    if actual != expected:
        logging.error(
            f"Stored tokens in '{token_dir}' belong to Garmin account '{actual}', but this "
            f"fetcher is configured for '{expected}' — refusing to download another user's "
            f"data. Re-authenticating as {expected}. If you run multiple users, every user "
            f"env MUST set its own distinct TOKEN_DIR."
        )
        raise GarminConnectAuthenticationError(
            f"Token store owner mismatch: {actual} != {expected}"
        )
    # Ownership confirmed — persist the marker so future runs can verify even
    # if the profile userName is unavailable or not email-form.
    if read_token_owner(token_dir) != expected:
        write_token_owner(token_dir, expected)
