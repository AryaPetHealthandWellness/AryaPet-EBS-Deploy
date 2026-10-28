"""add species_reference_ranges (normal ranges per species, learned from lab reports)

Revision ID: d1f0a7c3e925
Revises: 8ce3677cb8e6
Create Date: 2026-10-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f0a7c3e925"
down_revision: Union[str, None] = "8ce3677cb8e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "species_reference_ranges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("species", sa.String(length=60), nullable=False),
        sa.Column("parameter_key", sa.String(length=120), nullable=False),
        sa.Column("parameter_name", sa.String(length=120), nullable=False),
        sa.Column("reference_range", sa.String(length=120), nullable=False),
        sa.Column("unit", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=200), nullable=True),
        sa.Column(
            "contributed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("species", "parameter_key", name="uq_species_parameter"),
    )
    op.create_index("ix_species_reference_ranges_species", "species_reference_ranges", ["species"])


def downgrade() -> None:
    op.drop_index("ix_species_reference_ranges_species", table_name="species_reference_ranges")
    op.drop_table("species_reference_ranges")
