"""schema 同步守卫：建表语句必须与迁移落地后的真实结构一致。

守的是这条不变量 —— `db.SCHEMA` 是库结构的唯一权威描述，读它就该知道表里有哪些列。
历史上它一度落后于迁移：`questions` 少 3 列、`sessions` 少 4 列（含 `status`/`user_id`
这两个到处在用的列），只能把 137 行迁移代码读完才知道表长什么样。

另外守一条更隐蔽的：迁移新增的列**不能**在 `SCHEMA` 里建索引。
`init_db()` 是先执行 SCHEMA、再跑迁移，而 `executescript` 遇错即停 ——
在 DDL 里给"旧库还没有的列"建索引，会让老库直接起不来。
"""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from app.core import config, db

#: 只在迁移里创建、不在 SCHEMA 里的索引（建在迁移新增的列上）
MIGRATION_OWNED_INDEXES = {
    "idx_questions_clean_status",  # v6：questions.clean_status
    "idx_favorites_user",  # v7：favorites.user_id
    "idx_sessions_user_status",  # v7：sessions.user_id + status
}

#: v5 时代的旧库结构：questions 无 clean_* 列、sessions 无 user_id/state_json/status/updated_at、
#: favorites 无 user_id（且 question_id 带单列 UNIQUE）。用来验证老库还能升上来。
_LEGACY_V5_SQL = """
CREATE TABLE questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    source_id     TEXT,
    title         TEXT NOT NULL,
    content       TEXT,
    answer        TEXT,
    tags          TEXT,
    difficulty    TEXT,
    company       TEXT,
    url           TEXT,
    content_hash  TEXT NOT NULL UNIQUE,
    fetched_at    TEXT NOT NULL
);
CREATE TABLE sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL,
    job_title   TEXT,
    jd          TEXT,
    source      TEXT,
    persona     TEXT,
    started_at  TEXT NOT NULL,
    score       INTEGER,
    report      TEXT,
    weak_points TEXT
);
CREATE TABLE favorites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (question_id)
);
PRAGMA user_version = 5;
"""


def _tables(conn: sqlite3.Connection) -> list[str]:
    """真实业务表：排除 sqlite_ 内部表与 FTS5 的虚拟表/影子表（questions_fts*）。"""
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' ORDER BY name"
        )
    ]


def _columns(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """每张表的 (列名, 类型, notnull, 默认值, 主键序号) —— 顺序敏感。"""
    return {t: [tuple(c) for c in conn.execute(f"PRAGMA table_info({t})")] for t in _tables(conn)}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    }


class SchemaSyncTests(unittest.TestCase):
    """SCHEMA（新库路径）与迁移（老库路径）必须收敛到同一结构。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmp.name) / "sync.db")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _conn(self) -> sqlite3.Connection:
        return db.get_conn()

    def test_schema_alone_declares_all_migrated_columns(self):
        """只执行 SCHEMA（不跑迁移）就能建出与迁移后逐列一致的完整表。"""
        with closing(self._conn()) as conn, conn:
            conn.executescript(db.SCHEMA)
            declared = _columns(conn)

        db.init_db()  # 在同一个库里跑迁移，拿到"真实结构"
        with closing(self._conn()) as conn:
            migrated = _columns(conn)

        self.assertEqual(
            sorted(declared),
            sorted(migrated),
            "SCHEMA 建出的表集合与迁移后的表集合不一致",
        )
        for table in sorted(migrated):
            self.assertEqual(
                declared[table],
                migrated[table],
                f"{table} 表的列与建表语句不一致（顺序也要求一致）",
            )

    def test_legacy_v5_db_can_still_upgrade(self):
        """v5 老库（缺 v6/v7 的列）跑 init_db 不能抛异常，且列被补齐。

        这是最容易被"补全建表语句"踩坏的一条：往 SCHEMA 里加一条
        `CREATE INDEX ... ON questions(clean_status)`，老库就会因为"列不存在"起不来。
        """
        legacy = Path(self._tmp.name) / "legacy.db"
        config.DB_PATH = legacy
        with closing(self._conn()) as conn, conn:
            conn.executescript(_LEGACY_V5_SQL)

        db.init_db()  # 不抛异常即通过

        with closing(self._conn()) as conn:
            cols = _columns(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, db.SCHEMA_VERSION)
        self.assertIn("clean_status", {c[1] for c in cols["questions"]})
        self.assertIn("user_id", {c[1] for c in cols["sessions"]})
        self.assertIn("user_id", {c[1] for c in cols["favorites"]})

    def test_migration_owned_indexes_exist_after_init(self):
        """三个建在迁移新增列上的索引，最终必须存在（它们不写在 SCHEMA 里）。"""
        db.init_db()
        with closing(self._conn()) as conn:
            names = _index_names(conn)
        missing = MIGRATION_OWNED_INDEXES - names
        self.assertFalse(missing, f"迁移应创建的索引缺失: {sorted(missing)}")

    def test_schema_version_flag_matches_db(self):
        """SCHEMA_VERSION 必须与迁移落地的 user_version 一致（/health 依赖它）。"""
        db.init_db()
        with closing(self._conn()) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, db.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
