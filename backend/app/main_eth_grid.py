"""ETH grid LIVE EXPERIMENT process entry point (launchd com.quant.eth-grid).

Separate from app.main_trader: own DB (eth_grid_live.db), own OrderManager on KRW-ETH, the BTC
trader's quant.db is touched ONLY through the sleeve ledger row. Static gates: TRADING_MODE=LIVE,
LIVE_CONFIRM phrase, credentials, portfolio.sleeves.ETH_GRID.mode == LIVE_EXPERIMENT.
"""

from __future__ import annotations

import asyncio
import signal
from decimal import Decimal

from app.config.live_guard import LIVE_CONFIRM_PHRASE
from app.config.settings import Settings
from app.core.logging import configure_logging, get_logger, shutdown_logging
from app.core.types import TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.eth_grid.service import EthGridService
from app.exchange.upbit.rest_client import UpbitRestClient
from app.exchange.upbit.ws_client import MessageHandler, Subscription, UpbitWebSocketClient

log = get_logger("eth_grid.main")


def assert_eth_live_allowed(settings: Settings) -> None:
    env, cfg = settings.env, settings.config
    if env.trading_mode is not TradingMode.LIVE:
        raise PermissionError("ETH_LIVE_BLOCKED reason=TRADING_MODE_NOT_LIVE")
    if env.live_confirm != LIVE_CONFIRM_PHRASE:
        raise PermissionError("ETH_LIVE_BLOCKED reason=LIVE_CONFIRM_MISSING")
    if not env.has_credentials:
        raise PermissionError("ETH_LIVE_BLOCKED reason=CREDENTIALS_MISSING")
    sl = cfg.portfolio.sleeves.get("ETH_GRID")
    if not cfg.portfolio.enabled or sl is None or sl.mode != "LIVE_EXPERIMENT":
        raise PermissionError("ETH_LIVE_BLOCKED reason=ETH_SLEEVE_NOT_LIVE_EXPERIMENT")
    if cfg.eth_grid.candidate != "G0":
        raise PermissionError("ETH_LIVE_BLOCKED reason=CANDIDATE_NOT_G0")


async def run(settings: Settings, *, duration_sec: float | None = None) -> EthGridService:
    from app.exchange.upbit.live_gateway import LiveGateway

    assert_eth_live_allowed(settings)
    cfg = settings.config
    eth_engine = create_engine(cfg.eth_grid.database_url)
    await init_schema(eth_engine)
    main_engine = create_engine(cfg.system.database_url)
    repo, main_repo = Repository(eth_engine), Repository(main_engine)
    env = settings.env
    assert env.upbit_access_key is not None and env.upbit_secret_key is not None
    rest = UpbitRestClient(
        env.upbit_access_key.get_secret_value(), env.upbit_secret_key.get_secret_value()
    )
    market = cfg.eth_grid.market
    gateway = LiveGateway(rest, market)
    chance = await rest.get_order_chance(market)
    terms = {
        "bid_fee": str(chance.bid_fee),
        "ask_fee": str(chance.ask_fee),
        "min_total": str(max(chance.market.bid.min_total, chance.market.ask.min_total)),
        "state": chance.market.state,
    }
    if chance.market.state != "active":
        raise SystemExit(f"ETH_LIVE_BLOCKED reason=MARKET_NOT_ACTIVE {chance.market.state}")

    async def balances() -> dict[str, Decimal]:
        accts = await rest.get_accounts()
        out = {a.currency: a.balance + a.locked for a in accts}
        for k in ("KRW", "BTC", "ETH"):
            out.setdefault(k, Decimal(0))
        return out

    def feed_factory(handler: MessageHandler) -> UpbitWebSocketClient:
        return UpbitWebSocketClient(
            [Subscription("ticker", [market])],
            handler,
            ping_interval=cfg.market_data.ws_ping_interval_sec,
            backoff_initial=cfg.market_data.ws_backoff_initial_sec,
            backoff_max=cfg.market_data.ws_backoff_max_sec,
        )

    service = EthGridService(
        settings,
        repo,
        main_repo,
        gateway,
        balances=balances,
        feed_factory=feed_factory,
        rest=rest,
        exchange_terms=terms,
    )
    log.warning(
        "ETH_LIVE_EXPERIMENT_ARMED",
        market=market,
        candidate=cfg.eth_grid.candidate,
        unit_krw=str(service.p.unit_krw),
        budget_krw=str(service.p.budget_krw),
        fee_rate=str(service.fee_rate),
        min_total=str(service.min_total),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await service.start()
    log.warning("eth_grid_running", status=service.status.value, enabled=service.enabled)
    try:
        if duration_sec is None:
            await stop.wait()
        else:
            await asyncio.wait_for(stop.wait(), timeout=duration_sec)
    except TimeoutError:
        pass
    finally:
        await service.stop()
        await rest.aclose()
        await eth_engine.dispose()
        await main_engine.dispose()
    return service


def main() -> None:
    settings = Settings.load()
    configure_logging(
        settings.config.system.log_level, settings.config.system.log_dir, log_file="eth_grid.jsonl"
    )
    try:
        asyncio.run(run(settings))
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
