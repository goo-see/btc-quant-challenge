"""Strategy sleeves: capital isolation, grandfathered position, next D0 sizing from the 50k sleeve,
multi-asset reconciliation without external-flow false positives."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.config.settings import load_yaml_config
from app.portfolio.sleeves import (
    SleeveConfig,
    SleeveLedger,
    reconcile_multi_asset,
    sizing_caps,
    sleeve_snapshot,
)
from app.risk.position_sizer import SizingParams, size_buy_krw

ROOT = Path(__file__).resolve().parents[3]
SZ = SizingParams(Decimal("0.30"), Decimal(30000), Decimal(5000), Decimal("0.0005"), Decimal(1000))


def test_config_has_two_sleeves_50k_each() -> None:
    cfg = load_yaml_config(ROOT / "config/config.yaml")
    assert cfg.portfolio.enabled
    assert (
        cfg.portfolio.sleeves["BTC_D0"].allocated_capital == 50000
        and cfg.portfolio.sleeves["BTC_D0"].asset == "BTC"
    )
    assert cfg.portfolio.sleeves["ETH_GRID"].allocated_capital == 50000 and cfg.portfolio.sleeves[
        "ETH_GRID"
    ].mode in ("SHADOW", "PLANNED", "LIVE_EXPERIMENT")
    assert cfg.portfolio.for_strategy("trend_following_4h_v1") is not None
    assert cfg.portfolio.for_strategy("trend_following_4h_v1")[0] == "BTC_D0"  # type: ignore[index]


def test_d0_cannot_spend_eth_reserve_and_next_entry_is_15k() -> None:
    # wallet: ₩70,024 cash, no position -> D0 may only see min(cash, 50k) and sizes from 50k
    eq, cash = sizing_caps(Decimal(50000), Decimal(70024), Decimal(70024), Decimal(0))
    assert eq == 50000 and cash == 50000
    amount, why = size_buy_krw(eq, cash, SZ)
    assert amount == 15000 and why == "OK"
    # with the grandfathered 30k position open: sizing equity stays capped at 50k, cash 20k -> no 2nd entry anyway
    eq2, cash2 = sizing_caps(Decimal(50000), Decimal(99853), Decimal(70024), Decimal(29830))
    assert eq2 == 50000 and cash2 == Decimal(50000 - 29830)


def test_grid_cannot_spend_d0_reserve() -> None:
    # ETH sleeve sees at most its allocation even if the wallet holds more cash
    _, cash = sizing_caps(Decimal(50000), Decimal(70024), Decimal(70024), Decimal(0))
    assert cash == 50000
    # and once it holds 40k ETH inventory only 10k remains available to it (min reserve enforced by the grid)
    _, cash2 = sizing_caps(Decimal(50000), Decimal(70024), Decimal(30024), Decimal(40000))
    assert cash2 == 10000


def test_current_btc_position_is_grandfathered_not_reduced() -> None:
    sc = SleeveConfig(
        asset="BTC", allocated_capital=Decimal(50000), strategy_version="trend_following_4h_v1"
    )
    snap = sleeve_snapshot(
        "BTC_D0",
        sc,
        cash=Decimal("70024.91"),
        quantity=Decimal("0.00025795"),
        price=Decimal(115611000),
        realized_pnl=Decimal(0),
        fees=Decimal(15),
        entry_fees=Decimal(15),
        avg_entry=Decimal(116300000),
    )
    assert snap["grandfathered_position"] is True  # 29.8k > 30% of 50k
    assert Decimal(snap["position_value"]) == Decimal("0.00025795") * 115611000
    assert snap["next_max_entry_krw"] == "15000"
    assert Decimal(snap["available_to_strategy"]) == Decimal(50000) - Decimal(
        snap["position_value"]
    )
    assert Decimal(snap["cash_reserved"]) + Decimal(snap["position_value"]) == 50000


def test_multi_asset_reconcile_d0_position_plus_eth_buy_is_not_external_flow() -> None:
    tol = {"krw_tolerance": Decimal(100), "coin_tolerance": Decimal("0.00001")}
    d0 = SleeveLedger("BTC_D0", "BTC", Decimal(20024), Decimal("0.00025795"))
    eth = SleeveLedger("ETH_GRID", "ETH", Decimal(25000), Decimal("0.0068"), mode="LIVE")
    # D0 holds BTC, ETH grid just bought 25k of ETH: wallet shows the combined truth -> PASS
    r = reconcile_multi_asset(
        {"KRW": Decimal(45024), "BTC": Decimal("0.00025795"), "ETH": Decimal("0.0068")},
        [d0, eth],
        **tol,
    )
    assert r.ok, r.detail
    # ETH holds inventory, D0 sells its BTC (cash back to D0 ledger) -> PASS
    d0b = SleeveLedger("BTC_D0", "BTC", Decimal(49800), Decimal(0))
    r2 = reconcile_multi_asset(
        {"KRW": Decimal(74800), "BTC": Decimal(0), "ETH": Decimal("0.0068")}, [d0b, eth], **tol
    )
    assert r2.ok
    # a real external withdrawal of 10k is still detected
    r3 = reconcile_multi_asset(
        {"KRW": Decimal(64800), "BTC": Decimal(0), "ETH": Decimal("0.0068")}, [d0b, eth], **tol
    )
    assert not r3.ok and "KRW -10000" in r3.detail
    # a shadow ledger holds no real assets and is ignored
    shadow = SleeveLedger("ETH_GRID", "ETH", Decimal(25000), Decimal("0.0068"), mode="SHADOW")
    r4 = reconcile_multi_asset(
        {"KRW": Decimal(20024), "BTC": Decimal("0.00025795")}, [d0, shadow], **tol
    )
    assert r4.ok
    # unexpected coin on the exchange (not owned by any ledger) is a mismatch
    r5 = reconcile_multi_asset(
        {"KRW": Decimal(20024), "BTC": Decimal("0.00025795"), "ETH": Decimal("0.01")},
        [d0, shadow],
        **tol,
    )
    assert not r5.ok


def test_both_positions_restart_restore_reconcile_pass() -> None:
    """Ledgers persisted as plain dicts round-trip and reconcile after a 'restart'."""
    ledgers = [
        SleeveLedger("BTC_D0", "BTC", Decimal(20024), Decimal("0.00025795")),
        SleeveLedger("ETH_GRID", "ETH", Decimal(21000), Decimal("0.0079")),
    ]
    saved = [lg.to_dict() for lg in ledgers]
    restored = [
        SleeveLedger(
            d["strategy_id"], d["asset"], Decimal(d["cash"]), Decimal(d["quantity"]), d["mode"]
        )
        for d in saved
    ]
    r = reconcile_multi_asset(
        {"KRW": Decimal(41024), "BTC": Decimal("0.00025795"), "ETH": Decimal("0.0079")},
        restored,
        krw_tolerance=Decimal(100),
        coin_tolerance=Decimal("0.00001"),
    )
    assert r.ok and r.expected["KRW"] == 41024
