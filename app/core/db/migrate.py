"""建库与版本迁移。

`init_db()` 是唯一的入口：建表（SCHEMA）→ 按 `PRAGMA user_version` 逐级迁移。
**迁移段落是历史记录，只能追加、不能改写或删除** —— 老库靠它们才能升上来。
新增字段时：改 `schema.py` 的 DDL（让新库一次到位）+ 在本文件追加一段迁移（让老库补列）+ 升 `SCHEMA_VERSION`。
"""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from app.core.db.conn import get_conn
from app.core.db.schema import FTS_SCHEMA, FTS_TRIGRAM_SCHEMA, SCHEMA
from app.core.db.users import _token_hash

logger = logging.getLogger("interview_coach.db")


def init_db() -> None:
    """建库建表 + 执行迁移（幂等，可反复调用）。"""
    with closing(get_conn()) as conn, conn:
        # WAL 让读写不再互斥：爬虫批量写、语音 WS 写与用户请求读可并发，
        # 否则默认回滚日志的写锁全库独占，并发时 database is locked
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """基于 PRAGMA user_version 的版本迁移。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        try:
            conn.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError as e:
            logger.warning("FTS5 不可用（%s），全文检索将回退 LIKE 检索", e)
        conn.execute("PRAGMA user_version = 1")
        logger.info("数据库迁移至版本 1：新增 FTS5 全文索引")
    if version < 2:
        conn.execute("PRAGMA user_version = 2")
        logger.info("数据库迁移至版本 2：新增面试记录表（sessions / session_answers）")
    if version < 3:
        q_cols = {r[1] for r in conn.execute("PRAGMA table_info(questions)")}
        if "company" not in q_cols:
            conn.execute("ALTER TABLE questions ADD COLUMN company TEXT")
        s_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "persona" not in s_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN persona TEXT")
        conn.execute("PRAGMA user_version = 3")
        logger.info("数据库迁移至版本 3：新增收藏表、题目公司标签与面试官人格")
    if version < 4:
        try:
            conn.executescript(FTS_TRIGRAM_SCHEMA)
            conn.execute("INSERT INTO questions_fts_tr(questions_fts_tr) VALUES('rebuild')")
        except sqlite3.OperationalError as e:
            logger.warning("FTS5 trigram 索引创建失败（%s），中文子串检索将回退", e)
        conn.execute("PRAGMA user_version = 4")
        logger.info("数据库迁移至版本 4：新增 trigram 全文索引（中文子串检索）")
    if version < 5:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fetched_categories (
                source      TEXT NOT NULL,
                category    TEXT NOT NULL,
                new_count   INTEGER NOT NULL DEFAULT 0,
                fetched_at  TEXT NOT NULL,
                PRIMARY KEY (source, category)
            )
            """
        )
        conn.execute("PRAGMA user_version = 5")
        logger.info("数据库迁移至版本 5：新增懒加载补抓记录表（fetched_categories）")
    if version < 6:
        q_cols = {r[1] for r in conn.execute("PRAGMA table_info(questions)")}
        if "clean_status" not in q_cols:
            conn.execute(
                "ALTER TABLE questions ADD COLUMN clean_status TEXT NOT NULL DEFAULT 'raw'"
            )
        if "clean_version" not in q_cols:
            conn.execute("ALTER TABLE questions ADD COLUMN clean_version TEXT")
        if "cleaned_at" not in q_cols:
            conn.execute("ALTER TABLE questions ADD COLUMN cleaned_at TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_questions_clean_status ON questions(clean_status)"
        )
        conn.execute("PRAGMA user_version = 6")
        logger.info("数据库迁移至版本 6：新增清洗状态字段（clean_status/clean_version/cleaned_at）")
    if version < 7:
        # 多用户改造：账号 / 令牌 / 按用户定制面试；favorites 与 sessions 加 user_id。
        # favorites 原先对 question_id 有 UNIQUE 约束（无法多用户收藏同一题），需重建表。
        f_cols = {r[1] for r in conn.execute("PRAGMA table_info(favorites)")}
        if "user_id" not in f_cols:
            # 重建 favorites 表以支持多用户收藏。不用 executescript（内部会隐式提交，
            # 破坏外层事务，DROP+RENAME 崩溃中间态不可恢复，藏品会遗留在孤儿表中）；
            # 改用逐条 execute 让整个迁移共享同一事务，崩溃可原子回滚。
            conn.execute("DROP TABLE IF EXISTS favorites_new")
            conn.execute(
                """CREATE TABLE favorites_new (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     INTEGER,
                    question_id INTEGER NOT NULL REFERENCES questions(id),
                    created_at  TEXT NOT NULL,
                    UNIQUE (user_id, question_id)
                )"""
            )
            conn.execute(
                """INSERT INTO favorites_new (user_id, question_id, created_at)
                   SELECT NULL, question_id, created_at FROM favorites"""
            )
            conn.execute("DROP TABLE favorites")
            conn.execute("ALTER TABLE favorites_new RENAME TO favorites")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id)")
        s_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "user_id" not in s_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER")
        if "state_json" not in s_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN state_json TEXT")
        if "status" not in s_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
        if "updated_at" not in s_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN updated_at TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user_status ON sessions(user_id, status)"
        )
        conn.execute("PRAGMA user_version = 7")
        logger.info(
            "数据库迁移至版本 7：新增用户/令牌/按用户定制面试表，favorites 与 sessions 支持多用户"
        )
    if version < 8:
        # 登录令牌改存 SHA-256 哈希：重建 auth_tokens，明文行逐条哈希迁移。
        # SHA-256 为确定性哈希，迁移后客户端手中的明文令牌在下次请求时被服务端
        # 哈希后照常匹配，存量登录态不失效（用户不掉线）；顺带清理过期行。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(auth_tokens)")}
        if "token" in cols:  # 旧结构（token 明文列）才需要重建
            rows = conn.execute(
                "SELECT token, user_id, created_at, expires_at FROM auth_tokens"
            ).fetchall()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_tokens_new (
                    token_hash TEXT PRIMARY KEY,
                    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            now = datetime.now(timezone.utc).isoformat()
            for r in rows:
                if r["expires_at"] <= now:
                    continue
                tok = r["token"]
                if len(tok) != 64:  # 64 位 hex 视为已哈希（幂等保护），否则视为明文
                    tok = _token_hash(tok)
                conn.execute(
                    "INSERT OR IGNORE INTO auth_tokens_new "
                    "(token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                    (tok, r["user_id"], r["created_at"], r["expires_at"]),
                )
            conn.execute("DROP TABLE auth_tokens")
            conn.execute("ALTER TABLE auth_tokens_new RENAME TO auth_tokens")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id)")
        conn.execute("PRAGMA user_version = 8")
        logger.info("数据库迁移至版本 8：登录令牌改存 SHA-256 哈希，新增 WS 一次性票据表")
    _sync_fts(conn)


def _sync_fts(conn: sqlite3.Connection) -> None:
    """FTS 行数与主表不一致时重建索引（外部内容表需手动同步）。"""
    try:
        n = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        f = conn.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0]
    except sqlite3.OperationalError:
        return
    if n != f:
        conn.execute("INSERT INTO questions_fts(questions_fts) VALUES('rebuild')")
        logger.info("已重建 FTS 索引（%s -> %s）", f, n)
    try:
        t = conn.execute("SELECT COUNT(*) FROM questions_fts_tr").fetchone()[0]
        if n != t:
            conn.execute("INSERT INTO questions_fts_tr(questions_fts_tr) VALUES('rebuild')")
            logger.info("已重建 trigram FTS 索引（%s -> %s）", t, n)
    except sqlite3.OperationalError:
        pass
