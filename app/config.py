"""Application configuration.

Loaded once from the environment via ``pydantic-settings``. This module also
enforces the non-negotiable safety guards of the project: the application MUST
run in paper-trading mode against an exchange sandbox. Any attempt to run
otherwise makes settings construction fail loudly so the app refuses to start.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Values are read from environment variables (case-insensitive) and, when
    present, from a local ``.env`` file. Unknown keys are ignored so the same
    ``.env`` can be shared with other tooling.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Runtime ---------------------------------------------------------
    app_env: str = "dev"

    # --- Safety guards (see ``_enforce_safety_guards``) ------------------
    paper_trading: bool = True
    exchange_sandbox: bool = True

    # --- LLM provider ----------------------------------------------------
    llm_provider: Literal["groq", "gemini"] = "groq"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"

    # --- Exchange --------------------------------------------------------
    exchange_id: str = "binance"
    exchange_api_key: str = ""
    exchange_secret: str = ""

    # --- Trading universe / thresholds -----------------------------------
    asset_whitelist: str = "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT"
    confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    # --- Risk engine (see app/risk/rules.py) -----------------------------
    min_intensity: int = Field(default=3, ge=1, le=5)
    max_notional_abs: float = Field(default=100.0, gt=0)
    max_notional_equity_pct: float = Field(default=0.05, gt=0, le=1)
    stop_loss_pct: float = Field(default=1.5, gt=0)
    take_profit_pct: float = Field(default=3.0, gt=0)
    max_trades_per_hour: int = Field(default=6, ge=1)
    cooldown_s: int = Field(default=900, ge=0)
    daily_loss_limit_pct: float = Field(default=0.03, gt=0, le=1)
    # Paper equity used until the exchange provides a real balance (Étape 5).
    starting_equity_quote: float = Field(default=1000.0, gt=0)

    # --- Integrations ----------------------------------------------------
    slack_webhook_url: str = ""
    webhook_secret: str = "change-me"
    redis_url: str = "redis://localhost:6379/0"

    # --- Ingestion -------------------------------------------------------
    rss_feeds: str = ""
    rss_poll_interval_s: int = Field(default=30, ge=1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def asset_whitelist_set(self) -> tuple[str, ...]:
        """Normalized, de-duplicated tuple of whitelisted symbols."""
        seen: dict[str, None] = {}
        for raw in self.asset_whitelist.split(","):
            symbol = raw.strip().upper()
            if symbol:
                seen.setdefault(symbol, None)
        return tuple(seen)

    @model_validator(mode="after")
    def _enforce_safety_guards(self) -> Settings:
        """Refuse any configuration that could trade real money.

        These guards are intentionally not overridable: there is no code path
        in this project for live trading.
        """
        violations: list[str] = []
        if self.paper_trading is not True:
            violations.append(
                "PAPER_TRADING must be 'true'. This system is paper-trading only "
                "and has no live-trading code path."
            )
        if self.exchange_sandbox is not True:
            violations.append(
                "EXCHANGE_SANDBOX must be 'true'. Orders may only be routed to an "
                "exchange sandbox/testnet."
            )
        if violations:
            raise ValueError(
                "Refusing to start due to unsafe configuration:\n  - "
                + "\n  - ".join(violations)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the ``.env`` file and environment are read exactly once.
    """
    return Settings()
