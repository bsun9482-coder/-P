"""SQLite 连接与行转换：各领域模块共用的最底层。

本模块只负责"怎么连、怎么把行变成 dict"，不含任何业务语义。
"""

import sqlite3

from app.core import config


def get_conn() -> sqlite3.Connection:
    """获取数据库连接（开启 busy_timeout 与 WAL 友好配置）。"""
    config.ensure_data_dir()
    conn = sqlite3.connect(config.DB_PATH, timeout=config.DB_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.DB_TIMEOUT_SECONDS * 1000}")
    conn.execute("PRAGMA synchronous = NORMAL")
    # 外键约束默认关闭，schema 里的 REFERENCES/CASCADE 全部失效
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """把 sqlite3.Row 转为 dict，让调用方安全使用 .get() 等 dict 语义。"""
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    """把 sqlite3.Row 列表转为 dict 列表。"""
    return [dict(r) for r in rows]
