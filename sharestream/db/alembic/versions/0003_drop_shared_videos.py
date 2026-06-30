"""drop shared_videos

Removes the legacy SharedVideo table. All individual shares are now
exclusively handled by VideoOverride.

Revision ID: 0003_drop_shared
Revises: 0002_overrides_views
Create Date: 2026-06-30
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_drop_shared"
down_revision: Union[str, None] = "0002_overrides_views"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("shared_videos")


def downgrade() -> None:
    # Downgrade is not supported/meaningful since the data is deleted.
    pass
