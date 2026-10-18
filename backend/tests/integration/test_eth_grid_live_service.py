"""ETH grid LIVE EXPERIMENT service with a fake KRW-ETH exchange (NEVER the real Upbit client):
enable gate, real-fill seed/grid accounting, ledger for the BTC trader's reconciliation, two-asset
reconciliation, external-flow detection, API/DB error freeze, restart recovery, and the BTC trader's
one-time sleeve carve-out with position/stop preserved."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.config.settings import EnvSettings, Settings, load_yaml_config
from app.core.types import Fill, OrderState, OrdType, Side, TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.eth_grid import service as svc_mod
from app.eth_grid.live_engine import GridStatus
from app.eth_grid.service import CONTROL_KEY, STATE_KEY, EthGridService
from app.eth_grid.trend import DailyTrendGuard
from app.exchange.base import Balances, GatewayNetworkError, GatewayOrder, OrderNotFoundError
from app.portfolio import reconciler as rec_mod
from app.portfolio.portfolio_manager import PortfolioManager
from app.portfolio.reconciler import Reconciler, ReconcileStatus
from app.portfolio.sleeves import SleeveLedger
from tests.integration.test_live_preparation import FakeLiveGateway, _live_service
from tests.integration.test_trader_service import FakeFeed

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config" / "config.yaml"
# tick timestamps must be fresh relative to the REAL clock (risk layer: STALE_MARKET_DATA > 120 s);
# a fixed past date made the fakes look stale once the calendar moved on
T0 = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=1)
Q8 = Decimal("0.00000001")
Q2 = Decimal("0.01")
BTC_QTY = Decimal("0.00025795")
D0_CASH_AFTER = Decimal("20024.91")  # 70,024.91 - 50,000 carve-out
EX_KRW = Decimal("70042.91176366")


class FakeEthExchange:
    """Market-order exchange for KRW-ETH sharing one wallet with a BTC position."""

    name = "fake-eth"

    def __init__(self) -> None:
        self.wallet = {"KRW": EX_KRW, "BTC": BTC_QTY, "ETH": Decimal(0)}
        self.price = Decimal(3_660_000)
        self.orders: dict[str, GatewayOrder] = {}
        self.place_calls = 0
        self.fail_place: Exception | None = None
        self.fail_lookup: Exception | None = None
        self.lookup_result: GatewayOrder | None = None
        self.clock = T0
        self.open: list[GatewayOrder] = []
        self.fee = Decimal("0.0005")

    async def place_market_buy(self, client_order_id: str, krw_amount: Decimal) -> GatewayOrder:
        self.place_calls += 1
        if self.fail_place is not None:
            raise self.fail_place
        px = (self.price * Decimal("1.0003")).quantize(Decimal(1))
        vol = (krw_amount / px).quantize(Q8, rounding="ROUND_DOWN")
        funds = (vol * px).quantize(Q2)
        fee = (funds * self.fee).quantize(Q2)
        self.wallet["KRW"] -= funds + fee
        self.wallet["ETH"] += vol
        fill = Fill(client_order_id, px, vol, funds, fee, self.clock, f"t-{client_order_id}")
        o = GatewayOrder(
            client_order_id,
            f"uuid-{client_order_id}",
            "KRW-ETH",
            Side.BID,
            OrdType.PRICE,
            OrderState.FILLED,
            krw_amount,
            None,
            vol,
            px,
            fee,
            [fill],
            self.clock,
        )
        self.orders[client_order_id] = o
        return o

    async def place_market_sell(self, client_order_id: str, volume: Decimal) -> GatewayOrder:
        self.place_calls += 1
        if self.fail_place is not None:
            raise self.fail_place
        px = (self.price * Decimal("0.9997")).quantize(Decimal(1))
        funds = (volume * px).quantize(Q2)
        fee = (funds * self.fee).quantize(Q2)
        self.wallet["KRW"] += funds - fee
        self.wallet["ETH"] -= volume
        fill = Fill(client_order_id, px, volume, funds, fee, self.clock, f"t-{client_order_id}")
        o = GatewayOrder(
            client_order_id,
            f"uuid-{client_order_id}",
            "KRW-ETH",
            Side.ASK,
            OrdType.MARKET,
            OrderState.FILLED,
            None,
            volume,
            volume,
            px,
            fee,
            [fill],
            self.clock,
        )
        self.orders[client_order_id] = o
        return o

    async def get_order(
        self, *, client_order_id: str | None = None, exchange_uuid: str | None = None
    ) -> GatewayOrder:
        if self.fail_lookup is not None:
            raise self.fail_lookup
        if self.lookup_result is not None:
            return self.lookup_result
        o = self.orders.get(str(client_order_id))
        if o is None:
            raise OrderNotFoundError(str(client_order_id))
        return o

    async def cancel_order(
        self, *, client_order_id: str | None = None, exchange_uuid: str | None = None
    ) -> GatewayOrder:
        return self.orders[str(client_order_id)]

    async def get_open_orders(self) -> list[GatewayOrder]:
        return list(self.open)

    async def get_balances(self) -> Balances:
        return Balances(
            self.wallet["KRW"],
            Decimal(0),
            self.wallet["ETH"],
            Decimal(0),
            others={"BTC": self.wallet["BTC"]},
        )

    async def balances(self) -> dict[str, Decimal]:
        return dict(self.wallet)

    async def get_reference_price(self) -> Decimal:
        return self.price

    async def ping(self) -> bool:
        return True


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    for k in ("UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY", "LIVE_CONFIRM"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_yaml_config(CONFIG)
    cfg.system.database_url = f"sqlite+aiosqlite:///{tmp_path}/main.db"
    cfg.eth_grid.database_url = f"sqlite+aiosqlite:///{tmp_path}/eth.db"
    cfg.eth_grid.eth_market_db = tmp_path / "missing_eth_market.db"
    cfg.eth_grid.reconcile_interval_sec = 0
    # tests start on the original G0 rule set; the V2 cutover test switches explicitly
    cfg.eth_grid.strategy_epoch = "G0"
    cfg.eth_grid.active_gap_pct = None
    # the test scenario is a LIVE_EXPERIMENT sleeve regardless of the shipped (safe) config default
    cfg.portfolio.sleeves["ETH_GRID"].mode = "LIVE_EXPERIMENT"
    cfg.eth_grid.compounding.enabled = True
    return Settings(EnvSettings(_env_file=None), cfg)


def _d0_book(krw: Decimal = D0_CASH_AFTER) -> dict[str, Any]:
    return {
        "state": {
            "initial_equity": "50039.49655616",
            "krw": str(krw),
            "btc": str(BTC_QTY),
            "avg_entry": "116300000",
            "entry_fees": "15.00",
            "entry_time": "2026-10-02T04:01:01+00:00",
            "highest_since_entry": "117646000",
            "realized_pnl": "0",
            "fees_total": "15.00",
            "hwm": "50371.70",
            "max_drawdown": "0.0082",
            "trade_count": 0,
        },
        "open_trade_id": 2,
        "stop_price": "114804125.10160084080",
        "entry_reason": "BREAKOUT",
        "consecutive_losses": 0,
        "last_loss_at": None,
        "daily": {
            "day_key": "2026-10-05",
            "start_equity": "49866.63",
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "fees": "0",
            "realized": "0",
        },
    }


async def _repos(s: Settings, *, carved: bool = True) -> tuple[Repository, Repository]:
    me = create_engine(s.config.system.database_url)
    await init_schema(me)
    ee = create_engine(s.config.eth_grid.database_url)
    await init_schema(ee)
    main, eth = Repository(me), Repository(ee)
    await main.set_state("portfolio", _d0_book())
    await main.set_state(
        "sleeve_ledger_BTC_D0",
        SleeveLedger(
            "BTC_D0",
            "BTC",
            D0_CASH_AFTER,
            BTC_QTY,
            owner="trader",
            updated_at=datetime.now(UTC).isoformat(),
        ).to_dict(),
    )
    if carved:
        await main.set_state(
            "sleeve_transfers",
            {"ETH_GRID": {"status": "DONE", "amount": "50000", "from": "BTC_D0", "to": "ETH_GRID"}},
        )
    return main, eth


async def _service(
    s: Settings, main: Repository, eth: Repository, ex: FakeEthExchange
) -> EthGridService:
    svc = EthGridService(
        s,
        eth,
        main,
        ex,
        balances=ex.balances,
        feed_factory=FakeFeed,
        trend=DailyTrendGuard(100),
        exchange_terms={"bid_fee": "0.0005", "ask_fee": "0.0005", "min_total": "5000"},
        run_id="eth-t",
    )
    return svc


async def _enable(eth: Repository) -> None:
    await eth.set_state(
        CONTROL_KEY, {"enabled": True, "enabled_at": T0.isoformat(), "enabled_by": "test"}
    )


def _status(svc: EthGridService) -> GridStatus:
    return svc.status


def _frozen(svc: EthGridService) -> bool:
    return svc.engine.s.frozen


def _pause_reason(svc: EthGridService) -> str | None:
    return svc.engine.s.buy_pause_reason


# ----------------------------------------------------------------------------- enable gate
async def test_not_enabled_places_no_order_even_when_ticking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(svc_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    main, eth = await _repos(s)
    ex = FakeEthExchange()
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)
    for i in range(5):
        await svc.on_tick(ex.price - i * 10_000, T0 + timedelta(seconds=i))
    assert ex.place_calls == 0 and _status(svc) is GridStatus.WAITING_ENABLE
    assert svc.reconcile_last["status"] == "OK"
    lg = await main.get_state("sleeve_ledger_ETH_GRID")
    assert (
        lg
        and Decimal(lg["cash"]) == 50000
        and Decimal(lg["quantity"]) == 0
        and lg["mode"] == "LIVE_EXPERIMENT"
    )
    await svc.stop()


async def test_carve_out_pending_blocks_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(svc_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    main, eth = await _repos(s, carved=False)
    await main.set_state("portfolio", _d0_book(Decimal("70024.91")))  # trader still on the old book
    ex = FakeEthExchange()
    await _enable(eth)
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)
    await svc.on_tick(ex.price, T0)
    assert ex.place_calls == 0 and _status(svc) is GridStatus.WAITING_ENABLE
    await svc.stop()


# ----------------------------------------------------------------------------- seed + grid
async def test_seed_grid_cycle_real_fill_accounting_ledger_and_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(svc_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    main, eth = await _repos(s)
    ex = FakeEthExchange()
    await _enable(eth)
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)
    assert svc.enabled
    # 1) seed: ONE market buy of ₩25,000, split into 5 lots; accounting == exchange fill
    await svc.on_tick(ex.price, T0)
    assert ex.place_calls == 1
    seed = next(iter(ex.orders.values()))
    assert seed.requested_krw == 25000 and seed.side is Side.BID
    st = svc.engine.s
    assert len(st.lots) == 5 and st.inventory_qty == seed.executed_volume == ex.wallet["ETH"]
    assert st.cash == Decimal(50000) - seed.fills[0].funds - seed.paid_fee
    assert sum((lot.cost for lot in st.lots), Decimal(0)) == seed.fills[0].funds + seed.paid_fee
    assert _status(svc) is GridStatus.RUNNING
    lg = await main.get_state("sleeve_ledger_ETH_GRID")
    assert (
        lg
        and Decimal(lg["quantity"]) == ex.wallet["ETH"]
        and Decimal(lg["cash"]) == st.cash
        and lg["inflight"] is False
    )
    # orders live ONLY in the grid DB; the trader DB has no ETH orders
    assert len(await eth.recent_orders(TradingMode.LIVE, market="KRW-ETH")) == 1
    assert await main.recent_orders(TradingMode.LIVE) == []
    # 2) grid BUY when the price crosses level -1
    lvl = svc.engine.level_price(-1)
    ex.price = lvl - 500
    await svc.on_tick(ex.price, T0 + timedelta(minutes=1))
    assert ex.place_calls == 2
    lot = next(lot for lot in st.lots if not lot.seed)
    assert lot.level == -1 and lot.target == (lvl * Decimal("1.0075")).quantize(Decimal(1))
    assert lot.quantity == ex.orders[lot.buy_client_id].executed_volume
    # the same level is never bought twice while the lot is held
    ex.price = lvl + 2000
    await svc.on_tick(ex.price, T0 + timedelta(minutes=2))
    ex.price = lvl - 500
    await svc.on_tick(ex.price, T0 + timedelta(minutes=3))
    assert ex.place_calls == 2
    # 3) SELL at the target -> completed cycle with real fees, cash back to the ETH sleeve
    ex.price = lot.target
    cash_before = st.cash
    await svc.on_tick(ex.price, T0 + timedelta(minutes=4))
    assert ex.place_calls == 3 and st.completed_cycles == 1
    cyc = svc.engine.cycles[-1]
    sell = ex.orders[cyc.sell_client_id]
    assert (
        cyc.sell_uuid == sell.exchange_uuid
        and cyc.buy_uuid == ex.orders[cyc.buy_client_id].exchange_uuid
    )
    assert cyc.net_pnl == sell.fills[0].funds - sell.paid_fee - (cyc.buy_funds + cyc.buy_fee)
    assert cyc.net_pnl > 0 and st.cash == cash_before + sell.fills[0].funds - sell.paid_fee
    assert st.inventory_qty == ex.wallet["ETH"]
    # 4) multi-asset reconciliation PASSES: wallet == D0 book + ETH ledger (no 'external flow')
    r = await svc._reconcile()
    assert r["status"] == "OK", r
    # D0 book untouched; ETH cash never exceeds its ₩50,000 allocation (no cash contamination)
    d0_after = await main.get_state("portfolio")
    assert d0_after is not None and Decimal(d0_after["state"]["krw"]) == D0_CASH_AFTER
    assert st.cash + st.inventory_cost == Decimal(50000) + st.realized  # sleeve books close exactly
    # 5) Korean activity events exist
    types = {e.type for e in await eth.recent_events(100)}
    assert {"ETH_LIVE_EXPERIMENT_ENABLED", "ETH_SEED_BUY", "ETH_GRID_BUY", "ETH_GRID_SELL"} <= types
    msgs = [e.message for e in await eth.recent_events(100) if e.type == "ETH_GRID_SELL"]
    assert msgs and "순익" in msgs[0] and "Cycle #1" in msgs[0]
    # 6) client ids are deterministic/unique and all orders carry our prefix
    ids = [o.client_order_id for o in ex.orders.values()]
    assert len(set(ids)) == 3 and all(i.startswith("ethg-") for i in ids)
    book = svc.engine.book(ex.price)
    assert Decimal(book["current_equity"]) == st.equity(ex.price) and book["completed_cycles"] == 1
    await svc.stop()


# ----------------------------------------------------------------------------- trader reconciliation with the ETH ledger
async def test_btc_trader_reconciler_accounts_for_eth_ledger_and_defers_inflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(rec_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    main, eth = await _repos(s)
    ex = FakeEthExchange()

    # BTC trader side: its gateway sees the whole wallet (KRW + BTC + ETH as 'others')
    class BtcGw(FakeLiveGateway):
        async def get_balances(self) -> Balances:
            return Balances(
                ex.wallet["KRW"],
                Decimal(0),
                ex.wallet["BTC"],
                Decimal(0),
                others={"ETH": ex.wallet["ETH"]} if ex.wallet["ETH"] > 0 else {},
            )

        async def get_open_orders(self) -> list[GatewayOrder]:
            return []

    pf = PortfolioManager(main, TradingMode.LIVE, Decimal("50039.49655616"))
    pf.restore(_d0_book())
    pf.on_price(Decimal(115_700_000))
    rc = Reconciler(
        BtcGw(),
        pf,
        main,
        krw_tolerance=Decimal(100),
        btc_tolerance=Decimal("0.00001"),
        record_external_flows=True,
    )
    # before the ETH grid writes a ledger: wallet has 50k the book does not -> mismatch (as expected)
    assert (await rc.run([])).status is ReconcileStatus.FAIL
    # ETH ledger present (cash 50k, no ETH yet) -> OK
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)
    r = await rc.run([])
    assert r.status is ReconcileStatus.OK, r.details
    # ETH grid buys the seed -> exchange moved; ledger updated right after -> OK
    await _enable(eth)
    await svc._poll_control(force=True)
    await svc.on_tick(ex.price, T0)
    assert ex.wallet["ETH"] > 0
    assert (await rc.run([])).status is ReconcileStatus.OK
    # race: exchange already moved for a new ETH buy but the ledger still shows in-flight -> WARN (deferred), never FAIL
    lg = await main.get_state("sleeve_ledger_ETH_GRID")
    assert lg is not None
    stale = {**lg, "inflight": True, "updated_at": datetime.now(UTC).isoformat()}
    await main.set_state("sleeve_ledger_ETH_GRID", stale)
    ex.wallet["KRW"] -= Decimal(5002)
    ex.wallet["ETH"] += Decimal("0.001")
    r = await rc.run([])
    assert r.status is ReconcileStatus.WARN and r.details["deferred"] is True
    # settled wrongly (not in-flight, old timestamp) -> FAIL
    await main.set_state(
        "sleeve_ledger_ETH_GRID",
        {
            **lg,
            "inflight": False,
            "updated_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        },
    )
    assert (await rc.run([])).status is ReconcileStatus.FAIL
    # a heartbeat-fresh ledger WITHOUT a recent fill never hides a real mismatch
    await main.set_state(
        "sleeve_ledger_ETH_GRID",
        {
            **lg,
            "inflight": False,
            "updated_at": datetime.now(UTC).isoformat(),
            "last_trade_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        },
    )
    assert (await rc.run([])).status is ReconcileStatus.FAIL
    # ...but a fill a moment ago (ledger write still racing the exchange) is deferred once
    await main.set_state(
        "sleeve_ledger_ETH_GRID",
        {**lg, "inflight": False, "last_trade_at": datetime.now(UTC).isoformat()},
    )
    assert (await rc.run([])).status is ReconcileStatus.WARN
    await svc.stop()


# ----------------------------------------------------------------------------- external flow / freezes
async def test_external_withdrawal_detected_then_frozen_no_more_orders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(svc_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    main, eth = await _repos(s)
    ex = FakeEthExchange()
    await _enable(eth)
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)
    await svc.on_tick(ex.price, T0)
    assert ex.place_calls == 1
    ex.wallet["KRW"] -= Decimal(10_000)  # someone withdrew ₩10,000
    r1 = await svc._reconcile()
    assert r1["status"] == "FAIL" and "mismatch: KRW -99" in r1["detail"] and not _frozen(svc)
    r2 = await svc._reconcile()
    assert r2["status"] == "FAIL" and _frozen(svc) and _status(svc) is GridStatus.FROZEN
    # frozen: no automatic orders, no forced liquidation
    ex.price = svc.engine.level_price(-1) - 500
    await svc.on_tick(ex.price, T0 + timedelta(minutes=1))
    assert ex.place_calls == 1 and ex.wallet["ETH"] > 0
    lg = await main.get_state("sleeve_ledger_ETH_GRID")
    assert lg and lg["status"] == "FROZEN"
    await svc.stop()


async def test_api_error_unknown_order_freezes_and_recovers_on_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(svc_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    main, eth = await _repos(s)
    ex = FakeEthExchange()
    await _enable(eth)
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)
    # network error on the seed: request may have reached the exchange -> UNKNOWN -> FROZEN, pending kept
    ex.fail_place = GatewayNetworkError("timeout")
    ex.fail_lookup = GatewayNetworkError("timeout")
    await svc.on_tick(ex.price, T0)
    assert _frozen(svc) and svc.engine.s.frozen_reason == "ORDER_UNKNOWN_UNRESOLVED"
    assert svc.engine.s.pending is not None and len(svc.engine.s.lots) == 0
    await svc.stop()
    # restart: the exchange now says the order never existed -> pending cleared, no lots, still frozen (operator must clear)
    ex.fail_place = None
    ex.fail_lookup = None
    svc2 = await _service(s, main, eth, ex)
    await svc2.start(run_loops=False)
    assert svc2.engine.s.pending is None and len(svc2.engine.s.lots) == 0 and _frozen(svc2)
    # operator unfreeze via control -> seeding proceeds normally
    await eth.set_state(
        CONTROL_KEY,
        {
            "enabled": True,
            "unfreeze_request_at": (T0 + timedelta(days=1)).isoformat(),
            "unfreeze_by": "test",
        },
    )
    await svc2._poll_control(force=True)
    assert not _frozen(svc2)
    await svc2.on_tick(ex.price, T0 + timedelta(minutes=1))
    assert len(svc2.engine.s.lots) == 5 and ex.place_calls == 2  # 1 failed attempt + 1 real seed
    assert len([o for o in ex.orders.values()]) == 1  # only ONE order ever reached the exchange
    await svc2.stop()


async def test_db_write_errors_freeze(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(svc_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    main, eth = await _repos(s)
    ex = FakeEthExchange()
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)
    eth.write_errors += s.config.eth_grid.db_write_error_limit
    await svc.supervisor_tick()
    assert _frozen(svc) and svc.engine.s.frozen_reason == "DATABASE_FAILURE"
    await svc.stop()


async def test_restart_recovery_applies_fill_that_happened_before_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Crash between the exchange fill and the state update: order row FILLED in DB, pending in state."""
    monkeypatch.setattr(svc_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    main, eth = await _repos(s)
    ex = FakeEthExchange()
    await _enable(eth)
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)

    async def _crash(rec: Any, ts: Any, *, recovered: bool = False) -> None:
        raise RuntimeError("simulated crash after fill")

    monkeypatch.setattr(svc, "_settle", _crash)
    with pytest.raises(RuntimeError):
        await svc.on_tick(ex.price, T0)
    assert ex.place_calls == 1 and ex.wallet["ETH"] > 0
    saved = await eth.get_state(STATE_KEY)
    assert saved is not None
    assert saved["pending"] is not None and saved["lots"] == []
    # new process: recovers the pending order from the DB row + exchange and books the lots
    svc2 = await _service(s, main, eth, ex)
    await svc2.start(run_loops=False)
    st = svc2.engine.s
    assert st.pending is None and len(st.lots) == 5 and st.inventory_qty == ex.wallet["ETH"]
    assert not _frozen(svc2) and _status(svc2) is GridStatus.RUNNING
    assert (await svc2._reconcile())["status"] == "OK"
    assert {e.type for e in await eth.recent_events(50)} >= {"ETH_RECOVERY", "ETH_SEED_BUY"}
    # no duplicate order for the same seed after recovery
    await svc2.on_tick(ex.price, T0 + timedelta(minutes=1))
    assert ex.place_calls == 1
    await svc2.stop()


