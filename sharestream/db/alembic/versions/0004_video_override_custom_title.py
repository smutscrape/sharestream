from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_override_custom_title"
down_revision: Union[str, None] = "0003_drop_shared"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("video_overrides", schema=None) as batch_op:
        batch_op.add_column(sa.Column("custom_title", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("video_overrides", schema=None) as batch_op:
        batch_op.drop_column("custom_title")
