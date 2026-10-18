"""Reconciliation: internal accounting vs exchange truth (balances, open orders, position)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger
from app.core.types import OrderRecord
from app.database.repository import Repository
from app.exchange.base import Balances, ExchangeGateway
from app.portfolio.portfolio_manager import PortfolioManager
from app.portfolio.sleeves import LEDGER_KEY_PREFIX, SleeveLedger

# Another strategy that reports an in-flight order, or whose ledger changed by a FILL within this
# window, counts as "settling": a mismatch is re-checked once and then deferred (WARN) instead of
# failing; a persistent mismatch still FAILs on the next run.
OTHER_LEDGER_SETTLE_SEC = 30.0
RECHECK_DELAY_SEC = 3.0

log = get_logger("reconciler")


class ReconcileStatus(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(slots=True)
class ReconcileResult:
    status: ReconcileStatus
    krw_diff: Decimal
    btc_diff: Decimal
    balance_ok: bool
    open_orders_ok: bool
    position_ok: bool
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is ReconcileStatus.OK


class Reconciler:
    def __init__(
        self,
        gateway: ExchangeGateway,
        portfolio: PortfolioManager,
        repo: Repository | None,
        *,
        krw_tolerance: Decimal,
        btc_tolerance: Decimal,
        record_external_flows: bool = False,
    ) -> None:
        self.record_external_flows = record_external_flows
        self._gw = gateway
        self._pf = portfolio
        self._repo = repo
        self.krw_tol = krw_tolerance
        self.btc_tol = btc_tolerance
        self.last: ReconcileResult | None = None

    async def _other_live_ledgers(self) -> list[SleeveLedger]:
        """Ledgers of OTHER strategies that hold real assets (bot_state 'sleeve_ledger_<ID>',
        owner != trader). Shadow/planned ledgers hold nothing real and are ignored."""
        if self._repo is None:
            return []
        rows = await self._repo.get_states_prefix(LEDGER_KEY_PREFIX)
        out: list[SleeveLedger] = []
        for d in rows.values():
            try:
                lg = SleeveLedger.from_dict(d)
            except (KeyError, ValueError, TypeError):
                continue
            if lg.owner == "trader" or not lg.holds_real_assets:
                continue
            out.append(lg)
        return out

    @staticmethod
    def _settling(ledgers: list[SleeveLedger], now: datetime) -> tuple[bool, bool]:
        """(any other ledger in-flight, any other ledger updated within the settle window)."""
        inflight = any(lg.inflight for lg in ledgers)
        recent = False
        for lg in ledgers:
            if not lg.last_trade_at:
                continue
            try:
                age = (now - datetime.fromisoformat(lg.last_trade_at)).total_seconds()
            except ValueError:
                continue
            if 0 <= age <= OTHER_LEDGER_SETTLE_SEC:
                recent = True
        return inflight, recent

    def _balance_check(
        self, bal: Balances, others: list[SleeveLedger]
    ) -> tuple[bool, Decimal, Decimal, Decimal, dict[str, Decimal]]:
        st = self._pf.state
        ex_krw = bal.krw + bal.krw_locked
        ex_btc = bal.btc + bal.btc_locked
        other_cash = sum((lg.cash for lg in others), Decimal(0))
        other_assets: dict[str, Decimal] = {}
        for lg in others:
            if lg.quantity > 0:
                other_assets[lg.asset] = other_assets.get(lg.asset, Decimal(0)) + lg.quantity
        krw_diff = ex_krw - (st.krw + other_cash)
        btc_diff = ex_btc - st.btc
        others_ok = all(
            abs(bal.others.get(asset, Decimal(0)) - qty) <= self.btc_tol
            for asset, qty in other_assets.items()
        ) and all(asset in other_assets or asset == "BTC" for asset in bal.others)
        ok = abs(krw_diff) <= self.krw_tol and abs(btc_diff) <= self.btc_tol and others_ok
        return ok, krw_diff, btc_diff, other_cash, other_assets

    async def run(self, inflight: list[OrderRecord]) -> ReconcileResult:
        bal = await self._gw.get_balances()
        ex_open = await self._gw.get_open_orders()
        st = self._pf.state
        now = datetime.now(UTC)

        # balances: exchange available+locked vs internal (+ other LIVE strategy ledgers, if any)
        others = await self._other_live_ledgers()
        balance_ok, krw_diff, btc_diff, other_cash, other_assets = self._balance_check(bal, others)
        deferred = False
        if not balance_ok and others:
            # Another strategy may have just traded: its exchange balance moved before its ledger
            # row did. Re-check once after a short delay; while it reports in-flight, defer (WARN)
            # instead of failing — a persistent mismatch still FAILs on the next run.
            await asyncio.sleep(RECHECK_DELAY_SEC)
            bal = await self._gw.get_balances()
            others = await self._other_live_ledgers()
            balance_ok, krw_diff, btc_diff, other_cash, other_assets = self._balance_check(
                bal, others
            )
            if not balance_ok:
                other_inflight, other_recent = self._settling(others, datetime.now(UTC))
                deferred = other_inflight or other_recent
        ex_krw = bal.krw + bal.krw_locked
        ex_btc = bal.btc + bal.btc_locked

        # open orders: sets of identifiers must match
        ex_ids = {o.client_order_id for o in ex_open}
        in_ids = {o.client_order_id for o in inflight}
        open_orders_ok = ex_ids == in_ids

        # position: exchange BTC > dust  <=>  internal position  <=>  open trade row
        ex_has = ex_btc > self.btc_tol
        in_has = st.has_position
        trade_ok = (
            (self._pf.open_trade_id is not None) == in_has if self._repo is not None else True
        )
        position_ok = ex_has == in_has and trade_ok

        if balance_ok and open_orders_ok and position_ok:
            status = ReconcileStatus.OK
        elif deferred and open_orders_ok and position_ok:
            status = ReconcileStatus.WARN  # other strategy settling; judged again next run
        else:
            status = ReconcileStatus.FAIL
        # Challenge capital integrity: a KRW/BTC delta not explained by our own fills is an
        # external deposit/withdrawal -> recorded on the challenge state (skews performance).
        if (
            self._repo is not None
            and not balance_ok
            and status is ReconcileStatus.FAIL
            and self.record_external_flows
        ):
            ch = await self._repo.get_state("challenge") or {}
            flows = ch.setdefault("external_flows", [])
            kind = (
                "external_deposit_after_start"
                if (krw_diff > 0 or btc_diff > 0)
                else "external_withdrawal_after_start"
            )
            ch[kind] = str(Decimal(str(ch.get(kind, "0"))) + abs(krw_diff))
            flows.append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "kind": kind,
                    "krw_diff": str(krw_diff),
                    "btc_diff": str(btc_diff),
                }
            )
            ch["external_flows"] = flows[-50:]
            await self._repo.set_state("challenge", ch)
        res = ReconcileResult(
            status=status,
            krw_diff=krw_diff,
            btc_diff=btc_diff,
            balance_ok=balance_ok,
            open_orders_ok=open_orders_ok,
            position_ok=position_ok,
            details={
                "exchange_krw": str(ex_krw),
                "internal_krw": str(st.krw),
                "exchange_btc": str(ex_btc),
                "internal_btc": str(st.btc),
                "exchange_open": sorted(ex_ids),
                "internal_inflight": sorted(in_ids),
                "open_trade_id": self._pf.open_trade_id,
                "other_ledgers_cash": str(other_cash),
                "other_ledgers_assets": {k: str(v) for k, v in other_assets.items()},
                "other_ledgers": [lg.to_dict() for lg in others],
                "exchange_others": {k: str(v) for k, v in bal.others.items()},
                "deferred": deferred,
                "checked_at": now.isoformat(),
            },
        )
        self.last = res
        if self._repo is not None:
            await self._repo.add_reconciliation(
                {
                    "internal_krw": st.krw,
                    "exchange_krw": ex_krw,
                    "internal_btc": st.btc,
                    "exchange_btc": ex_btc,
                    "krw_diff": krw_diff,
                    "btc_diff": btc_diff,
                    "status": status.value,
                }
            )
        if status is ReconcileStatus.FAIL:
            log.error("reconcile_mismatch", **res.details)
        elif status is ReconcileStatus.WARN:
            log.warning("reconcile_deferred_other_strategy_settling", **res.details)
        return res
