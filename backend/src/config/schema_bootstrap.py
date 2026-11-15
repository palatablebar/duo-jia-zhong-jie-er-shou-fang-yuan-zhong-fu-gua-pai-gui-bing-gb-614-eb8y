"""轻量建表/补列：新表由 ``create_all`` 建立；对已存在的表补齐本次迭代新增列。

项目运行时不引入 Alembic，DDL 参考见 migrations/。这里用 inspector 判列后执行
``ALTER TABLE ... ADD COLUMN``，MySQL 8.0 与 SQLite 均支持，缺列才补、可重复执行。
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from src.config.database import engine
from src.models import Base

logger = logging.getLogger("listingmerge.schema")

# 表名 -> [(列名, 列 DDL 片段)]
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "listings": [
        ("ingest_stage", "VARCHAR(20) NOT NULL DEFAULT 'complete'"),
        ("defect_flags", "JSON"),
        ("ingest_batch", "VARCHAR(60)"),
        ("raw_payload", "JSON"),
        ("withdrawn_at", "DATETIME"),
    ],
}


def _quote(inspector, name: str) -> str:
    dialect = inspector.dialect.name.lower()
    if dialect == "mysql":
        return f"`{name}`"
    return f'"{name}"'


def ensure_schema() -> None:
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all 已按新模型建表
            present = {column["name"] for column in inspector.get_columns(table)}
            for column_name, ddl in columns:
                if column_name in present:
                    continue
                conn.execute(
                    text(f"ALTER TABLE {_quote(inspector, table)} ADD COLUMN {_quote(inspector, column_name)} {ddl}")
                )
                logger.info("schema migrated: %s.%s added", table, column_name)
