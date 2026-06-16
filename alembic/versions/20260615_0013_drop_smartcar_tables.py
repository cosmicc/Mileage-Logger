"""drop removed vehicle webhook tables

Revision ID: 20260615_0013
Revises: 20260615_0012
Create Date: 2026-06-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260615_0013"
down_revision: str | None = "20260615_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop obsolete external vehicle webhook tables when they exist."""

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("smartcar_webhook_signals"):
        op.drop_table("smartcar_webhook_signals")
    if inspector.has_table("smartcar_webhook_events"):
        op.drop_table("smartcar_webhook_events")


def downgrade() -> None:
    """Keep downgraded databases without the removed external integration tables."""

    return None
