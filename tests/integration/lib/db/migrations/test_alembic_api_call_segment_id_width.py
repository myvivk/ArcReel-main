"""api_calls.segment_id 宽度迁移的双向测试。

镜头宫格单元编号 `shot_grid_<12 hex>` 有 22 个字符，旧的 varchar(20) 会让记账 INSERT 抛
StringDataRightTruncationError。SQLite 不校验 varchar 长度，所以这里直接断言列定义。
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from alembic import command

_PREVIOUS_REVISION = "015359a572fa"
_REVISION = "8f2c1d4e9b70"


def _sync_engine(db_path: Path) -> sa.Engine:
    return sa.create_engine(f"sqlite:///{db_path}")


def _segment_id_length(engine: sa.Engine) -> int | None:
    columns = sa.inspect(engine).get_columns("api_calls")
    segment_id = next(column for column in columns if column["name"] == "segment_id")
    return getattr(segment_id["type"], "length", None)


def test_upgrade_widens_segment_id_to_fit_shot_grid_ids(alembic_cfg):
    cfg, db_path = alembic_cfg
    command.upgrade(cfg, _PREVIOUS_REVISION)
    assert _segment_id_length(_sync_engine(db_path)) == 20

    command.upgrade(cfg, _REVISION)

    length = _segment_id_length(_sync_engine(db_path))
    assert length is not None
    assert length >= len("shot_grid_d7bfc340a12a")


def test_downgrade_restores_the_original_width(alembic_cfg):
    cfg, db_path = alembic_cfg
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, _PREVIOUS_REVISION)

    assert _segment_id_length(_sync_engine(db_path)) == 20
