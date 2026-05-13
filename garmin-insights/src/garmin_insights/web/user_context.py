"""Per-user agent + visualization service pool.

Each user has their own SQLite database. We lazily construct a HealthAgent,
VisualizationService, and LifestyleService per user and cache them for the
lifetime of the server process.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from garmin_insights.agent import HealthAgent
from garmin_insights.config import Settings
from garmin_insights.web.lifestyle_viz import LifestyleService
from garmin_insights.web.visualizations import VisualizationService

logger = logging.getLogger(__name__)


@dataclass
class UserBundle:
    user_id: str
    agent: HealthAgent
    viz: VisualizationService
    lifestyle: LifestyleService


class UserContext:
    """Thread-safe lazy pool of per-user agent bundles."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._user_map = settings.user_map
        self._bundles: dict[str, UserBundle] = {}
        self._lock = threading.Lock()

    @property
    def user_ids(self) -> list[str]:
        return list(self._user_map.keys())

    def list_users(self) -> list[dict]:
        return [
            {"id": uid, "db_path": path}
            for uid, path in self._user_map.items()
        ]

    def has_user(self, user_id: str) -> bool:
        return user_id in self._user_map

    def get(self, user_id: str) -> UserBundle:
        if user_id not in self._user_map:
            raise KeyError(f"Unknown user: {user_id}")

        with self._lock:
            bundle = self._bundles.get(user_id)
            if bundle is not None:
                return bundle

            logger.info("Initialising agent for user '%s'", user_id)
            user_settings = self._settings.settings_for_user(user_id)
            agent = HealthAgent(user_settings)
            try:
                agent.ensure_cache_fresh(days=90)
            except Exception as e:
                logger.warning("Cache refresh for user '%s' failed: %s", user_id, e)
            viz = VisualizationService(user_settings.sqlite_db_path)
            lifestyle = LifestyleService(user_settings.sqlite_db_path)
            bundle = UserBundle(user_id=user_id, agent=agent, viz=viz, lifestyle=lifestyle)
            self._bundles[user_id] = bundle
            return bundle

    def close(self) -> None:
        with self._lock:
            for bundle in self._bundles.values():
                try:
                    bundle.agent.close()
                except Exception as e:
                    logger.warning("Error closing agent for '%s': %s", bundle.user_id, e)
            self._bundles.clear()
