"""widen api_calls.segment_id for shot_grid ids

镜头宫格的单元编号形如 ``shot_grid_d7bfc340a12a``（22 字符），旧的 varchar(20) 装不下，
记账 INSERT 直接抛 StringDataRightTruncationError，整张宫格图生成失败。

Revision ID: 8f2c1d4e9b70
Revises: 015359a572fa
Create Date: 2026-10-18 22:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f2c1d4e9b70"
down_revision: str | Sequence[str] | None = "015359a572fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("api_calls", schema=None) as batch_op:
        batch_op.alter_column(
            "segment_id",
            existing_type=sa.String(length=20),
            type_=sa.String(length=64),
            existing_nullable=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    # 只在库内没有超过 20 字符的历史记账时才可回退；有的话截断风险由调用方自负。
    with op.batch_alter_table("api_calls", schema=None) as batch_op:
        batch_op.alter_column(
            "segment_id",
            existing_type=sa.String(length=64),
            type_=sa.String(length=20),
            existing_nullable=True,
        )
