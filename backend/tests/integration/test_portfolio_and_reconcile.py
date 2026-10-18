from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.core.events import EventBus
from app.core.types import OrderIntent, OrdType, Side, TradingMode
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository
from app.execution.fill_model import FillParams
from app.execution.order_manager import OrderManager
from app.execution.paper_gateway import PaperGateway
from app.portfolio.portfolio_manager import PortfolioManager
from app.portfolio.reconciler import Reconciler, ReconcileStatus

P = FillParams(fee_rate=Decimal("0.0005"), slippage_bps=Decimal(3))


def _pos(pf: PortfolioManager) -> tuple[bool, bool, Decimal | None]:
    return (pf.state.has_position, pf.open_trade_id is not None, pf.stop_price)


async def _world(
    tmp_path: Path,
) -> tuple[Repository, PaperGateway, OrderManager, PortfolioManager, Reconciler]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/pf.db")
    await init_schema(engine)
    repo = Repository(engine)
    gw = PaperGateway(market="KRW-BTC", initial_krw=Decimal(100000), fill_params=P)
    gw.update_price(Decimal(100_000_000))
    om = OrderManager(gw, repo, EventBus(), TradingMode.PAPER, "KRW-BTC")
    pf = PortfolioManager(repo, TradingMode.PAPER, Decimal(100000), strategy_name="trend_momentum")
    pf.on_price(Decimal(100_000_000))
    rc = Reconciler(gw, pf, repo, krw_tolerance=Decimal(100), btc_tolerance=Decimal("0.00001"))
    return repo, gw, om, pf, rc


async def test_round_trip_trade_accounting_matches_gateway_ledger(tmp_path: Path) -> None:
    repo, gw, om, pf, rc = await _world(tmp_path)
    buy = await om.submit(
        OrderIntent(
            "b1", "KRW-BTC", Side.BID, OrdType.PRICE, Decimal(30000), None, None, TradingMode.PAPER
        )
    )
    await pf.apply_order(buy, reason="GOLDEN_CROSS", stop=Decimal(95_000_000))
    assert (
        pf.state.has_position
        and pf.open_trade_id is not None
        and pf.stop_price == Decimal(95_000_000)
    )
    assert (await rc.run(om.inflight())).status is ReconcileStatus.OK

    gw.update_price(Decimal(110_000_000))
    pf.on_price(Decimal(110_000_000))
    assert pf.state.unrealized_pnl(pf.price) > 0
    sell = await om.submit(
        OrderIntent(
            "s1", "KRW-BTC", Side.ASK, OrdType.MARKET, None, pf.state.btc, None, TradingMode.PAPER
        )
    )
    sell.raw["entry_price"] = str(pf.state.avg_entry)
    sell.raw["entry_fees"] = str(pf.state.entry_fees)
    realized = await pf.apply_order(sell, reason="EMA_DEATH_CROSS")
    assert realized is not None and realized > 0
    assert _pos(pf) == (False, False, None)
    assert pf.daily.trades == 1 and pf.daily.wins == 1 and pf.consecutive_losses == 0
    trades = await repo.closed_trades(TradingMode.PAPER)
    assert (
        len(trades) == 1
        and Decimal(str(trades[0].net_pnl)) == realized
        and trades[0].exit_reason == "EMA_DEATH_CROSS"
    )
    # internal KRW equals paper ledger KRW to the cent
    res = await rc.run(om.inflight())
    assert res.status is ReconcileStatus.OK and res.krw_diff == 0 and res.btc_diff == 0


async def test_losing_trade_updates_consecutive_losses_and_cooldown_anchor(tmp_path: Path) -> None:
    _, gw, om, pf, _ = await _world(tmp_path)
    buy = await om.submit(
        OrderIntent(
            "b", "KRW-BTC", Side.BID, OrdType.PRICE, Decimal(30000), None, None, TradingMode.PAPER
        )
    )
    await pf.apply_order(buy, reason="x", stop=None)
    gw.update_price(Decimal(95_000_000))
    pf.on_price(Decimal(95_000_000))
    sell = await om.submit(
        OrderIntent(
            "s", "KRW-BTC", Side.ASK, OrdType.MARKET, None, pf.state.btc, None, TradingMode.PAPER
        )
    )
    realized = await pf.apply_order(sell, reason="STOP")
    assert realized is not None and realized < 0
    assert (
        pf.consecutive_losses == 1
        and pf.last_loss_at == sell.fills[-1].timestamp
        and pf.daily.losses == 1
    )


async def test_persist_and_restore_roundtrip(tmp_path: Path) -> None:
    repo, _, om, pf, _ = await _world(tmp_path)
    buy = await om.submit(
        OrderIntent(
            "b", "KRW-BTC", Side.BID, OrdType.PRICE, Decimal(30000), None, None, TradingMode.PAPER
        )
    )
    await pf.apply_order(buy, reason="entry", stop=Decimal(90_000_000))
    pf2 = PortfolioManager(repo, TradingMode.PAPER, Decimal(1))
    assert await pf2.load()
    assert (
        pf2.state == pf.state
        and pf2.stop_price == pf.stop_price
        and pf2.open_trade_id == pf.open_trade_id
    )
    assert pf2.daily.day_key == pf.daily.day_key and pf2.entry_reason == "entry"


async def test_reconcile_detects_balance_position_and_open_order_mismatch(tmp_path: Path) -> None:
    repo, gw, om, _, rc = await _world(tmp_path)
    assert (await rc.run([])).ok
    gw.ledger.krw -= Decimal(500)  # exchange says we have less KRW than we think
    res = await rc.run([])
    assert (
        res.status is ReconcileStatus.FAIL and not res.balance_ok and res.krw_diff == Decimal(-500)
    )
    gw.ledger.krw += Decimal(500)
    gw.ledger.btc = Decimal("0.0005")  # unexpected position on the exchange
    res = await rc.run([])
    assert not res.position_ok and not res.balance_ok
    gw.ledger.btc = Decimal(0)
    ghost = await om.submit(
        OrderIntent(
            "g", "KRW-BTC", Side.BID, OrdType.PRICE, Decimal(10000), None, None, TradingMode.PAPER
        )
    )
    gw._orders["g"].fills.clear()
    from app.core.types import OrderState

    gw._orders["g"].state = OrderState.OPEN  # exchange shows an open order we don't track
    res = await rc.run([])
    assert not res.open_orders_ok
    last = await repo.last_reconciliation()
    assert last is not None and last.status == "FAIL" and ghost.client_order_id == "g"


async def test_daily_rollover_resets_day_start_equity(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    _, _, _, pf, _ = await _world(tmp_path)
    pf.on_price(Decimal(100_000_000), datetime(2026, 1, 1, 14, 59, tzinfo=UTC))  # 23:59 KST
    pf.state.krw -= Decimal(1000)
    assert pf.daily_pnl() == Decimal(-1000)
    pf.on_price(Decimal(100_000_000), datetime(2026, 1, 1, 15, 0, tzinfo=UTC))  # 00:00 KST next day
    assert pf.daily.day_key == "2026-01-02" and pf.daily_pnl() == 0
