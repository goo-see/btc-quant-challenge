"""Trader process entry point (PAPER on Day 2; LIVE gateway is wired on Day 4)."""

from __future__ import annotations

import asyncio
import signal
from decimal import Decimal

from app.config.live_guard import assert_mode_is_safe
from app.config.settings import Settings
from app.core.logging import configure_logging, get_logger, shutdown_logging
from app.core.types import TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.exchange.upbit.rest_client import UpbitRestClient
from app.exchange.upbit.ws_client import MessageHandler, Subscription, UpbitWebSocketClient
from app.execution.fill_model import FillParams
from app.execution.paper_gateway import PaperGateway
from app.services.trader_service import TraderService

log = get_logger("main")


def build_paper_service(
    settings: Settings, repo: Repository, rest: UpbitRestClient
) -> TraderService:
    cfg = settings.config
    gateway = PaperGateway(
        market=cfg.system.market,
        initial_krw=cfg.challenge.initial_capital_krw,
        fill_params=FillParams(fee_rate=cfg.risk.fee_rate, slippage_bps=cfg.risk.slippage_bps),
        min_order_krw=cfg.risk.min_order_krw,
    )

    def feed_factory(handler: MessageHandler) -> UpbitWebSocketClient:
        return UpbitWebSocketClient(
            [
                Subscription("ticker", [cfg.system.market]),
                Subscription("candle.1m", [cfg.system.market]),
            ],
            handler,
            ping_interval=cfg.market_data.ws_ping_interval_sec,
            backoff_initial=cfg.market_data.ws_backoff_initial_sec,
            backoff_max=cfg.market_data.ws_backoff_max_sec,
        )

    return TraderService(settings, repo, gateway, feed_factory=feed_factory, rest=rest)


async def build_live_service(
    settings: Settings, repo: Repository
) -> tuple[TraderService, UpbitRestClient]:
    """LIVE wiring. Reached only after assert_mode_is_safe() (static gates) — and it re-checks the
    runtime evidence gates (readiness) plus a fresh preflight before returning."""
    from app.exchange.upbit.live_gateway import LiveGateway
    from app.monitoring.readiness import evaluate_readiness
    from app.services.live_preflight import run_preflight

    readiness = await evaluate_readiness(settings, repo)
    if not readiness.live_allowed:
        raise SystemExit(f"LIVE_BLOCKED reason={readiness.block_reason}")
    for item in readiness.items:
        if item.status == "WARN":
            log.warning(
                "live_readiness_warning", item=item.name, value=item.value, detail=item.detail
            )
    pre = await run_preflight(settings, repo)
    if not pre.ok:
        raise SystemExit(f"LIVE_BLOCKED reason={pre.blocked_reason}")
    env = settings.env
    assert env.upbit_access_key is not None and env.upbit_secret_key is not None
    rest = UpbitRestClient(
        env.upbit_access_key.get_secret_value(), env.upbit_secret_key.get_secret_value()
    )
    cfg = settings.config
    gateway = LiveGateway(rest, cfg.system.market)

    def feed_factory(handler: MessageHandler) -> UpbitWebSocketClient:
        return UpbitWebSocketClient(
            [
                Subscription("ticker", [cfg.system.market]),
                Subscription("candle.1m", [cfg.system.market]),
            ],
            handler,
            ping_interval=cfg.market_data.ws_ping_interval_sec,
            backoff_initial=cfg.market_data.ws_backoff_initial_sec,
            backoff_max=cfg.market_data.ws_backoff_max_sec,
        )

    service = TraderService(settings, repo, gateway, feed_factory=feed_factory, rest=rest)
    # exchange-reported terms (orders/chance, stored by the preflight) override config when stricter
    terms = await repo.get_state("exchange_terms") or {}
    if terms.get("min_total"):
        service.risk.min_order_krw = max(
            service.risk.min_order_krw, Decimal(str(terms["min_total"]))
        )
    fee = max(Decimal(str(terms.get("bid_fee", 0))), Decimal(str(terms.get("ask_fee", 0))))
    if fee > 0:
        service.risk.fee_rate = max(service.risk.fee_rate, fee)
    log.warning(
        "LIVE_MODE_ARMED",
        market=cfg.system.market,
        max_order_krw=str(cfg.risk.for_mode(TradingMode.LIVE).max_order_krw),
        max_position_pct=str(cfg.risk.for_mode(TradingMode.LIVE).max_position_pct),
        min_order_krw=str(service.risk.min_order_krw),
        fee_rate=str(service.risk.fee_rate),
    )
    return service, rest


async def run(settings: Settings, *, duration_sec: float | None = None) -> TraderService:
    assert_mode_is_safe(settings)
    if settings.mode is TradingMode.BACKTEST:
        raise SystemExit("TRADING_MODE=BACKTEST: use scripts/run_backtest.py")
    engine = create_engine(settings.config.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    if settings.mode is TradingMode.LIVE:
        service, rest = await build_live_service(settings, repo)
    else:
        rest = UpbitRestClient()
        service = build_paper_service(settings, repo, rest)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await service.start()
    log.info("trader_running", mode=settings.mode.value, run_id=service.run_id)
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
        await engine.dispose()
    return service


def main() -> None:
    settings = Settings.load()
    configure_logging(settings.config.system.log_level, settings.config.system.log_dir)
    try:
        asyncio.run(run(settings))
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
