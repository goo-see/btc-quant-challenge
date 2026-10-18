"""LIVE preparation: execution metrics, LIVE book initialisation from exchange truth,
challenge_started_at vs live_activated_at, no-signal-no-order, launchd mode rendering.
Uses a fake LIVE-shaped gateway — NEVER the real Upbit client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.config.settings import EnvSettings, Settings, load_yaml_config
from app.core.state import BotState
from app.core.types import (
    Action,
    Fill,
    OrderRecord,
    OrderState,
    OrdType,
    Side,
    Signal,
    Timeframe,
    TradingMode,
)
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.exchange.base import Balances, GatewayOrder
from app.monitoring.slippage import build_execution_record
from app.services.trader_service import TraderService
from tests.integration.test_trader_service import FakeFeed, Probe, _tick
from tests.unit.test_strategy import PARAMS

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config" / "config.yaml"
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


class FakeLiveGateway:
    """Exchange-shaped gateway: fills market orders instantly; counts every order call."""

    name = "fake-live"

    def __init__(self, krw: Decimal = Decimal(100039), btc: Decimal = Decimal(0)) -> None:
        self.krw, self.btc = krw, btc
        self.open: list[GatewayOrder] = []
        self.orders: dict[str, GatewayOrder] = {}
        self.place_calls = 0
        self.price = Decimal(115_000_000)
        self.clock = datetime.now(UTC)  # exchange fill timestamp (tests advance it explicitly)

    async def place_market_buy(self, client_order_id: str, krw_amount: Decimal) -> GatewayOrder:
        self.place_calls += 1
        px = self.price * Decimal("1.0002")
        vol = (krw_amount / px).quantize(Decimal("0.00000001"))
        fee = (krw_amount * Decimal("0.0005")).quantize(Decimal("0.01"))
        fill = Fill(
            client_order_id,
            px,
            vol,
            (vol * px).quantize(Decimal("0.01")),
            fee,
            self.clock,
            "t-1",
        )
        self.krw -= fill.funds + fee
        self.btc += vol
        o = GatewayOrder(
            client_order_id,
            "u-1",
            "KRW-BTC",
            Side.BID,
            OrdType.PRICE,
            OrderState.FILLED,
            krw_amount,
            None,
            vol,
            px,
            fee,
            [fill],
        )
        self.orders[client_order_id] = o
        return o

    async def place_market_sell(self, client_order_id: str, volume: Decimal) -> GatewayOrder:
        self.place_calls += 1
        px = self.price * Decimal("0.9998")
        funds = (volume * px).quantize(Decimal("0.01"))
        fee = (funds * Decimal("0.0005")).quantize(Decimal("0.01"))
        fill = Fill(client_order_id, px, volume, funds, fee, self.clock, "t-2")
        self.krw += funds - fee
        self.btc -= volume
        o = GatewayOrder(
            client_order_id,
            "u-2",
            "KRW-BTC",
            Side.ASK,
            OrdType.MARKET,
            OrderState.FILLED,
            None,
            volume,
            volume,
            px,
            fee,
            [fill],
        )
        self.orders[client_order_id] = o
        return o

    async def get_order(
        self, *, client_order_id: str | None = None, exchange_uuid: str | None = None
    ) -> GatewayOrder:
        return self.orders[str(client_order_id)]

    async def cancel_order(
        self, *, client_order_id: str | None = None, exchange_uuid: str | None = None
    ) -> GatewayOrder:
        return self.orders[str(client_order_id)]

    async def get_open_orders(self) -> list[GatewayOrder]:
        return list(self.open)

    async def get_balances(self) -> Balances:
        return Balances(self.krw, Decimal(0), self.btc, Decimal(0))

    async def get_reference_price(self) -> Decimal:
        return self.price

    async def ping(self) -> bool:
        return True


def _live_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    for k in ("UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY", "LIVE_CONFIRM"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_yaml_config(CONFIG)
    cfg.system.database_url = f"sqlite+aiosqlite:///{tmp_path}/live.db"
    cfg.system.warmup_days = 0
    cfg.system.reconcile_interval_sec = 0
    cfg.strategy.version_file = None
    cfg.strategy.name = "trend_momentum"
    cfg.strategy.version = ""
    cfg.strategy.timeframe = "15m"
    cfg.strategy.trend_timeframe = "1h"
    cfg.strategy.params = dict(PARAMS)
    cfg.risk.cooldown_after_loss_min = 0
    # the shipped (public) config leaves the whitelist IP empty; LIVE test scenarios need one
    cfg.live_gate.static_public_ip = "1.2.3.4"
    return Settings(EnvSettings(_env_file=None), cfg)


async def _live_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gw: FakeLiveGateway
) -> tuple[TraderService, Repository]:
    s = _live_settings(monkeypatch, tmp_path)
    engine = create_engine(s.config.system.database_url)
    await init_schema(engine)
    repo = Repository(engine)
    # a PAPER book left behind by the previous trader must be archived, never reused
    await repo.set_state(
        "portfolio",
        {
            "state": {
                "initial_equity": "100000",
                "krw": "70000",
                "btc": "0.0003",
                "avg_entry": "100000000",
                "entry_fees": "15",
                "entry_time": None,
                "highest_since_entry": None,
                "realized_pnl": "1234",
                "fees_total": "30",
                "hwm": "101000",
                "max_drawdown": "0.01",
                "trade_count": 3,
            },
            "open_trade_id": None,
            "stop_price": None,
            "entry_reason": "",
            "consecutive_losses": 0,
            "last_loss_at": None,
            "daily": {
                "day_key": "2026-09-29",
                "start_equity": "100000",
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "fees": "0",
                "realized": "0",
            },
        },
    )
    svc = TraderService(s, repo, gw, feed_factory=FakeFeed, probe=Probe(), run_id="live-t")
    return svc, repo


def _buy_signal(price: float, stop: float, atr: float) -> Signal:
    return Signal(
        Action.BUY,
        1.0,
        "BREAKOUT>1 TREND_UP",
        T0,
        "trend_momentum",
        Timeframe.M15,
        {"close": price, "atr": atr, "channel_upper": price - 1},
        Decimal(str(stop)),
    )


# ----------------------------------------------------------------------------- metrics
def test_execution_record_buy_and_sell_fields() -> None:
    now = datetime.now(UTC)
    buy = OrderRecord(
        "c-1",
        TradingMode.LIVE,
        "KRW-BTC",
        Side.BID,
        OrdType.PRICE,
        OrderState.FILLED,
        now,
        now,
        req_krw=Decimal(30000),
        signal_id=7,
        submitted_at=now,
        db_id=11,
    )
    buy.fills = [
        Fill(
            "c-1",
            Decimal(115_050_000),
            Decimal("0.00026"),
            Decimal("29913"),
            Decimal("14.96"),
            now,
            "t1",
        )
    ]
    sig = _buy_signal(115_000_000, 112_000_000, 1_000_000)
    row = build_execution_record(
        rec=buy,
        sig=sig,
        strategy_version="trend_following_4h_v1",
        reference_price_at_send=Decimal(115_020_000),
        atr_trail_mult=4.0,
    )
    assert row["side"] == "BUY" and row["signal_price"] == Decimal(115_000_000)
    assert row["actual_fill_price"] == Decimal("115050000.00000000")
    assert row["slippage_bps"] == Decimal("4.35")  # (115.05M-115M)/115M*1e4
    assert row["slippage_vs_reference_bps"] == Decimal("2.61")
    assert row["initial_stop"] == Decimal(112_000_000)
    assert row["stop_distance_from_signal"] == Decimal("0.026087")
    assert row["stop_distance_from_fill"] == Decimal("0.026510")
    assert (
        row["atr"] == Decimal(1_000_000)
        and row["fee"] == Decimal("14.96")
        and row["signal_id"] == 7
    )
    assert row["order_id"] == 11 and row["quantity"] == Decimal("0.00026")

    sell = OrderRecord(
        "c-2",
        TradingMode.LIVE,
        "KRW-BTC",
        Side.ASK,
        OrdType.MARKET,
        OrderState.FILLED,
        now,
        now,
        req_volume=Decimal("0.00026"),
        submitted_at=now,
        db_id=12,
    )
    sell.fills = [
        Fill(
            "c-2",
            Decimal(111_900_000),
            Decimal("0.00026"),
            Decimal("29094"),
            Decimal("14.55"),
            now + timedelta(hours=10),
            "t2",
        )
    ]
    stop_sig = Signal(
        Action.SELL,
        1.0,
        "STOP@112000000 (price 111950000)",
        now,
        "trend_momentum",
        Timeframe.M15,
        {"tick_price": 111_950_000.0, "trigger_price": 112_000_000.0},
        Decimal(112_000_000),
    )
    row2 = build_execution_record(
        rec=sell,
        sig=stop_sig,
        strategy_version="v",
        reference_price_at_send=Decimal(111_950_000),
        atr_trail_mult=4.0,
        realized_pnl=Decimal("-849.51"),
        entry_price=Decimal(115_050_000),
        entry_time=now,
    )
    assert row2["side"] == "SELL" and row2["exit_reason"].startswith("STOP")
    assert row2["exit_trigger_price"] == Decimal(112_000_000)
    assert row2["signal_price"] == Decimal(111_950_000)  # tick price for stop exits
    assert row2["slippage_bps"] == Decimal("4.47")  # adverse: sold below the tick price
    assert row2["realized_pnl"] == Decimal("-849.51") and row2["holding_sec"] == 36000
    assert row2["return_pct"] == Decimal("-2.8399")  # -849.51 / (115.05M * 0.00026)


# ----------------------------------------------------------------------------- LIVE books
async def test_live_first_start_books_from_exchange_and_archives_paper_book(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gw = FakeLiveGateway(krw=Decimal(100039))
    svc, repo = await _live_service(monkeypatch, tmp_path, gw)
    await svc.start(run_loops=False)
    assert svc.portfolio.state.krw == Decimal(100039) and svc.portfolio.state.btc == 0
    assert svc.portfolio.state.initial_equity == Decimal(100039)
    ch = await repo.get_state("challenge") or {}
    assert ch["status"] == "LIVE_ACTIVATED_WAITING_FIRST_BUY"
    assert ch["initial_challenge_capital"] == "100000" and ch["live_start_krw"] == "100039"
    assert ch["live_activated_at"] and "challenge_started_at" not in ch
    keys = [k for k in await _state_keys(repo) if k.startswith("portfolio_archive_")]
    assert len(keys) == 1
    archived = await repo.get_state(keys[0]) or {}
    assert archived["state"]["btc"] == "0.0003" and archived["archived_mode"] == "PAPER"
    assert await repo.get_state("strategy_epoch_LIVE")
    # second start of the same epoch: NO re-init (books persist, no second archive)
    s2 = TraderService(
        svc.settings, repo, gw, feed_factory=FakeFeed, probe=Probe(), run_id="live-t2"
    )
    await s2.start(run_loops=False)
    assert s2.portfolio.state.initial_equity == Decimal(100039)
    assert len([k for k in await _state_keys(repo) if k.startswith("portfolio_archive_")]) == 1
    await svc.stop()
    await s2.stop()


async def _state_keys(repo: Repository) -> list[str]:
    from sqlalchemy import select

    from app.database.models import BotStateRow

    async with repo._factory() as s:
        return list((await s.execute(select(BotStateRow.key))).scalars())


@pytest.mark.parametrize(
    ("krw", "btc", "open_n", "reason"),
    [
        (Decimal(100039), Decimal("0.0001"), 0, "EXCHANGE_BTC_NOT_ZERO"),
        (Decimal(100039), Decimal(0), 1, "EXCHANGE_OPEN_ORDERS"),
        (Decimal(10039), Decimal(0), 0, "CHALLENGE_CAPITAL_NOT_FUNDED"),
        (Decimal(100600), Decimal(0), 0, "CHALLENGE_CAPITAL_NOT_FUNDED"),
    ],
)
async def test_live_start_blocked_unless_flat_and_funded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    krw: Decimal,
    btc: Decimal,
    open_n: int,
    reason: str,
) -> None:
    gw = FakeLiveGateway(krw=krw, btc=btc)
    if open_n:
        gw.open = [
            GatewayOrder(
                "x", "u", "KRW-BTC", Side.BID, OrdType.PRICE, OrderState.OPEN, Decimal(6000), None
            )
        ]
    svc, repo = await _live_service(monkeypatch, tmp_path, gw)
    with pytest.raises(SystemExit, match=reason):
        await svc.start(run_loops=False)
    assert gw.place_calls == 0
    assert (
        await repo.get_state("strategy_epoch_LIVE")
    ) is not None  # epoch row is harmless; books untouched
    assert (await repo.get_state("portfolio") or {})["state"][
        "btc"
    ] == "0.0003"  # PAPER book not overwritten


# ----------------------------------------------------------------------------- no signal, no order
async def test_live_no_signal_no_order_then_natural_buy_records_metrics_and_challenge_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gw = FakeLiveGateway()
    svc, repo = await _live_service(monkeypatch, tmp_path, gw)
    await svc.start(run_loops=False)
    await svc.on_ws_message(_tick(Decimal(115_000_000), T0))  # marks market data fresh
    hold = Signal(
        Action.HOLD,
        0.0,
        "INSIDE_CHANNEL",
        T0,
        "trend_momentum",
        Timeframe.M15,
        {"close": 115_000_000.0},
    )
    for _ in range(5):
        await svc.handle_signal(hold)
    assert gw.place_calls == 0
    # startup state is MAINTENANCE (FROZEN): even a BUY signal must not reach the exchange
    assert svc.sm.state.value == BotState.MAINTENANCE.value
    await svc.handle_signal(_buy_signal(115_000_000, 112_000_000, 1_000_000))
    assert gw.place_calls == 0
    # recovery checks pass -> RUNNING -> a natural BUY executes exactly one exchange order
    await svc.supervisor_tick(T0 + timedelta(seconds=1))
    assert svc.sm.state.value == BotState.RUNNING.value
    live_activated = (await repo.get_state("challenge") or {})["live_activated_at"]
    gw.clock = T0
    await svc.handle_signal(_buy_signal(115_000_000, 112_000_000, 1_000_000))
    assert gw.place_calls == 1
    assert svc.portfolio.state.has_position
    assert (
        Decimal(29_000) <= svc.portfolio.state.btc * Decimal(115_000_000) <= Decimal(30_100)
    )  # 30% of ~₩100k, cap ₩30,000
    rows = await repo.recent_execution_metrics(TradingMode.LIVE)
    assert len(rows) == 1 and rows[0].side == "BUY" and rows[0].slippage_bps == Decimal("2.00")
    assert rows[0].initial_stop == Decimal(112_000_000) and rows[
        0
    ].reference_price_at_send == Decimal(115_000_000)
    ch = await repo.get_state("challenge") or {}
    assert (
        ch["status"] == "STARTED"
        and ch["challenge_started_at"]
        and ch["live_activated_at"] == live_activated
    )
    assert ch["challenge_started_at"] != ch["live_activated_at"]
    # a second BUY while long is blocked before the exchange (no pyramiding)
    await svc.handle_signal(_buy_signal(116_000_000, 112_000_000, 1_000_000))
    assert gw.place_calls == 1
    # protective stop via ticker -> exactly one SELL, SELL metrics with exit economics
    gw.clock = T0 + timedelta(hours=6)
    await svc.on_ws_message(_tick(Decimal(111_900_000), T0 + timedelta(hours=6)))
    assert gw.place_calls == 2 and svc.portfolio.state.btc == 0
    rows = await repo.recent_execution_metrics(TradingMode.LIVE)
    sell = next(r for r in rows if r.side == "SELL")
    assert sell.exit_reason and sell.exit_reason.startswith("STOP@112000000")
    assert (
        sell.exit_trigger_price == Decimal(112_000_000)
        and sell.realized_pnl is not None
        and sell.realized_pnl < 0
    )
    assert sell.holding_sec == 6 * 3600
    await svc.stop()


# ----------------------------------------------------------------------------- launchd / scripts
def test_trader_plist_mode_is_rendered_not_hardcoded() -> None:
    plist = (ROOT / "scripts/launchd/com.quant.trader.plist").read_text()
    assert "<string>__TRADING_MODE__</string>" in plist
    assert "<string>LIVE</string>" not in plist and "<string>PAPER</string>" not in plist
    installer = (ROOT / "scripts/launchd_install.sh").read_text()
    assert 'MODE="${TRADING_MODE:-PAPER}"' in installer and "__TRADING_MODE__" in installer


def test_live_transition_script_checks_every_gate_before_switching() -> None:
    s = (ROOT / "scripts/live_transition.sh").read_text()
    for needle in (
        "live_approved: true",
        "enabled: true",
        "TRADING_MODE=LIVE",
        "LIVE_CONFIRM=I_UNDERSTAND_THIS_USES_REAL_KRW",
        "private_smoke.py",
        "launchctl bootout",
        "pgrep -f",
        'TRADING_MODE=LIVE "$ROOT/scripts/launchd_install.sh" install com.quant.trader',
    ):
        assert needle in s
    assert "create_order" not in s and "orders/test" not in s.replace("orders/test dry run", "")


def _unused(_: Any) -> None:  # keep Any import meaningful for mypy strictness on helpers
    return None


async def test_live_restart_with_open_position_restores_and_places_no_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gw = FakeLiveGateway()
    svc, repo = await _live_service(monkeypatch, tmp_path, gw)
    await svc.start(run_loops=False)
    await svc.on_ws_message(_tick(Decimal(115_000_000), T0))
    await svc.supervisor_tick(T0 + timedelta(seconds=1))
    gw.clock = T0
    await svc.handle_signal(_buy_signal(115_000_000, 112_000_000, 1_000_000))
    assert gw.place_calls == 1 and svc.portfolio.state.has_position
    stop_before, qty_before = svc.portfolio.stop_price, svc.portfolio.state.btc
    await svc.stop()
    # crash/restart: exchange still holds the BTC -> books, stop and challenge state restored,
    # LIVE book NOT re-initialised, no order placed, reconciliation OK
    s2 = TraderService(svc.settings, repo, gw, feed_factory=FakeFeed, probe=Probe(), run_id="r2")
    await s2.start(run_loops=False)
    assert s2.portfolio.state.btc == qty_before and s2.portfolio.stop_price == stop_before
    assert s2.portfolio.state.has_position and gw.place_calls == 1
    ch = await repo.get_state("challenge") or {}
    assert ch["status"] == "STARTED" and ch["challenge_started_at"]
    await s2.on_ws_message(_tick(Decimal(115_500_000), T0 + timedelta(hours=1)))
    await s2.supervisor_tick(T0 + timedelta(hours=1, seconds=1))
    assert s2.sm.state.value == BotState.RUNNING.value
    assert s2.reconciler.last is not None and s2.reconciler.last.ok
    assert gw.place_calls == 1
    await s2.stop()


def test_funding_gate_applies_only_before_live_activation() -> None:
    from app.services.live_preflight import funding_check_required

    assert funding_check_required({}) and funding_check_required({"status": "NOT_STARTED"})
    assert not funding_check_required({"status": "LIVE_ACTIVATED_WAITING_FIRST_BUY"})
    assert not funding_check_required({"status": "STARTED"})
