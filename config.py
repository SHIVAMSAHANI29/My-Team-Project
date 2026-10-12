"""Configuration for ChronosMatch.

Two kinds of settings live here:

* **Domain limits** (module-level constants) used by the core engine to
  validate orders. They are deliberately *not* read from the environment, so
  the engine behaves identically everywhere.
* **Runtime settings** (:class:`Settings`) read from environment variables or
  an optional ``.env`` file. Used by the API/persistence layers (Phase 3).

No credentials are stored or required: this is a simulator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

# ---------------------------------------------------------------------------
# Domain limits (core engine)
# ---------------------------------------------------------------------------
PRICE_DECIMAL_PLACES = 4
MAX_PRICE = Decimal("1000000")
MAX_QUANTITY = 1_000_000_000
SYMBOL_PATTERN = r"[A-Z0-9]{1,10}"
ORDER_ID_PATTERN = r"[A-Za-z0-9_.\-]{1,64}"

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Runtime settings
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from *path* into ``os.environ`` (no overriding)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings. All values have safe local-development defaults."""

    host: str = "127.0.0.1"
    port: int = 8000
    db_path: Path = PROJECT_ROOT / "chronosmatch.db"
    log_level: str = "INFO"
    max_request_bytes: int = 16_384
    rate_limit_per_minute: int = 600
    ws_queue_size: int = 100
    default_trade_limit: int = 50


def load_settings(env_file: Path | None = None) -> Settings:
    """Build :class:`Settings` from the environment (and ``.env`` if present)."""
    _load_dotenv(env_file if env_file is not None else PROJECT_ROOT / ".env")
    env = os.environ
    db_path = Path(env.get("CHRONOS_DB_PATH", "chronosmatch.db"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    return Settings(
        host=env.get("CHRONOS_HOST", "127.0.0.1"),
        port=int(env.get("CHRONOS_PORT", "8000")),
        db_path=db_path,
        log_level=env.get("CHRONOS_LOG_LEVEL", "INFO").upper(),
        max_request_bytes=int(env.get("CHRONOS_MAX_REQUEST_BYTES", "16384")),
        rate_limit_per_minute=int(env.get("CHRONOS_RATE_LIMIT_PER_MINUTE", "600")),
        ws_queue_size=int(env.get("CHRONOS_WS_QUEUE_SIZE", "100")),
        default_trade_limit=int(env.get("CHRONOS_DEFAULT_TRADE_LIMIT", "50")),
    )
