"""Configuration: secrets from .env (pydantic-settings), parameters from config.yaml.

TRADING_MODE is read ONLY from the environment. Nothing in config.yaml can switch the mode.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.types import TradingMode
from app.portfolio.sleeves import PortfolioConfig


class EnvSettings(BaseSettings):
    # .env lives at the repo root; processes run from backend/ (uv) or the root (scripts).
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    trading_mode: TradingMode = Field(default=TradingMode.BACKTEST, alias="TRADING_MODE")
    upbit_access_key: SecretStr | None = Field(default=None, alias="UPBIT_ACCESS_KEY")
    upbit_secret_key: SecretStr | None = Field(default=None, alias="UPBIT_SECRET_KEY")
    live_confirm: str | None = Field(default=None, alias="LIVE_CONFIRM")
    dashboard_control_token: SecretStr | None = Field(default=None, alias="DASHBOARD_CONTROL_TOKEN")
    config_path: Path = Field(default=Path("../config/config.yaml"), alias="CONFIG_PATH")
    api_host: str = Field(default="127.0.0.1", alias="API_HOST")
    api_port: int = Field(default=8787, alias="API_PORT")

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v

    @property
    def has_credentials(self) -> bool:
        return bool(
            self.upbit_access_key
            and self.upbit_secret_key
            and self.upbit_access_key.get_secret_value()
            and self.upbit_secret_key.get_secret_value()
        )


class SystemConfig(BaseModel):
    market: str = "KRW-BTC"
    report_timezone: str = "Asia/Seoul"
    heartbeat_sec: int = 5
    snapshot_interval_sec: int = 60
    reconcile_interval_sec: int = 300
    database_url: str = "sqlite+aiosqlite:///../data/quant.db"
    candle_store_dir: Path = Path("../data/candles")
    log_dir: Path = Path("../logs")
    reports_dir: Path = Path("../reports")
    log_level: str = "INFO"
    warmup_days: int = 12
    warmup_days_1h: int = 400  # 1h history replayed for 4h/1d strategies (1d EMA100 needs ~100d+)
    command_poll_sec: float = 2.0
    command_ttl_sec: int = 120
    kill_command_ttl_sec: int = 600


class ChallengeConfig(BaseModel):
    initial_capital_krw: Decimal = Decimal(100000)
    goal_krw: Decimal = Decimal(1000000)
    funding_tolerance_krw: Decimal = Decimal(500)
    start_date: str | None = None


class MarketDataConfig(BaseModel):
    timeframes: list[str] = ["1m", "5m", "15m", "1h"]
    ws_ping_interval_sec: float = 30.0
    ws_backoff_initial_sec: float = 1.0
    ws_backoff_max_sec: float = 60.0
    history_years: int = 2


class StrategyConfig(BaseModel):
    version_file: str | None = None
    name: str = "trend_momentum"
    timeframe: str = "15m"
    trend_timeframe: str = "1h"
    params: dict[str, float] = Field(default_factory=dict)
    version: str = ""  # filled from the version file, else "<name>_v1"

    @property
    def strategy_version(self) -> str:
        return self.version or f"{self.name}_v1"


class StrategyStatus(BaseModel):
    research_approved: bool = False
    paper_approved: bool = False
    live_approved: bool = False


class ModeRiskConfig(BaseModel):
    max_position_pct: Decimal = Decimal("0.30")
    max_order_krw: Decimal = Decimal(30000)
    max_concurrent_positions: int = 1


class RiskConfig(BaseModel):
    by_mode: dict[TradingMode, ModeRiskConfig]
    min_order_krw: Decimal = Decimal(5000)
    fee_rate: Decimal = Decimal("0.0005")
    slippage_bps: Decimal = Decimal(3)
    daily_loss_limit_pct: Decimal = Decimal("0.03")
    max_drawdown_limit_pct: Decimal = Decimal("0.15")
    max_consecutive_losses: int = 4
    cooldown_after_loss_min: int = 60
    minimum_krw_balance: Decimal = Decimal(1000)
    max_orders_per_hour: int = 4

    def for_mode(self, mode: TradingMode) -> ModeRiskConfig:
        return self.by_mode[mode]


class KillSwitchConfig(BaseModel):
    api_error_consecutive: int = 5
    market_data_stale_sec: int = 120
    heartbeat_stale_sec: int = 30
    reconcile_krw_tolerance: Decimal = Decimal(100)
    reconcile_btc_tolerance: Decimal = Decimal("0.00001")
    local_fault_grace_multiplier: float = 2.0
    db_write_error_limit: int = 3


class LiveGateConfig(BaseModel):
    enabled: bool = False
    min_paper_hours: float = 48
    min_paper_trades: int = 10
    strategy_paper_min_hours: float = 24
    max_paper_critical_errors: int = 0
    require_reconcile_ok: bool = True
    require_order_test_ok: bool = True
    require_recent_backtest_days: int = 14
    small_live_max_order_krw: Decimal = Decimal(6000)
    backtest_min_profit_factor: float = 1.0
    backtest_min_net_return_pct: float = 0.0
    strategy_status: dict[str, StrategyStatus] = Field(default_factory=dict)
    static_public_ip: str = ""
    static_ip_min_observation_hours: float = 24
    public_ip_check_interval_sec: int = 3600
    order_test_krw: Decimal = Decimal(6000)

    def status_of(self, version: str) -> StrategyStatus:
        return self.strategy_status.get(version, StrategyStatus())


class CompoundingConfig(BaseModel):
    """Realized-profit compounding of the grid order unit (ratios from the frozen candidate)."""

    enabled: bool = False
    round_krw: Decimal = Decimal(1000)
    max_step_up_pct: Decimal = Decimal("0.25")  # per sizing update; decreases are immediate
    max_single_grid_order_krw: Decimal = Decimal(20000)  # hard cap; lifted only by user approval
    auto_rebalance: bool = False  # cross-strategy transfers never automatic


class EthGridConfig(BaseModel):
    """ETH grid LIVE EXPERIMENT process (separate launchd agent + separate DB).

    The strategy parameters are NOT here: they are the frozen research candidate (G0) imported
    from app.research.eth_grid.engine so backtest and LIVE share one definition."""

    candidate: str = "G0"
    market: str = "KRW-ETH"
    database_url: str = "sqlite+aiosqlite:///../data/eth_grid_live.db"
    eth_market_db: Path = Path("../data/eth_market.db")  # read-only daily EMA100 warm-up
    heartbeat_sec: int = 5
    reconcile_interval_sec: int = 300
    trend_refresh_sec: int = 3600
    market_data_stale_sec: int = 120
    max_orders_per_hour: int = 30  # runaway guard (research: < 1 order/day on average)
    max_orders_per_day: int = 60
    api_error_consecutive: int = 5
    db_write_error_limit: int = 3
    reconcile_fail_limit: int = 2  # consecutive FAILs before FROZEN
    compounding: CompoundingConfig = CompoundingConfig()
    # Active rule set for NEW buys. Changing it is a LIVE strategy change (explicit user approval):
    # the process cuts over at the first real tick after start; existing lots become EXIT_ONLY.
    strategy_epoch: str = "G0"
    active_gap_pct: Decimal | None = None  # None -> the candidate's gap
    legacy_epoch_label: str = "LEGACY_G0_075"


class AppConfig(BaseModel):
    system: SystemConfig = SystemConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    eth_grid: EthGridConfig = EthGridConfig()
    challenge: ChallengeConfig = ChallengeConfig()
    market_data: MarketDataConfig = MarketDataConfig()
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig
    kill_switch: KillSwitchConfig = KillSwitchConfig()
    live_gate: LiveGateConfig = LiveGateConfig()


def load_yaml_config(path: Path) -> AppConfig:
    with path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    cfg = AppConfig.model_validate(raw)
    vf = cfg.strategy.version_file
    if vf:
        vpath = (path.parent / vf).resolve()
        with vpath.open("r", encoding="utf-8") as f:
            frozen: dict[str, Any] = yaml.safe_load(f) or {}
        cfg.strategy = StrategyConfig(
            version_file=vf,
            name=str(frozen["strategy"]),
            timeframe=str(frozen["timeframe"]),
            trend_timeframe=str(frozen.get("trend_timeframe") or "")
            or cfg.strategy.trend_timeframe,
            params={k: float(v) for k, v in dict(frozen["params"]).items()},
            version=str(frozen["strategy_version"]),
        )
    return cfg


class Settings:
    """Merged view. `env` holds secrets/mode, `config` holds parameters."""

    def __init__(self, env: EnvSettings, config: AppConfig) -> None:
        self.env = env
        self.config = config

    @property
    def mode(self) -> TradingMode:
        return self.env.trading_mode

    @classmethod
    def load(cls, config_path: Path | None = None) -> Settings:
        env = EnvSettings()
        path = config_path or env.config_path
        return cls(env, load_yaml_config(path))
