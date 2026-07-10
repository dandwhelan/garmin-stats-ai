"""Upload manual body-composition weigh-ins to Garmin Connect.

Mirrors what lswiderski's mi-scale-exporter web form does, but locally: the
weigh-in is pushed straight to the user's own Garmin Connect account using the
OAuth tokens the fetcher already maintains in TOKEN_DIR, so no credentials
ever leave this machine. The uploaded reading then flows back into the local
DB on the next fetch cycle like any scale weigh-in.

Token-only by design: the web server never has the Garmin password, so if the
stored tokens are missing/expired we fail with a clear message instead of
attempting a credential login — the fetcher owns credential refresh.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Written by the fetcher on every credential login (see garmin_fetch.py) so
# token stores stay attributable to a Garmin account even when the profile
# userName is an opaque handle rather than an email.
_TOKEN_OWNER_FILE = "account_owner.txt"


class GarminUploadError(Exception):
    """User-presentable failure uploading a weigh-in to Garmin Connect."""


def _verify_token_owner(garmin, token_dir: str, expected_email: str) -> None:
    """Refuse to upload through tokens that belong to a different account.

    Same two-step identity check as the fetcher: the profile userName when it
    is email-form, else the owner marker the fetcher wrote at credential
    login. Unverifiable ownership only warns (single-user setups without a
    marker), but a positive mismatch is a hard error — uploading a weigh-in
    to someone else's Garmin account is worse than failing.
    """
    if not expected_email:
        return
    expected = expected_email.strip().lower()

    try:
        profile = garmin.connectapi("/userprofile-service/socialProfile") or {}
        username = str(profile.get("userName") or "")
    except Exception as err:
        logger.debug("Could not read profile userName for ownership check: %s", err)
        username = ""

    if "@" in username:
        actual = username.strip().lower()
    else:
        try:
            with open(os.path.join(os.path.expanduser(token_dir), _TOKEN_OWNER_FILE)) as f:
                actual = f.read().strip().lower() or None
        except OSError:
            actual = None

    if actual is None:
        logger.warning(
            "Cannot verify which Garmin account the tokens in '%s' belong to — "
            "proceeding with upload for '%s'.", token_dir, expected,
        )
        return
    if actual != expected:
        raise GarminUploadError(
            f"The Garmin tokens in '{token_dir}' belong to '{actual}', not "
            f"'{expected}' — refusing to upload a weigh-in to another user's "
            "account. Check this user's TOKEN_DIR."
        )


def upload_body_composition(
    token_dir: str,
    expected_email: str,
    *,
    timestamp: str | None = None,
    weight_kg: float,
    percent_fat: float | None = None,
    percent_hydration: float | None = None,
    muscle_mass_kg: float | None = None,
    bone_mass_kg: float | None = None,
    visceral_fat_rating: float | None = None,
    metabolic_age: float | None = None,
    physique_rating: float | None = None,
    bmi: float | None = None,
) -> None:
    """Push one weigh-in to Garmin Connect via the stored fetcher tokens.

    Raises GarminUploadError with a user-presentable message on any failure.
    """
    try:
        from garminconnect import Garmin
    except ImportError as err:  # insights installed without garmin-grafana's deps
        raise GarminUploadError(
            "The 'garminconnect' package is not installed in this environment — "
            "install garmin-grafana alongside garmin-insights to enable uploads."
        ) from err

    if not token_dir:
        raise GarminUploadError(
            "No TOKEN_DIR configured for this user — add it to their "
            "users/<name>.env (same directory the fetcher uses)."
        )
    if not os.path.isdir(os.path.expanduser(token_dir)):
        raise GarminUploadError(
            f"Garmin token directory '{token_dir}' does not exist yet — run the "
            "fetcher once so it logs in and stores tokens."
        )

    try:
        garmin = Garmin()
        garmin.login(token_dir)
    except Exception as err:
        raise GarminUploadError(
            f"Garmin token login failed ({err}). The stored tokens may have "
            "expired — the fetcher will refresh them on its next run."
        ) from err

    _verify_token_owner(garmin, token_dir, expected_email)

    def _fit(v, scale):
        """Pre-quantize to the FIT field grid. The encoder does int(v*scale),
        which truncates — 72.57*100 is 7256.999… in floats and lands as 72.56,
        and 24.8 years becomes 24. Round to the grid and nudge past the float
        representation error so the packed integer is exact."""
        if v is None:
            return None
        return round(v * scale) / scale + 1e-9

    try:
        garmin.add_body_composition(
            timestamp,
            weight=_fit(weight_kg, 100),
            percent_fat=_fit(percent_fat, 100),
            percent_hydration=_fit(percent_hydration, 100),
            muscle_mass=_fit(muscle_mass_kg, 100),
            bone_mass=_fit(bone_mass_kg, 100),
            visceral_fat_rating=_fit(visceral_fat_rating, 1),
            metabolic_age=_fit(metabolic_age, 1),
            physique_rating=_fit(physique_rating, 1),
            bmi=_fit(bmi, 10),
        )
    except Exception as err:
        raise GarminUploadError(f"Garmin Connect rejected the upload: {err}") from err
    logger.info(
        "Uploaded weigh-in (%.1f kg) to Garmin Connect for %s",
        weight_kg, expected_email or "<unknown>",
    )