async def test_restart_recovery_pending_without_db_row_not_on_exchange_is_cleared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(svc_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    main, eth = await _repos(s)
    ex = FakeEthExchange()
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)
    from app.eth_grid.live_engine import Intent

    svc.engine.mark_pending(
        Intent("SEED", Side.BID, ex.price, krw=Decimal(25000)), "ethg-ghost", T0
    )
    await svc._persist(force=True)  # crash right here: no order row, nothing at the exchange
    await svc.stop()
    svc2 = await _service(s, main, eth, ex)
    await svc2.start(run_loops=False)
    assert svc2.engine.s.pending is None and not _frozen(svc2) and ex.place_calls == 0
    await svc2.stop()


# ----------------------------------------------------------------------------- BTC trader: carve-out + restart restore
async def test_trader_restart_carves_out_eth_sleeve_once_and_keeps_position_and_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gw = FakeLiveGateway(krw=Decimal("70042.91"), btc=BTC_QTY)
    svc, repo = await _live_service(monkeypatch, tmp_path, gw)
    # the trader's book as it is today (pre-carve-out), with the open position and ratcheted stop
    book = _d0_book(Decimal("70024.91"))
    book["state"]["initial_equity"] = "100039.49655616"
    book["state"]["hwm"] = "100371.70"
    book["daily"]["start_equity"] = "99866.63"
    await repo.set_state("portfolio", book)
    await repo.set_state(
        "challenge",
        {
            "status": "STARTED",
            "live_activated_at": "2026-09-29T15:09:53+00:00",
            "live_start_krw": "100039.49655616",
        },
    )
    import hashlib
    import json

    snapshot = {k: float(v) for k, v in sorted(svc.strategy.params.items())}
    phash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:16]
    await repo.set_state(
        "strategy_epoch_LIVE",
        {
            "strategy_version": svc.cfg.strategy.strategy_version,
            "parameter_hash": phash,
            "counters": {},
        },
    )
    svc.cfg.portfolio.sleeves[
        "BTC_D0"
    ].strategy_version = svc.cfg.strategy.strategy_version  # test strategy owns the D0 sleeve
    svc.cfg.portfolio.sleeves["ETH_GRID"].mode = "LIVE_EXPERIMENT"  # the sleeve being carved out
    await svc.start(run_loops=False)
    st = svc.portfolio.state
    assert st.btc == BTC_QTY and st.avg_entry == Decimal(116_300_000)
    assert svc.portfolio.stop_price == Decimal("114804125.10160084080")
    assert st.krw == Decimal("20024.91") and st.initial_equity == Decimal("50039.49655616")
    assert st.hwm == Decimal("50371.70") and svc.portfolio.daily.start_equity == Decimal("49866.63")
    tr = await repo.get_state("sleeve_transfers")
    assert tr and tr["ETH_GRID"]["status"] == "DONE" and tr["ETH_GRID"]["amount"] == "50000"
    assert tr["ETH_GRID"]["position_quantity"] == str(BTC_QTY)
    assert gw.place_calls == 0  # no order from the restart
    ph = await repo.get_state("sleeve_ledger_ETH_GRID")
    assert ph and Decimal(ph["cash"]) == 50000 and Decimal(ph["quantity"]) == 0
    assert ph["mode"] == "LIVE_EXPERIMENT"
    # the trader's own reconciliation balances from this restart on: wallet 70,042 = 20,024 + 50,000
    gw.krw = Decimal("70042.91")
    r = await svc.reconciler.run([])
    assert r.ok, r.details
    # risk context: sleeve-capped equity/cash; next entry policy 15k (30% of 50k) — position grandfathered
    svc.portfolio.on_price(Decimal(115_700_000))
    await svc._persist_bot_state()
    ctx = await svc._risk_context()
    assert ctx.equity <= Decimal(50000) and ctx.krw <= Decimal(50000) - st.btc_value(
        svc.portfolio.price
    ) + Decimal("0.01")
    lg = await repo.get_state("sleeve_ledger_BTC_D0")
    assert (
        lg
        and lg["owner"] == "trader"
        and Decimal(lg["cash"]) == Decimal("20024.91")
        and Decimal(lg["quantity"]) == BTC_QTY
    )
    snap = (await repo.get_state("strategy_sleeves") or {}).get("sleeves", {}).get("BTC_D0", {})
    assert snap.get("next_max_entry_krw") == "15000" and snap.get("grandfathered_position") is True
    await svc.stop()
    carved_book = await repo.get_state("portfolio")
    assert carved_book is not None
    # second restart: idempotent (no second carve-out), position/stop intact
    gw2 = FakeLiveGateway(krw=Decimal("70042.91"), btc=BTC_QTY)
    svc2, repo2 = await _live_service(monkeypatch, tmp_path, gw2)
    await repo2.set_state(
        "portfolio", carved_book
    )  # the helper plants a dummy PAPER book; restore ours
    svc2.cfg.portfolio.sleeves["BTC_D0"].strategy_version = svc2.cfg.strategy.strategy_version
    svc2.cfg.portfolio.sleeves["ETH_GRID"].mode = "LIVE_EXPERIMENT"
    await svc2.start(run_loops=False)
    assert svc2.portfolio.state.krw == Decimal("20024.91") and svc2.portfolio.state.btc == BTC_QTY
    assert svc2.portfolio.stop_price == Decimal("114804125.10160084080") and gw2.place_calls == 0
    # drawdown/daily limits are NOT distorted by the transfer
    svc2.portfolio.on_price(Decimal(115_700_000))
    assert svc2.portfolio.drawdown() < Decimal("0.02") and abs(
        svc2.portfolio.daily_pnl()
    ) < Decimal(100)
    await svc2.stop()


