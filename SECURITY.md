# Security notes (public copy)

- Secrets live ONLY in `.env` (never committed): `UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY`, `LIVE_CONFIRM`, `DASHBOARD_CONTROL_TOKEN`.
- `live_gate.enabled`, `strategy_status.*.live_approved`, `live_gate.static_public_ip` are shipped DISABLED/empty.
  Real-money trading requires all three static gates plus the readiness evidence gates (see `docs/00-architecture.md`).
- IP addresses and hostnames in docs/tests are placeholders (RFC 5737 `203.0.113.x`, CGNAT `100.64.0.x`).
- Runtime artefacts (`data/*.db`, `logs/`, `reports/`) are git-ignored; keep them out of any public fork.
