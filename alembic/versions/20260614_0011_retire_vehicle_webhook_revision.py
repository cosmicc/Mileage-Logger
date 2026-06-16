"""retire removed vehicle webhook revision

Revision ID: 20260614_0011
Revises: 20260614_0010
Create Date: 2026-06-14
"""

from collections.abc import Sequence

revision: str = "20260614_0011"
down_revision: str | None = "20260614_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Keep the historical revision chain without creating removed tables."""

    return None


def downgrade() -> None:
    """Keep downgraded databases without recreating removed tables."""

    return None