# ----------------------------------------------------------------------------- compounding activation
async def test_compounding_activation_on_restart_causes_zero_orders_and_keeps_lots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Existing experiment (seeded, one cycle done) restarted with compounding ENABLED: unit stays
    ₩5,000 (capital 50,030 -> 5,003 -> floor 5,000), no order is placed, lots/targets untouched,
    sizing epoch C0 recorded, next NEW buy uses the dynamic unit."""
    monkeypatch.setattr(svc_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    s.config.eth_grid.compounding.enabled = False  # the experiment started WITHOUT compounding
    main, eth = await _repos(s)
    ex = FakeEthExchange()
    await _enable(eth)
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)
    await svc.on_tick(ex.price, T0)  # seed
    lvl = svc.engine.level_price(-1)
    ex.price = lvl - 500
    await svc.on_tick(ex.price, T0 + timedelta(minutes=1))  # grid buy ₩5,000
    lot = next(lot for lot in svc.engine.s.lots if not lot.seed)
    ex.price = lot.target
    await svc.on_tick(ex.price, T0 + timedelta(minutes=2))  # sell -> 1 cycle, realized ~ +30
    assert ex.place_calls == 3 and svc.engine.s.completed_cycles == 1
    before = {x.lot_id: (x.quantity, x.target) for x in svc.engine.s.lots}
    realized = svc.engine.s.realized
    await svc.stop()
    # --- activation: same DB, compounding switched on in config, process restarted
    s.config.eth_grid.compounding.enabled = True
    svc2 = await _service(s, main, eth, ex)
    await svc2.start(run_loops=False)
    e = svc2.engine
    assert e.policy is not None and e.policy.enabled
    assert e.current_unit == 5000 and e.s.sizing_epoch == "C0"
    assert e.realized_capital() == Decimal(50000) + realized and 0 < realized < 1000
    assert {x.lot_id: (x.quantity, x.target) for x in e.s.lots} == before
    assert all(x.entry_unit_krw == 5000 and x.sizing_epoch == "C0" for x in e.s.lots)
    assert ex.place_calls == 3  # activation itself placed nothing
    await svc2.on_tick(ex.price, T0 + timedelta(minutes=3))
    assert ex.place_calls == 3
    saved = await eth.get_state(STATE_KEY)
    assert saved is not None and saved["compounding"]["enabled"] is True
    assert (
        saved["compounding"]["current_unit"] == "5000"
        and saved["compounding_policy"]["unit_ratio"] == "0.1"
    )
    assert (
        saved["compounding"]["next_unit"] == "6000"
        and saved["compounding"]["next_unit_threshold_capital"] == "60000"
    )
    assert (await svc2._reconcile())["status"] == "OK"
    # a realized jump (simulated) -> the NEXT new buy is ₩6,000 and the order row carries the epoch
    e.s.realized = Decimal(10_000)
    e.s.cash += Decimal(10_000)
    ex.wallet["KRW"] += Decimal(10_000)
    lvl2 = e.level_price(-2)
    ex.price = lvl2 - 500
    await svc2.on_tick(ex.price, T0 + timedelta(minutes=4))
    # levels -1 (freed by the earlier sell) and -2 were both crossed -> two ₩6,000 buys, one unit
    assert ex.place_calls == 5
    assert all(x.entry_unit_krw == 6000 and x.sizing_epoch == "C1" for x in e.s.lots if not x.seed)
    new_lot = next(x for x in e.s.lots if x.level == -2)
    assert new_lot.entry_unit_krw == 6000 and new_lot.sizing_epoch == "C1"
    assert ex.orders[new_lot.buy_client_id].requested_krw == 6000
    row = await eth.get_order(new_lot.buy_client_id)
    assert (
        row is not None
        and row.raw.get("sizing_epoch") == "C1"
        and row.raw.get("unit_krw") == "6000"
    )
    assert row.raw.get("strategy_epoch") == "G0"
    # old ₩5,000 lots are still ₩5,000 lots with their original targets
    assert {x.lot_id: (x.quantity, x.target) for x in e.s.lots if x.lot_id in before} == before
    types = {ev.type for ev in await eth.recent_events(100)}
    assert "ETH_SIZING_UPDATE" in types
    await svc2.stop()
    # --- restart recovery keeps the sizing epoch and unit
    svc3 = await _service(s, main, eth, ex)
    await svc3.start(run_loops=False)
    assert svc3.engine.s.sizing_epoch == "C1" and svc3.engine.current_unit == 6000
    assert ex.place_calls == 5
    await svc3.stop()


# ----------------------------------------------------------------------------- V2 cutover (service)
async def test_v2_cutover_on_restart_zero_orders_legacy_exit_only_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Experiment running on G0 (4 legacy lots, 1 cycle) restarted with strategy_epoch=AGGRESSIVE_V2_050:
    cutover happens at the first REAL tick, creates no order, keeps legacy lots/targets, main DB
    untouched; later a real crossing of a V2 level buys at the compounding unit with a fill x 1.005
    target; legacy sells still work; restart keeps the mixed state."""
    monkeypatch.setattr(svc_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    s.config.eth_grid.strategy_epoch = "G0"
    s.config.eth_grid.active_gap_pct = None
    main, eth = await _repos(s)
    ex = FakeEthExchange()
    await _enable(eth)
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)
    await svc.on_tick(ex.price, T0)  # seed at 3,660,000
    lot1 = min(svc.engine.s.lots, key=lambda x: x.target)
    ex.price = lot1.target
    await svc.on_tick(ex.price, T0 + timedelta(minutes=1))  # legacy cycle #1
    assert ex.place_calls == 2 and len(svc.engine.s.lots) == 4
    legacy_before = {
        x.lot_id: (x.quantity, x.buy_price, x.cost, x.target) for x in svc.engine.s.lots
    }
    main_pf_before = await main.get_state("portfolio")
    await svc.stop()
    # --- cutover approved: new config, controlled restart
    s.config.eth_grid.strategy_epoch = "AGGRESSIVE_V2_050"
    s.config.eth_grid.active_gap_pct = Decimal("0.005")
    svc2 = await _service(s, main, eth, ex)
    await svc2.start(run_loops=False)
    e = svc2.engine
    assert e.s.strategy_epoch == "G0" and ex.place_calls == 2  # nothing happens without a real tick
    act = Decimal(3_686_000)
    ex.price = act
    await svc2.on_tick(act, T0 + timedelta(minutes=5))  # activation tick
    assert (
        e.s.strategy_epoch == "AGGRESSIVE_V2_050"
        and e.s.gap == Decimal("0.005")
        and e.s.anchor == act
    )
    assert ex.place_calls == 2  # cutover caused no order
    assert all(x.exit_only and x.strategy_epoch == "LEGACY_G0_075" for x in e.s.lots)
    assert {
        x.lot_id: (x.quantity, x.buy_price, x.cost, x.target) for x in e.s.lots
    } == legacy_before
    assert await main.get_state("portfolio") == main_pf_before  # BTC book untouched
    saved = await eth.get_state(STATE_KEY)
    assert saved is not None and saved["active_strategy"]["epoch"] == "AGGRESSIVE_V2_050"
    assert saved["active_strategy"]["cutover_pending"] is False
    types = {ev.type for ev in await eth.recent_events(50)}
    assert "ETH_STRATEGY_CUTOVER" in types
    # price history before activation is never replayed: a tick between anchor and level -1 -> no buy
    lvl1 = e.level_price(-1)
    assert lvl1 == (act / Decimal("1.005")).quantize(Decimal(1))  # 0.50 % lattice step
    ex.price = lvl1 + 500
    await svc2.on_tick(ex.price, T0 + timedelta(minutes=6))
    assert ex.place_calls == 2
    # real crossing -> V2 buy ₩5,000, target = fill x 1.005 tick-rounded
    ex.price = lvl1 - 500
    await svc2.on_tick(ex.price, T0 + timedelta(minutes=7))
    assert ex.place_calls == 3
    v2 = next(x for x in e.s.lots if not x.exit_only)
    order = ex.orders[v2.buy_client_id]
    assert (
        order.requested_krw == 5000
        and v2.strategy_epoch == "AGGRESSIVE_V2_050"
        and v2.gap_pct == Decimal("0.005")
    )
    from app.exchange.upbit.tick_size import round_price

    assert v2.target == round_price(v2.buy_price * Decimal("1.005"), "up")
    row = await eth.get_order(v2.buy_client_id)
    assert (
        row is not None
        and row.raw.get("strategy_epoch") == "AGGRESSIVE_V2_050"
        and row.raw.get("candidate") == "G0"
    )
    # legacy sell still allowed at its ORIGINAL 0.75 % target
    legacy = min((x for x in e.s.lots if x.exit_only), key=lambda x: x.target)
    ex.price = legacy.target
    await svc2.on_tick(ex.price, T0 + timedelta(minutes=8))
    # the legacy target (+0.75 %) is above the V2 lot's target (fill x 1.005): both sell -> 2 orders
    assert ex.place_calls == 5
    assert {c.strategy_epoch for c in e.cycles[-2:]} == {"LEGACY_G0_075", "AGGRESSIVE_V2_050"}
    v2_cycle = next(c for c in e.cycles if c.strategy_epoch == "AGGRESSIVE_V2_050")
    assert v2_cycle.sell_price >= v2.target * Decimal("0.999") and v2_cycle.net_pnl > 0
    assert (await svc2._reconcile())["status"] == "OK"
    await svc2.stop()
    # --- restart recovery with mixed lots: no cutover again, no order
    svc3 = await _service(s, main, eth, ex)
    await svc3.start(run_loops=False)
    await svc3.on_tick(ex.price, T0 + timedelta(minutes=9))
    assert svc3.engine.s.strategy_epoch == "AGGRESSIVE_V2_050" and ex.place_calls == 5
    assert (
        len([x for x in svc3.engine.s.lots if x.exit_only]) == 3
        and len(svc3.engine.active_lots()) == 0
    )
    assert len(svc3.engine.s.cutover_history) == 1
    await svc3.stop()


