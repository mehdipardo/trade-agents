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
    # Futures testnet so agents can short (Binance Futures is geo-blocked in FR).
    # MEXC covers both crypto perpetuals AND TradFi perpetuals (stocks, indices,
    # commodities) — a single venue for the whole whitelist. Kraken Futures
    # stays supported but only sees the crypto subset.
    exchange_id: str = "mexc"
    exchange_api_key: str = ""
    exchange_secret: str = ""

    # --- Trading universe / thresholds -----------------------------------
    # Mixed crypto + TradFi (MEXC perpetuals cover both).
    asset_whitelist: str = (
        "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,"
        "GOLD/USDT,SILVER/USDT,OIL/USDT,SPX/USDT,"
        "BABA/USDT,TSLA/USDT,NVDA/USDT,AVGO/USDT,AAPL/USDT,MSFT/USDT,META/USDT"
    )
    confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    # Freshness gate: drop events whose published_at is older than this (seconds).
    # Guards against stale / re-syndicated news re-triggering trades (a broad
    # aggregator often re-surfaces old stories). 0 disables the gate. Events with
    # no published_at are treated as fresh (stamped at reception).
    #
    # 30 min, NOT hours: a high-impact print is fully priced within minutes, so a
    # story that only reaches us hours later (e.g. a CPI article syndicated 3h
    # after the release) must be dropped, not chased. This also matches the
    # economic watcher's trailing window so a legitimately delayed macro actual
    # (fired up to TRAIL_S late) still passes.
    max_news_age_s: int = Field(default=1800, ge=0)  # 30 minutes

    # --- Risk engine (see app/risk/rules.py) -----------------------------
    min_intensity: int = Field(default=3, ge=1, le=5)
    min_actionability: int = Field(default=2, ge=1, le=5)
    # Risk-based sizing: fraction of equity risked if the SL is hit. Position
    # notional = (equity * risk_per_trade_pct) / (stop_loss_pct/100). So on a
    # $1000 account, 1% risk with a 1.5% SL => ~$667 notional, -$10 at the stop.
    risk_per_trade_pct: float = Field(default=0.01, gt=0, le=0.2)
    # Hard ceiling on notional as a multiple of equity (futures leverage cap).
    max_gross_exposure: float = Field(default=3.0, gt=0)
    # Margin leverage: margin locked per position = notional / margin_leverage.
    # Higher leverage frees capital for more concurrent triggers; it does NOT
    # change the SL/TP value (those scale with notional, not margin).
    margin_leverage: int = Field(default=5, ge=1, le=50)
    max_notional_abs: float = Field(default=100.0, gt=0)  # legacy display only
    max_notional_equity_pct: float = Field(default=0.05, gt=0, le=1)
    stop_loss_pct: float = Field(default=1.5, gt=0)
    take_profit_pct: float = Field(default=3.0, gt=0)
    max_trades_per_hour: int = Field(default=6, ge=1)
    cooldown_s: int = Field(default=900, ge=0)
    daily_loss_limit_pct: float = Field(default=0.03, gt=0, le=1)
    # Paper equity used until the exchange provides a real balance (Étape 5).
    starting_equity_quote: float = Field(default=1000.0, gt=0)
    # Trading fees as a percent of notional, charged per fill. Market orders are
    # takers. Defaults reflect MEXC USDT-M futures (low). Round trip = 2x taker.
    taker_fee_pct: float = Field(default=0.02, ge=0)
    maker_fee_pct: float = Field(default=0.0, ge=0)
    # Mark open paper positions against REAL public prices (read-only, no keys)
    # so SL/TP actually trigger and PnL is real. Falls back to mock on failure.
    use_live_prices: bool = True
    price_exchange_id: str = "mexc"  # public exchange used for mark prices

    # --- Integrations ----------------------------------------------------
    slack_webhook_url: str = ""
    webhook_secret: str = "change-me"
    redis_url: str = "redis://localhost:6379/0"

    # --- Observability (optional, Étape 9 stretch) -----------------------
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"

    # --- Ingestion -------------------------------------------------------
    rss_feeds: str = ""
    rss_poll_interval_s: int = Field(default=30, ge=1)
    # Broad real-time news aggregator (free-crypto-news SSE stream). This is the
    # primary firehose; the LLM funnel triages it. Opt-in: set the SSE URL.
    aggregator_sse_url: str = ""
    # Economic-calendar watcher (opt-in: set a feed URL to start it).
    econ_calendar_url: str = ""
    # Trump / Truth Social poller (opt-in: set a statuses feed URL to start it).
    truth_social_url: str = ""
    # Watchlist: comma/newline-separated account status feeds (the "10 most
    # influential accounts"). Falls back to the single truth_social_url. Each is
    # a Mastodon-shaped .../api/v1/accounts/<id>/statuses endpoint or mirror.
    truth_social_urls: str = ""
    truth_social_poll_interval_s: int = Field(default=10, ge=2)
    # Congress.gov bill tracker (opt-in: needs a free API key + tracked bills).
    congress_api_key: str = ""
    congress_tracked_bills: str = ""  # e.g. "119/hr/1747,119/s/1582"
    congress_poll_interval_s: int = Field(default=300, ge=60)

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
