"""add passkey credentials

Revision ID: 20260627_0017
Revises: 20260624_0016
Create Date: 2026-06-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260627_0017"
down_revision: str | None = "20260624_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store WebAuthn passkeys for the configured web-login account."""

    op.create_table(
        "passkey_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.String(length=2048), nullable=False),
        sa.Column("user_handle", sa.String(length=128), nullable=False),
        sa.Column("username", sa.String(length=256), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("transports", sa.JSON(), nullable=False),
        sa.Column("aaguid", sa.String(length=80), nullable=False),
        sa.Column("credential_type", sa.String(length=40), nullable=False),
        sa.Column("device_type", sa.String(length=40), nullable=False),
        sa.Column("backed_up", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_passkey_credentials_credential_id"),
        "passkey_credentials",
        ["credential_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_passkey_credentials_user_handle"),
        "passkey_credentials",
        ["user_handle"],
        unique=False,
    )


def downgrade() -> None:
    """Remove stored WebAuthn passkeys."""

    op.drop_index(op.f("ix_passkey_credentials_user_handle"), table_name="passkey_credentials")
    op.drop_index(
        op.f("ix_passkey_credentials_credential_id"),
        table_name="passkey_credentials",
    )
    op.drop_table("passkey_credentials")
