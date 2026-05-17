"""Configuration loaded from .env file using pydantic-settings."""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All application settings, loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
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

    @property
    def scan_time_list(self) -> list[str]:
        return [t.strip() for t in self.scan_times.split(",") if t.strip()]


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
