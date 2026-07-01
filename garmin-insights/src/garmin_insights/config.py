"""Configuration loaded from .env file using pydantic-settings."""

from __future__ import annotations

from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_VALID_EFFORT = {"low", "medium", "high", "xhigh", "max"}


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Settings(BaseSettings):
    """All application settings, loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database (single-user fallback / default user DB)
    sqlite_db_path: str = "garmin.db"

    # User identity (shown in the web UI; derived from the Garmin login)
    garminconnect_email: str = ""
    display_name: str = ""
    biological_sex: str = ""

    # Directory containing per-user env files (e.g. users/dan.env). Looked up
    # by settings_for_user() to resolve the right display name / email / sex.
    users_dir: str = "users"

    # Claude / Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-5"
    # Reasoning depth / cost lever for adaptive-thinking models: one of
    # low | medium | high | xhigh | max. Lower effort = fewer thinking/output
    # tokens = lower cost (output tokens are the pricey side), at the cost of
    # shallower analysis. Ignored on the legacy budget-thinking models, which
    # don't support the effort parameter.
    #
    # Env var is INSIGHTS_EFFORT, NOT CLAUDE_EFFORT: Claude Code sets
    # CLAUDE_EFFORT=xhigh in its own shell, so a CLAUDE_EFFORT-named setting
    # would be silently overridden to xhigh whenever the server is (re)started
    # from a Claude Code session — the opposite of the intended low default.
    claude_effort: str = Field(default="low", validation_alias="INSIGHTS_EFFORT")

    @field_validator("claude_effort")
    @classmethod
    def _normalize_effort(cls, v: str) -> str:
        # Coerce a bad/typo'd value to "low" rather than 400 every API call.
        v = (v or "").strip().lower()
        return v if v in _VALID_EFFORT else "low"

    # Web server
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    # Scheduler
    scan_times: str = "06:00,12:00,18:00,22:00"

    # Multi-user mode (optional): comma-separated "user_id:db_path" pairs.
    # When empty, the app runs single-user with sqlite_db_path as the
    # "default" user. Example: USERS="dan:/data/dan.db,helen:/data/helen.db"
    users: str = ""

    @property
    def scan_time_list(self) -> list[str]:
        return [t.strip() for t in self.scan_times.split(",") if t.strip()]

    @property
    def user_map(self) -> dict[str, str]:
        """Parse USERS env var into {user_id: db_path}.

        Falls back to {"default": sqlite_db_path} when USERS is empty.
        """
        if not self.users.strip():
            return {"default": self.sqlite_db_path}

        result: dict[str, str] = {}
        for entry in self.users.split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            user_id, db_path = entry.split(":", 1)
            user_id = user_id.strip()
            db_path = db_path.strip()
            if user_id and db_path:
                result[user_id] = db_path
        if not result:
            return {"default": self.sqlite_db_path}
        return result

    def settings_for_user(self, user_id: str) -> "Settings":
        """Return a copy of these settings with the user's DB path AND their
        identity fields (display name, email, biological sex) overlaid from
        users/<user_id>.env so each user gets the right header + AI persona."""
        db_path = self.user_map.get(user_id)
        if not db_path:
            raise ValueError(f"Unknown user: {user_id}")
        updates: dict[str, str] = {"sqlite_db_path": db_path}
        env_path = Path(self.users_dir) / f"{user_id}.env"
        per_user = _parse_env_file(env_path)
        if per_user.get("DISPLAY_NAME"):
            updates["display_name"] = per_user["DISPLAY_NAME"]
        if per_user.get("GARMINCONNECT_EMAIL"):
            updates["garminconnect_email"] = per_user["GARMINCONNECT_EMAIL"]
        if per_user.get("BIOLOGICAL_SEX"):
            updates["biological_sex"] = per_user["BIOLOGICAL_SEX"]
        return self.model_copy(update=updates)


def get_settings() -> Settings:
    """Return a Settings instance."""
    return Settings()