# ----------------------------------------------------------------------------- operator BUY pause
async def test_buy_pause_control_blocks_buys_keeps_sells_zero_orders_on_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(svc_mod, "RECHECK_DELAY_SEC", 0.0)
    s = _settings(monkeypatch, tmp_path)
    main, eth = await _repos(s)
    ex = FakeEthExchange()
    await _enable(eth)
    svc = await _service(s, main, eth, ex)
    await svc.start(run_loops=False)
    await svc.on_tick(ex.price, T0)  # seed (5 lots)
    assert ex.place_calls == 1
    before = {x.lot_id: (x.quantity, x.target, x.cost) for x in svc.engine.s.lots}
    await svc.stop()
    # operator pause written to the control key, then a controlled restart: zero orders
    ctl = await eth.get_state(CONTROL_KEY) or {}
    await eth.set_state(
        CONTROL_KEY,
        {
            **ctl,
            "buy_paused": True,
            "buy_pause_reason": "PAUSED_NEW_BUYS_INVENTORY_RISK",
            "buy_pause_by": "user",
        },
    )
    svc2 = await _service(s, main, eth, ex)
    await svc2.start(run_loops=False)
    e = svc2.engine
    assert (
        e.s.buy_pause_reason == "PAUSED_NEW_BUYS_INVENTORY_RISK" and e.status.value == "BUY_PAUSED"
    )
    assert {x.lot_id: (x.quantity, x.target, x.cost) for x in e.s.lots} == before
    # price crosses buy levels -> no order; a sell target -> sell order
    ex.price = e.level_price(-3) - 500
    await svc2.on_tick(ex.price, T0 + timedelta(minutes=1))
    assert ex.place_calls == 1
    lot = min(e.s.lots, key=lambda x: x.target)
    ex.price = lot.target
    await svc2.on_tick(ex.price, T0 + timedelta(minutes=2))
    assert ex.place_calls == 2 and e.cycles[-1].lot_id == lot.lot_id and e.cycles[-1].net_pnl > 0
    assert {x.lot_id: (x.quantity, x.target, x.cost) for x in e.s.lots} == {
        k: v for k, v in before.items() if k != lot.lot_id
    }
    saved = await eth.get_state(STATE_KEY)
    assert saved is not None and saved["buy_pause"]["paused"] is True
    assert "ETH_BUY_PAUSED" in {ev.type for ev in await eth.recent_events(50)}
    assert (await svc2._reconcile())["status"] == "OK"
    # resume via control -> buys allowed again
    await eth.set_state(
        CONTROL_KEY, {**(await eth.get_state(CONTROL_KEY) or {}), "buy_paused": False}
    )
    await svc2._poll_control(force=True)
    assert _pause_reason(svc2) is None and _status(svc2) is GridStatus.RUNNING
    await svc2.stop()
