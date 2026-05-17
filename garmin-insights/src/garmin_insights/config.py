"""Configuration loaded from .env file using pydantic-settings."""

from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Claude / Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"

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
        """Return a copy of these settings with sqlite_db_path set to the user's DB."""
        db_path = self.user_map.get(user_id)
        if not db_path:
            raise ValueError(f"Unknown user: {user_id}")
        return self.model_copy(update={"sqlite_db_path": db_path})


def get_settings() -> Settings:
    """Return a Settings instance."""
    return Settings()
