"""幂等迁移：让已经装着 V0.1 数据的库能直接升到 V0.2，不需要重建。

两类迁移：
1. 结构性增量列：新表由 SQLAlchemy 的 create_all 建好，旧表缺的列在这里补上。
2. 数据回填：V0.1 的 Canon 事实没有「生效章号」，回填后才能做时点视图。
所有操作都可重复执行（先探测再改），启动时自动跑。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

#: 表 -> [(列名, 列定义)]
ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "canon_facts": [
        ("valid_from_chapter", "INTEGER"),
        ("valid_until_chapter", "INTEGER"),
    ],
    "chapter_plans": [
        ("steer", "TEXT"),
    ],
    "style_profiles": [
        # V0.5：作者锁定文风后按更严的窗口比对（旧库默认未锁定）
        ("locked", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "novels": [
        # V0.7：全书大纲（旧库默认空字符串，行为与之前一致）
        ("outline", "TEXT DEFAULT ''"),
        # V0.7：发布口径（按章长度期望区间与每日更新目标），旧库按番茄常见值回填
        ("chapter_words_min", "INTEGER NOT NULL DEFAULT 2000"),
        ("chapter_words_max", "INTEGER NOT NULL DEFAULT 3000"),
        ("daily_words_target", "INTEGER NOT NULL DEFAULT 4000"),
    ],
}

SCHEMA_VERSION = "0.5"


def _columns(connection, table: str) -> set[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _table_exists(connection, table: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _model_indexes(table: str) -> dict[str, str]:
    """从 SQLAlchemy 模型里读出「列名 -> 索引名」，用于补列后重建索引。"""
    from app.database import Base

    model = Base.metadata.tables.get(table)
    if model is None:
        return {}
    indexes: dict[str, str] = {}
    for column in model.columns:
        if column.index and not column.primary_key:
            indexes[column.name] = f"ix_{table}_{column.name}"
    return indexes


def migrate(engine: Engine) -> list[str]:
    """补齐增量列（含其索引）并回填历史数据，返回本次实际执行的动作描述。"""
    applied: list[str] = []
    with engine.begin() as connection:
        for table, columns in ADDITIVE_COLUMNS.items():
            if not _table_exists(connection, table):
                continue
            existing = _columns(connection, table)
            indexes = _model_indexes(table)
            for name, ddl in columns:
                if name in existing:
                    continue
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                applied.append(f"add column {table}.{name}")
                index_name = indexes.get(name)
                if index_name:
                    connection.exec_driver_sql(
                        f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({name})"
                    )
                    applied.append(f"add index {index_name}")

        if _table_exists(connection, "canon_facts"):
            existing = _columns(connection, "canon_facts")
            if {"valid_from_chapter", "valid_until_chapter"} <= existing:
                result = connection.execute(
                    text(
                        "UPDATE canon_facts SET valid_from_chapter = COALESCE(source_chapter, 1) "
                        "WHERE valid_from_chapter IS NULL"
                    )
                )
                if result.rowcount:
                    applied.append(f"backfill valid_from_chapter x{result.rowcount}")
                # V0.1 里被取代的事实没有失效章号，用取代它的那条事实的生效章号补上
                result = connection.execute(
                    text(
                        "UPDATE canon_facts SET valid_until_chapter = ("
                        "  SELECT successor.valid_from_chapter FROM canon_facts AS successor "
                        "  WHERE successor.id = canon_facts.superseded_by"
                        ") WHERE valid_until_chapter IS NULL AND superseded_by IS NOT NULL"
                    )
                )
                if result.rowcount:
                    applied.append(f"backfill valid_until_chapter x{result.rowcount}")

        if _table_exists(connection, "meta"):
            connection.execute(
                text(
                    "INSERT INTO meta (key, value, updated_at) VALUES ('schema_version', :version, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(key) DO UPDATE SET value = :version, updated_at = CURRENT_TIMESTAMP"
                ),
                {"version": SCHEMA_VERSION},
            )
    return applied
