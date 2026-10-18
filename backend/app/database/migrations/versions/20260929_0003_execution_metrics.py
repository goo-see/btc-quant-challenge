"""execution_metrics: per-fill slippage / stop distance / exit economics (LIVE preparation)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

M = sa.Numeric(24, 8)


def upgrade() -> None:
    op.create_table(
        "execution_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("strategy_version", sa.String(60), nullable=False),
        sa.Column("client_order_id", sa.String(64), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("signal_id", sa.Integer(), sa.ForeignKey("signals.id"), nullable=True),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("signal_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signal_price", M, nullable=True),
        sa.Column("order_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reference_price_at_send", M, nullable=True),
        sa.Column("actual_fill_price", M, nullable=False),
        sa.Column("slippage_bps", M, nullable=True),
        sa.Column("slippage_vs_reference_bps", M, nullable=True),
        sa.Column("fee", M, nullable=False, server_default="0"),
        sa.Column("quantity", M, nullable=False),
        sa.Column("funds", M, nullable=False),
        sa.Column("fill_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("atr", M, nullable=True),
        sa.Column("initial_stop", M, nullable=True),
        sa.Column("stop_distance_from_signal", M, nullable=True),
        sa.Column("stop_distance_from_fill", M, nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("exit_trigger_price", M, nullable=True),
        sa.Column("realized_pnl", M, nullable=True),
        sa.Column("return_pct", M, nullable=True),
        sa.Column("holding_sec", sa.Integer(), nullable=True),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extra", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("client_order_id", name="uq_execution_metrics_client_order_id"),
    )
    op.create_index("ix_execution_metrics_mode", "execution_metrics", ["mode"])


def downgrade() -> None:
    op.drop_index("ix_execution_metrics_mode", table_name="execution_metrics")
    op.drop_table("execution_metrics")
