"""E1 forward shadow: book semantics (tick ordering, +1R, BE price, stop sources, exits),
persistence/restart, isolation (no order path, no production DB write), crash isolation."""

from __future__ import annotations

import ast
import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.types import Candle, Timeframe
from app.research.e1_forward.books import (
    FEE,
    SLIP,
    BookState,
    break_even_stop,
    open_pair,
    virtual_buy,
)
from app.research.e1_forward.store import ForwardStore
from app.research.round3.market_reader import MarketReader

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
ENTRY_REF = Decimal(116_266_000)
INIT_STOP = Decimal(113_374_924)


def _src(st: BookState) -> str | None:  # defeats mypy attribute narrowing on dataclass defaults
    return st.exit_source


def _exit_price(st: BookState) -> Decimal:
    assert st.exit is not None
    return st.exit.price


def _is_open(st: BookState) -> bool:
    return st.open


def _is_be(st: BookState) -> bool:
    return st.be_activated


def _be_trigger_price(st: BookState) -> Decimal | None:
    return st.be_trigger_price


def _pair() -> tuple[BookState, BookState]:
    fill = virtual_buy(T0, ENTRY_REF, Decimal(30000))
    return open_pair(1, fill, INIT_STOP)


def _t(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_s0_s1_same_entry_and_r_fixed_at_entry() -> None:
    s0, s1 = _pair()
    assert (s0.entry_price, s0.quantity, s0.entry_fee, s0.initial_stop) == (
        s1.entry_price,
        s1.quantity,
        s1.entry_fee,
        s1.initial_stop,
    )
    assert s0.entry_price == ENTRY_REF * (1 + SLIP)
    assert s0.r_unit == s0.entry_price - INIT_STOP and s0.r_unit > 0
    assert s1.be_trigger == s1.entry_price + s1.r_unit
    # R never changes when the trailing stop ratchets
    s1.apply_trailing(INIT_STOP + Decimal(2_000_000), _t(1))
    assert s1.r_unit == s1.entry_price - INIT_STOP and s1.be_trigger == s1.entry_price + s1.r_unit


def test_break_even_stop_covers_both_fees_and_slippage_without_margin() -> None:
    entry = Decimal("116300000")
    be = break_even_stop(entry)
    qty = Decimal("0.00025795")
    net = qty * be * (1 - SLIP) * (1 - FEE) - qty * entry * (1 + FEE)
    assert net >= 0
    assert (
        qty * (be - 1) * (1 - SLIP) * (1 - FEE) - qty * entry * (1 + FEE) < 0
    )  # one won lower would lose
    assert be == Decimal(116_451_294)  # entry*(1+0.0005)/((1-0.0003)(1-0.0005)) rounded up


def test_plus_1r_exact_trigger_activates_only_s1_and_acts_from_next_tick() -> None:
    s0, s1 = _pair()
    below = s1.be_trigger - 1
    for st in (s0, s1):
        st.on_tick(_t(1), below)
    assert not s1.be_activated and not s0.be_activated
    evs = s1.on_tick(_t(2), s1.be_trigger)  # exact touch
    s0.on_tick(_t(2), s1.be_trigger)
    assert _is_be(s1) and evs[0].kind == "BE_ACTIVATED"
    assert _be_trigger_price(s1) == s1.be_trigger
    assert not s0.be_activated and s0.effective_stop()[1] == "INITIAL"
    assert s1.effective_stop() == (s1.be_stop, "BREAK_EVEN")
    # price falls back to BE: S1 exits at ~break-even, S0 stays open
    s1.on_tick(_t(3), s1.be_stop)
    s0.on_tick(_t(3), s1.be_stop)
    assert not _is_open(s1)
    assert _src(s1) == "BREAK_EVEN" and s1.net_pnl() >= Decimal("-1")
    assert s0.open


def test_plus_1r_not_reached_books_identical() -> None:
    s0, s1 = _pair()
    for i, px in enumerate(
        [ENTRY_REF + 1_000_000, ENTRY_REF + 2_500_000, ENTRY_REF + 500_000, INIT_STOP]
    ):
        s0.on_tick(_t(i), px)
        s1.on_tick(_t(i), px)
    assert not s1.be_activated
    assert (s0.exit is not None) and (s1.exit is not None)
    assert (
        s0.exit.price == s1.exit.price
        and s0.exit_reason == s1.exit_reason
        and s0.net_pnl() == s1.net_pnl()
    )


def test_stop_source_selection_and_never_decreases() -> None:
    _, s1 = _pair()
    s1.on_tick(_t(1), s1.be_trigger)
    assert s1.effective_stop() == (s1.be_stop, "BREAK_EVEN")
    s1.apply_trailing(s1.be_stop - Decimal(1_000_000), _t(2))  # trailing below BE -> BE wins
    assert s1.effective_stop()[1] == "BREAK_EVEN"
    s1.apply_trailing(s1.be_stop + Decimal(500_000), _t(3))  # trailing above BE -> trailing wins
    assert s1.effective_stop() == (s1.be_stop + Decimal(500_000), "TRAILING")
    before = s1.effective_stop()[0]
    s1.apply_trailing(before - Decimal(2_000_000), _t(4))  # LIVE stop can never be lower; ignored
    assert s1.effective_stop()[0] == before


def test_same_tick_ordering_stop_before_activation() -> None:
    """A tick can only be both >= trigger and <= stop if the stop is above the trigger; make it so
    (trailing far above) to prove the stop check runs first and BE is never activated on an exit tick."""
    _, s1 = _pair()
    s1.apply_trailing(s1.be_trigger + Decimal(1_000_000), _t(1))
    evs = s1.on_tick(_t(2), s1.be_trigger + Decimal(500_000))
    assert (
        [e.kind for e in evs] == ["EXIT"] and not s1.be_activated and s1.exit_source == "TRAILING"
    )


def test_catchup_bar_is_pessimistic_low_before_high() -> None:
    _, s1 = _pair()
    bar = Candle(
        "KRW-BTC",
        Timeframe.M1,
        _t(1),
        ENTRY_REF,
        s1.be_trigger + 1,
        INIT_STOP - 1,
        ENTRY_REF,
        Decimal(1),
        Decimal(1),
    )
    evs = s1.on_catchup_bar(bar)
    assert evs and evs[0].kind == "EXIT" and not s1.be_activated and evs[0].detail["catchup_bar"]
    _, s1b = _pair()
    bar2 = Candle(
        "KRW-BTC",
        Timeframe.M1,
        _t(1),
        ENTRY_REF,
        s1b.be_trigger + 1,
        ENTRY_REF - 1000,
        ENTRY_REF,
        Decimal(1),
        Decimal(1),
    )
    evs2 = s1b.on_catchup_bar(bar2)
    assert [e.kind for e in evs2] == ["BE_ACTIVATED"] and s1b.open and s1b.last_price == ENTRY_REF


def test_live_sell_mirrored_channel_trend_and_trailing_exits() -> None:
    s0, s1 = _pair()
    ev = s0.on_live_sell(
        _t(10), ENTRY_REF + Decimal(3_000_000), "CHANNEL_EXIT<115000000 (LIVE signal 99)"
    )
    assert ev and not _is_open(s0)
    assert _src(s0) == "LIVE_SIGNAL" and s0.net_pnl() > 0
    assert s1.open  # S1 is told separately by the runner (same observation) — simulate
    s1.on_live_sell(
        _t(10), ENTRY_REF + Decimal(3_000_000), "CHANNEL_EXIT<115000000 (LIVE signal 99)"
    )
    assert not _is_open(s1)
    assert _exit_price(s1) == _exit_price(s0)
    s0b, s1b = _pair()
    s0b.on_live_sell(_t(10), ENTRY_REF - Decimal(1_000_000), "TREND_LOSS_EXIT (LIVE signal 100)")
    assert s0b.net_pnl() < 0 and s1b.open
    # trailing exit after BE: trailing above BE -> source TRAILING on both books
    s0c, s1c = _pair()
    for st in (s0c, s1c):
        st.on_tick(_t(1), s1c.be_trigger + Decimal(2_000_000))
        st.apply_trailing(s1c.be_stop + Decimal(800_000), _t(2))
        st.on_tick(_t(3), s1c.be_stop + Decimal(700_000))
    assert _src(s0c) == "TRAILING" and _src(s1c) == "TRAILING"
    assert _exit_price(s0c) == _exit_price(s1c)


def test_persistence_roundtrip_including_be_state(tmp_path: Path) -> None:
    store = ForwardStore(tmp_path / "f.db")
    s0, s1 = _pair()
    s1.on_tick(_t(1), s1.be_trigger + 1)
    s1.apply_trailing(INIT_STOP + Decimal(1_500_000), _t(2))
    s0.on_tick(_t(1), s1.be_trigger + 1)
    for st in (s0, s1):
        store.save_position(st)
    store.commit()
    restored = {b.book: b for b in ForwardStore(tmp_path / "f.db").load_positions()}
    r1 = restored["S1"]
    assert (
        r1.be_activated
        and r1.be_activated_at == s1.be_activated_at
        and r1.be_stop == s1.be_stop
        and r1.be_trigger == s1.be_trigger
    )
    assert (
        r1.effective_stop() == s1.effective_stop()
        and r1.highest == s1.highest
        and r1.r_unit == s1.r_unit
    )
    assert (
        r1.quantity == s1.quantity
        and r1.entry_fee == s1.entry_fee
        and r1.trailing_stop == s1.trailing_stop
    )
    assert restored["S0"].effective_stop()[1] == "INITIAL" and not restored["S0"].be_activated
    # restart after BE: the very next tick at the BE line exits S1 at break-even
    r1.on_tick(_t(5), r1.be_stop)
    assert not _is_open(r1)
    assert _src(r1) == "BREAK_EVEN"
    store.add_trade(r1)
    store.commit()
    assert store.q("select be_activated, exit_source from trades where book='S1'") == [
        (1, "BREAK_EVEN")
    ]


def test_no_order_path_and_no_secrets_in_e1_package() -> None:
    forbidden = (
        "app.exchange.upbit.rest_client",
        "app.exchange.upbit.live_gateway",
        "app.exchange.upbit.auth",
        "app.execution",
        "app.risk",
        "app.services",
        "app.config.settings",
        "app.database",
        "httpx",
        "requests",
    )
    names_forbidden = {
        "create_order",
        "cancel_order",
        "test_order",
        "place_market_buy",
        "place_market_sell",
        "OrderManager",
        "LiveGateway",
        "UPBIT_ACCESS_KEY",
        "UPBIT_SECRET_KEY",
        "upsert_candles",
        "set_state",
    }
    for f in (ROOT / "backend/app/research/e1_forward").glob("*.py"):
        tree = ast.parse(f.read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        assert not [m for m in mods if m.startswith(forbidden)], (f.name, mods)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }
        assert not (names & names_forbidden), (f.name, names & names_forbidden)
    plist = (ROOT / "scripts/launchd/com.quant.shadow-e1.plist").read_text()
    assert "app.research.e1_forward.runner" in plist and "TRADING_MODE" not in plist


def test_production_db_handle_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "market.db"
    con = sqlite3.connect(db)
    con.execute("create table bot_state (key text primary key, value text)")
    con.execute(
        "create table orders (id integer primary key, mode text, side text, executed_volume real)"
    )
    con.commit()
    con.close()
    reader = MarketReader(db, "KRW-BTC")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.connection.execute("insert into orders values (1,'LIVE','bid',0.1)")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.connection.execute("update bot_state set value='x'")
    reader.close()


async def test_runner_poll_errors_are_isolated(tmp_path: Path) -> None:
    """A broken market DB must not raise out of the poll loop (LIVE is a separate process anyway)."""
    from app.research.e1_forward.runner import E1ForwardRunner

    db = tmp_path / "market.db"
    con = sqlite3.connect(db)
    con.execute("create table bot_state (key text primary key, value text)")
    con.execute(
        "create table candles (market text, timeframe text, open_time text, open, high, low, close, volume, quote_volume, source text)"
    )
    con.commit()
    con.close()  # no orders/signals tables -> poll_entries raises inside
    r = E1ForwardRunner(
        market_db=db, forward_db=tmp_path / "f.db", log_file=tmp_path / "e1.log", poll_sec=0.01
    )
    try:
        r.store.ensure_experiment(
            candidate="E1",
            baseline="S0",
            started_at="x",
            forward_primary_start="2026-10-03T00:00:00+00:00",
            be_formula="f",
            fee="0.0005",
            slip="0.0003",
            git_head="h",
        )
        r.primary_start = T0

        task = asyncio.create_task(r.poll_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert r.errors >= 1 and r.last_error and "no such table" in r.last_error
    finally:
        r.store.close()
        r.market.close()
