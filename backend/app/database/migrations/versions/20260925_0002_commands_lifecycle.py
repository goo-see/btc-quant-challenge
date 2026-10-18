"""commands lifecycle: command_id, status, TTL, audit

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("commands") as b:
        b.add_column(sa.Column("command_id", sa.String(64), nullable=True))
        b.add_column(sa.Column("status", sa.String(12), nullable=False, server_default="PENDING"))
        b.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("failure_reason", sa.Text(), nullable=True))
        b.add_column(sa.Column("audit", sa.JSON(), nullable=False, server_default="{}"))
        b.alter_column("requested_by", type_=sa.String(120))
    # legacy rows: synthesize ids, mark processed ones EXECUTED and unprocessed ones EXPIRED
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE commands SET command_id = 'legacy-' || id WHERE command_id IS NULL")
    )
    conn.execute(
        sa.text(
            "UPDATE commands SET status = CASE WHEN processed_at IS NULL THEN 'EXPIRED' "
            "ELSE 'EXECUTED' END"
        )
    )
    with op.batch_alter_table("commands") as b:
        b.alter_column("command_id", nullable=False)
        b.create_unique_constraint("uq_commands_command_id", ["command_id"])
        b.create_index("ix_commands_status", ["status"])


def downgrade() -> None:
    with op.batch_alter_table("commands") as b:
        b.drop_index("ix_commands_status")
        b.drop_constraint("uq_commands_command_id", type_="unique")
        for col in (
            "command_id",
            "status",
            "expires_at",
            "acknowledged_at",
            "executed_at",
            "failure_reason",
            "audit",
        ):
            b.drop_column(col)
