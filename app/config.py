"""Application configuration.

All values can be overridden through environment variables, which keeps the
container image free of deployment specific settings (see README.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # SQLite database file. Inside Compose this points at a mounted volume.
    db_path: str = os.getenv("DB_PATH", "/data/licenses.db")
    # Host/port used by uvicorn.
    host: str = os.getenv("APP_HOST", "0.0.0.0")
    port: int = int(os.getenv("APP_PORT", "8000"))
    # Create schema on startup if it does not exist yet.
    init_db: bool = _as_bool(os.getenv("INIT_DB"), default=True)
    # Seed a small, fixed demo dataset on first boot (idempotent).
    seed_demo: bool = _as_bool(os.getenv("SEED_DEMO"), default=True)
    # Print resolved configuration at startup (useful for container logs).
    echo: bool = _as_bool(os.getenv("CONFIG_ECHO"), default=True)

    @property
    def base_url(self) -> str:
        host = "localhost" if self.host in {"0.0.0.0", "::"} else self.host
        return f"http://{host}:{self.port}"


settings = Settings()
