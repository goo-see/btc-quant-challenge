"""Private API preflight (LIVE readiness evidence). NEVER creates a real order.

Order: credentials -> static public IP gate -> accounts -> orders/chance -> orders/test.
Every outcome (including BLOCKED) is recorded in bot_state['order_test'] for the readiness panel.
Secrets are never logged; only booleans, counts and Upbit's public error names are stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.config.settings import Settings
from app.core.logging import get_logger
from app.database.repository import Repository
from app.exchange.upbit.errors import UpbitAuthError, UpbitError, UpbitHTTPError
from app.exchange.upbit.rest_client import UpbitRestClient
from app.monitoring.network import evaluate_static_ip

log = get_logger("preflight")


@dataclass(slots=True)
class PreflightResult:
    ok: bool
    blocked_reason: str | None
    steps: list[dict[str, Any]] = field(default_factory=list)
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"name": name, "ok": ok, "detail": detail})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blocked_reason": self.blocked_reason,
            "steps": self.steps,
            "at": self.at,
        }


def funding_check_required(challenge: dict[str, Any]) -> bool:
    """The ₩100,000 ± tolerance / BTC = 0 funding gate applies BEFORE LIVE activation only.
    Once LIVE is activated (books initialised from the exchange) or the challenge has started (a
    position may be open, fees paid), a restart must not be blocked by the principal check —
    integrity is then enforced by reconciliation + external-flow detection."""
    return str(challenge.get("status") or "NOT_STARTED") not in {
        "LIVE_ACTIVATED_WAITING_FIRST_BUY",
        "STARTED",
    }


async def run_preflight(settings: Settings, repo: Repository) -> PreflightResult:
    cfg = settings.config.live_gate
    res = PreflightResult(ok=False, blocked_reason=None)

    # 1) credentials (presence only; values never logged)
    if not settings.env.has_credentials:
        res.step("credentials", False, "UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY not set in .env")
        res.blocked_reason = "CREDENTIALS_MISSING"
        await repo.set_state("order_test", res.to_dict())
        return res
    res.step("credentials", True, "present")

    # 2) static public IP gate
    ip_state = await repo.get_state("public_ip") or {}
    verdict = evaluate_static_ip(
        cfg.static_public_ip or None,
        list(ip_state.get("history", [])),
        min_hours=cfg.static_ip_min_observation_hours,
    )
    res.step("static_public_ip", verdict.passed or verdict.pending, verdict.reason)
    if not verdict.passed and not verdict.pending:
        # not configured / mismatch / inconsistent -> do not even touch private endpoints
        res.blocked_reason = "STATIC_PUBLIC_IP_NOT_VERIFIED"
        await repo.set_state("order_test", res.to_dict())
        return res
    # pending (same IP, <24h): the read-only smoke may proceed; LIVE stays blocked via readiness

    assert settings.env.upbit_access_key is not None and settings.env.upbit_secret_key is not None
    market = settings.config.system.market
    async with UpbitRestClient(
        settings.env.upbit_access_key.get_secret_value(),
        settings.env.upbit_secret_key.get_secret_value(),
    ) as client:
        # 3) accounts
        try:
            accts = await client.get_accounts()
            krw = next((a.balance for a in accts if a.currency == "KRW"), Decimal(0))
            krw_locked = next((a.locked for a in accts if a.currency == "KRW"), Decimal(0))
            btc = next((a.balance for a in accts if a.currency == "BTC"), Decimal(0))
            res.step("accounts", True, f"{len(accts)} assets, KRW available {krw:,.0f}")
            await repo.set_state(
                "exchange_balance",
                {
                    "krw": str(krw),
                    "krw_locked": str(krw_locked),
                    "btc": str(btc),
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        except UpbitAuthError as exc:
            res.step(
                "accounts",
                False,
                f"auth error {exc.status} {exc.name} (check key permissions / IP whitelist)",
            )
            res.blocked_reason = "AUTH_FAILED"
            await repo.set_state("order_test", res.to_dict())
            return res
        except UpbitError as exc:
            res.step("accounts", False, f"error {exc}")
            res.blocked_reason = "ACCOUNTS_FAILED"
            await repo.set_state("order_test", res.to_dict())
            return res
        # 4) orders/chance (fees + min order from the exchange)
        try:
            ch = await client.get_order_chance(market)
            res.step(
                "orders_chance",
                True,
                f"bid_fee={ch.bid_fee} ask_fee={ch.ask_fee} "
                f"min_total={ch.market.bid.min_total} state={ch.market.state}",
            )
            await repo.set_state(
                "exchange_terms",
                {
                    "bid_fee": str(ch.bid_fee),
                    "ask_fee": str(ch.ask_fee),
                    "min_total": str(ch.market.bid.min_total),
                    "max_total": str(ch.market.max_total),
                    "state": ch.market.state,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        except UpbitError as exc:
            res.step("orders_chance", False, f"error {exc}")
            res.blocked_reason = "ORDERS_CHANCE_FAILED"
            await repo.set_state("order_test", res.to_dict())
            return res
        # 5) orders/test — dry run only
        try:
            o = await client.test_order(
                market=market,
                side="bid",
                ord_type="price",
                price=cfg.order_test_krw,
                identifier=f"preflight-{datetime.now(UTC):%Y%m%d%H%M%S}",
            )
            res.step("orders_test", True, f"dry-run accepted (state={o.state}, no real order)")
        except UpbitHTTPError as exc:
            res.step("orders_test", False, f"{exc.status} {exc.name}: {exc.message}")
            res.blocked_reason = f"ORDER_TEST_REJECTED_{(exc.name or 'unknown').upper()}"
            await repo.set_state("order_test", res.to_dict())
            return res
        except UpbitError as exc:
            res.step("orders_test", False, f"error {exc}")
            res.blocked_reason = "ORDER_TEST_FAILED"
            await repo.set_state("order_test", res.to_dict())
            return res
    # 6) challenge capital integrity: exchange KRW must equal the ₩100,000 principal (± tolerance)
    #    — enforced before LIVE activation only (a LIVE restart with an open position must pass)
    initial = settings.config.challenge.initial_capital_krw
    tol = settings.config.challenge.funding_tolerance_krw
    chal = await repo.get_state("challenge") or {}
    required = funding_check_required(chal)
    funded = abs((krw + krw_locked) - initial) <= tol and btc == 0
    res.step(
        "challenge_capital",
        funded or not required,
        f"KRW {krw + krw_locked:,.0f} vs initial {initial:,.0f} (±{tol:,.0f}), BTC {btc}"
        + (
            ""
            if funded
            else (
                f" (not enforced: challenge {chal.get('status')})"
                if not required
                else " -> CHALLENGE_CAPITAL_NOT_FUNDED"
            )
        ),
    )
    chal.update(
        {
            "initial_capital": str(initial),
            "funded": funded,
            "funded_checked_at": datetime.now(UTC).isoformat(),
            "status": chal.get("status") or "NOT_STARTED",
        }
    )
    await repo.set_state("challenge", chal)
    if not funded and required:
        res.blocked_reason = "CHALLENGE_CAPITAL_NOT_FUNDED"
        res.ok = False
        res.steps[-1]["detail"] += " (private API steps PASS; funding step blocks)"
        await repo.set_state("order_test", {**res.to_dict(), "api_ok": True})
        return res
    res.ok = True
    await repo.set_state("order_test", {**res.to_dict(), "api_ok": True})
    log.info("preflight_ok", steps=[s["name"] for s in res.steps])
    return res
