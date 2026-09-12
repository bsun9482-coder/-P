"""SQLite 数据层：建库、题目去重入库、多条件检索、FTS5 全文检索。

设计要点：
- 去重：content_hash（归一化 SHA-256）唯一，INSERT OR IGNORE 增量入库；
- 连接：每次操作显式关闭（`with closing(...) as conn, conn:`），并设置 busy_timeout；
- 批量写入：executemany + 单事务，避免逐行提交（全量抓取从 ~3.5 分钟降到数秒级）；
- 全文检索：FTS5 外部内容表（标题/题干/答案/标签），不可用时回退 LIKE；
- 迁移：PRAGMA user_version 管理 schema 版本。
"""

import hashlib
import json
import logging
import re
import sqlite3
from contextlib import closing, suppress
from datetime import datetime, timezone

from app.core import config

logger = logging.getLogger("interview_coach.db")

#: 当前 schema 版本，与 _migrate 的最终 PRAGMA user_version 保持一致；变更 schema 时同步更新
SCHEMA_VERSION = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,              -- 来源标识：mianshiya / leetcode / nowcoder / ...
    source_id     TEXT,                       -- 源站题号/页面ID（便于反查）
    title         TEXT NOT NULL,              -- 题干（短问题）
    content       TEXT,                       -- 详细题干/描述
    answer        TEXT,                       -- 参考答案（源站如有，可为空）
    tags          TEXT,                       -- 逗号分隔的标签，如 "Python,GIL"
    difficulty    TEXT,                       -- 难度：简单/中等/困难（或算法题 easy/medium/hard）
    company       TEXT,                       -- 公司维度标签（如 字节跳动 / 阿里 / 腾讯）
    url           TEXT,                       -- 来源链接
    content_hash  TEXT NOT NULL UNIQUE,       -- 归一化去重哈希
    fetched_at    TEXT NOT NULL               -- 抓取时间（ISO 8601）
);

CREATE INDEX IF NOT EXISTS idx_questions_source ON questions(source);
CREATE INDEX IF NOT EXISTS idx_questions_tags ON questions(tags);
CREATE INDEX IF NOT EXISTS idx_questions_difficulty ON questions(difficulty);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL,              -- mock / coach
    job_title   TEXT,                       -- 定制面试目标岗位
    jd          TEXT,                       -- 定制面试招聘信息
    source      TEXT,                       -- 题库 / 定制
    persona     TEXT,                       -- 面试官人格（一面/二面/三面）
    started_at  TEXT NOT NULL,              -- 开始时间（ISO 8601）
    score       INTEGER,                    -- 报告总分（0-100）
    report      TEXT,                       -- 总结报告全文
    weak_points TEXT                        -- 薄弱点清单（每行一条）
);

CREATE TABLE IF NOT EXISTS session_answers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL REFERENCES sessions(id),
    stage          TEXT,                    -- 阶段名（如 Python基础 / 定制题 1）
    question_title TEXT,
    answer         TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_answers_sid ON session_answers(session_id);

CREATE TABLE IF NOT EXISTS favorites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,                             -- 所属用户（NULL=历史遗留的全局收藏）
    question_id INTEGER NOT NULL REFERENCES questions(id),
    created_at  TEXT NOT NULL,
    UNIQUE (user_id, question_id)
);
-- 注意：idx_favorites_user 索引由迁移 v7 统一创建（旧库的 favorites 重建后才有 user_id）

-- 懒加载补抓记录：记录某个数据源的某分类是否已按需抓取过（避免重复抓取）
CREATE TABLE IF NOT EXISTS fetched_categories (
    source      TEXT NOT NULL,
    category    TEXT NOT NULL,
    new_count   INTEGER NOT NULL DEFAULT 0,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (source, category)
);

-- ---- 多用户：账号、登录令牌、定制面试（按用户隔离）----
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- 登录名（不区分大小写）
    password_hash TEXT NOT NULL,                         -- pbkdf2 密码散列
    nickname      TEXT,                                  -- 昵称（显示名）
    persona       TEXT,                                  -- 默认面试官人格
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash TEXT PRIMARY KEY,                          -- 登录令牌的 SHA-256 哈希（明文不落库）
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id);

-- 语音 WS 一次性连接票据（URL 只出现短时票据，长效令牌不出 Bearer 头）
CREATE TABLE IF NOT EXISTS ws_tickets (
    ticket_hash TEXT PRIMARY KEY,                         -- 票据的 SHA-256 哈希（单次消费）
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ws_tickets_user ON ws_tickets(user_id);

-- 定制面试（文字版生成，语音接通时读取；按用户隔离，取代旧单文件 voice_store）
CREATE TABLE IF NOT EXISTS custom_interviews (
    user_id        INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    job_title      TEXT,
    jd             TEXT,
    questions_json TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
"""

#: FTS5 外部内容表：与 questions 通过 rowid 关联，触发器保持同步
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts USING fts5(
    title, content, answer, tags,
    content='questions',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS questions_ai AFTER INSERT ON questions BEGIN
    INSERT INTO questions_fts(rowid, title, content, answer, tags)
    VALUES (new.id, new.title, new.content, new.answer, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS questions_ad AFTER DELETE ON questions BEGIN
    INSERT INTO questions_fts(questions_fts, rowid, title, content, answer, tags)
    VALUES ('delete', old.id, old.title, old.content, old.answer, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS questions_au AFTER UPDATE ON questions BEGIN
    INSERT INTO questions_fts(questions_fts, rowid, title, content, answer, tags)
    VALUES ('delete', old.id, old.title, old.content, old.answer, old.tags);
    INSERT INTO questions_fts(rowid, title, content, answer, tags)
    VALUES (new.id, new.title, new.content, new.answer, new.tags);
END;
"""

#: trigram 全文索引：按 3 字符子串建索引，中文子串/组合词匹配远好于 unicode61 逐字索引。
#: 要求查询词 ≥3 字符（短词由 _fts_search 回退到 unicode61 / LIKE）。
FTS_TRIGRAM_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts_tr USING fts5(
    title, content, answer, tags,
    content='questions',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS questions_tr_ai AFTER INSERT ON questions BEGIN
    INSERT INTO questions_fts_tr(rowid, title, content, answer, tags)
    VALUES (new.id, new.title, new.content, new.answer, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS questions_tr_ad AFTER DELETE ON questions BEGIN
    INSERT INTO questions_fts_tr(questions_fts_tr, rowid, title, content, answer, tags)
    VALUES ('delete', old.id, old.title, old.content, old.answer, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS questions_tr_au AFTER UPDATE ON questions BEGIN
    INSERT INTO questions_fts_tr(questions_fts_tr, rowid, title, content, answer, tags)
    VALUES ('delete', old.id, old.title, old.content, old.answer, old.tags);
    INSERT INTO questions_fts_tr(rowid, title, content, answer, tags)
    VALUES (new.id, new.title, new.content, new.answer, new.tags);
END;
"""

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")

#: 题目清洗状态机：raw(未洗) → rule_cleaned(规则清洗) → semantic_cleaned(LLM 语义清洗) / ready(达标)
CLEAN_STATUS_RAW = "raw"
CLEAN_STATUS_RULE = "rule_cleaned"
CLEAN_STATUS_SEMANTIC = "semantic_cleaned"
CLEAN_STATUS_READY = "ready"
CLEAN_STATUSES = (
    CLEAN_STATUS_RAW,
    CLEAN_STATUS_RULE,
    CLEAN_STATUS_SEMANTIC,
    CLEAN_STATUS_READY,
)


def get_conn() -> sqlite3.Connection:
    """获取数据库连接（开启 busy_timeout 与 WAL 友好配置）。"""
    config.ensure_data_dir()
    conn = sqlite3.connect(config.DB_PATH, timeout=config.DB_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.DB_TIMEOUT_SECONDS * 1000}")
    conn.execute("PRAGMA synchronous = NORMAL")
    # 外键约束默认关闭，schema 里的 REFERENCES/CASCADE 全部失效（bug #24）
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """把 sqlite3.Row 转为 dict，让调用方安全使用 .get() 等 dict 语义。"""
    return dict(row) if row is not None else None


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    """把 sqlite3.Row 列表转为 dict 列表。"""
    return [dict(r) for r in rows]


def init_db() -> None:
    """建库建表 + 执行迁移（幂等，可反复调用）。"""
    with closing(get_conn()) as conn, conn:
        # WAL 让读写不再互斥：爬虫批量写、语音 WS 写与用户请求读可并发，
        # 否则默认回滚日志的写锁全库独占，并发时 database is locked（bug #8）
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
            # 破坏外层事务，DROP+RENAME 崩溃中间态不可恢复，藏品会遗留在孤儿表中，
            # bug #31）；改用逐条 execute 让整个迁移共享同一事务，崩溃可原子回滚。
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
        # 登录令牌改存 SHA-256 哈希（bug #25）：重建 auth_tokens，明文行逐条哈希迁移。
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


def _normalize(text: str) -> str:
    """归一化：去空白/换行/大小写，用于生成稳定的内容哈希。"""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def make_hash(*parts: str) -> str:
    """由题干片段组合生成去重哈希。"""
    raw = "|".join(_normalize(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _escape_like(text: str) -> str:
    """转义 LIKE 通配符，防止用户输入 %/_ 干扰匹配。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def upsert_question(
    *,
    source: str,
    title: str,
    source_id: str | None = None,
    content: str | None = None,
    answer: str | None = None,
    tags: list[str] | None = None,
    difficulty: str | None = None,
    company: str | None = None,
    url: str | None = None,
) -> bool:
    """插入一条题目，返回是否为新入库（False 表示已存在被跳过）。"""
    h = make_hash(source, title, content or "")
    now = datetime.now(timezone.utc).isoformat()
    tag_str = ",".join(tags) if tags else None
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO questions
               (source, source_id, title, content, answer, tags, difficulty, company, url, content_hash, fetched_at, clean_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source,
                source_id,
                title,
                content,
                answer,
                tag_str,
                difficulty,
                company,
                url,
                h,
                now,
                CLEAN_STATUS_RAW,
            ),
        )
        return cur.rowcount > 0


def upsert_many(questions: list[dict]) -> dict:
    """批量入库（单事务），返回统计 {'new': n, 'skipped': n}。"""
    if not questions:
        return {"new": 0, "skipped": 0}
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for q in questions:
        h = make_hash(q.get("source", ""), q.get("title", ""), q.get("content") or "")
        rows.append(
            (
                q.get("source", ""),
                q.get("source_id"),
                q.get("title"),
                q.get("content"),
                q.get("answer"),
                ",".join(q["tags"]) if q.get("tags") else None,
                q.get("difficulty"),
                q.get("company"),
                q.get("url"),
                h,
                now,
                CLEAN_STATUS_RAW,
            )
        )
    with closing(get_conn()) as conn, conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO questions
               (source, source_id, title, content, answer, tags, difficulty, company, url, content_hash, fetched_at, clean_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        new = max(cur.rowcount, 0)
    return {"new": new, "skipped": len(rows) - new}


def count_questions() -> int:
    with closing(get_conn()) as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]


def count_by_source() -> list[dict]:
    with closing(get_conn()) as conn:
        return _rows_to_dicts(
            conn.execute(
                "SELECT source, COUNT(*) AS n FROM questions GROUP BY source ORDER BY n DESC"
            ).fetchall()
        )


# ------------------------------------------------------------ 懒加载补抓记录


def is_category_fetched(source: str, category: str) -> bool:
    """该数据源+分类是否已按需补抓过。"""
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT 1 FROM fetched_categories WHERE source = ? AND category = ?",
            (source, category),
        ).fetchone()
    return row is not None


def mark_category_fetched(source: str, category: str, new_count: int = 0) -> None:
    """记录该数据源+分类已完成懒加载补抓（幂等，重复抓取会刷新时间）。"""
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn, conn:
        conn.execute(
            """INSERT INTO fetched_categories (source, category, new_count, fetched_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source, category)
               DO UPDATE SET new_count = excluded.new_count, fetched_at = excluded.fetched_at""",
            (source, category, new_count, now),
        )


# ------------------------------------------------------------ 数据清洗状态机


def update_question_fields(question_id: int, fields: dict) -> int:
    """按字段字典更新题目任意列（清洗回写用），返回受影响行数。"""
    if not fields:
        return 0
    allowed = {
        "title",
        "answer",
        "tags",
        "difficulty",
        "content",
        "company",
        "url",
        "source",
        "source_id",
        "clean_status",
        "clean_version",
        "cleaned_at",
    }
    invalid = set(fields.keys()) - allowed
    if invalid:
        raise ValueError(f"非法字段: {invalid}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    with closing(get_conn()) as conn, conn:
        return conn.execute(
            f"UPDATE questions SET {sets} WHERE id=?", [*fields.values(), question_id]
        ).rowcount


def list_pending_rule_clean(clean_version: str, limit: int | None = None) -> list[dict]:
    """待规则清洗的行：状态为 raw，或 clean_version 过期（规则升级后精准重洗）。"""
    sql = (
        "SELECT * FROM questions WHERE clean_status = ? OR "
        "(clean_status != ? AND (clean_version IS NULL OR clean_version != ?))"
    )
    params: list = [CLEAN_STATUS_RAW, CLEAN_STATUS_RAW, clean_version]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with closing(get_conn()) as conn:
        return _rows_to_dicts(conn.execute(sql, params).fetchall())


def list_pending_semantic_clean(
    coarse_tags: set[str] | None = None, limit: int | None = None
) -> list[dict]:
    """待语义清洗（LLM 打标）的行：已过规则清洗、但标签仍粗糙/缺失。"""
    params: list = [CLEAN_STATUS_RULE]
    conds = ["clean_status = ?", "(tags IS NULL OR tags = '')"]
    for t in coarse_tags or set():
        conds.append("tags = ?")
        params.append(t)
    sql = "SELECT * FROM questions WHERE " + " OR ".join(conds)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with closing(get_conn()) as conn:
        return _rows_to_dicts(conn.execute(sql, params).fetchall())


def mark_clean(ids: list[int], status: str, clean_version: str) -> int:
    """批量更新清洗状态（+版本+时间），返回受影响行数。"""
    if not ids:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    placeholders = ",".join("?" * len(ids))
    with closing(get_conn()) as conn, conn:
        return conn.execute(
            f"UPDATE questions SET clean_status=?, clean_version=?, cleaned_at=? "
            f"WHERE id IN ({placeholders})",
            [status, clean_version, now, *ids],
        ).rowcount


def set_question_tags(ids: list[int], tags_by_id: dict[int, str], clean_version: str) -> int:
    """语义清洗后写回标签并标记 semantic_cleaned，返回受影响行数。"""
    if not ids:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    with closing(get_conn()) as conn, conn:
        for qid in ids:
            tag_str = tags_by_id.get(qid)
            if tag_str is None:
                continue
            cur = conn.execute(
                "UPDATE questions SET tags=?, clean_status=?, clean_version=?, cleaned_at=? "
                "WHERE id=?",
                (tag_str, CLEAN_STATUS_SEMANTIC, clean_version, now, qid),
            )
            n += cur.rowcount
    return n


def clean_stats() -> dict:
    """按清洗状态统计题目数量（让清洗覆盖率可见）。"""
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT clean_status, COUNT(*) AS n FROM questions GROUP BY clean_status"
        ).fetchall()
    stats = {s: 0 for s in CLEAN_STATUSES}
    for r in rows:
        if r["clean_status"] in stats:
            stats[r["clean_status"]] = r["n"]
    stats["total"] = sum(stats.values())
    stats["done"] = stats[CLEAN_STATUS_SEMANTIC] + stats[CLEAN_STATUS_READY]
    return stats


def reset_clean_status(status_from: str, status_to: str) -> int:
    """重置清洗状态（如规则升级/清洗规则变更后把某状态批量退回重洗）。"""
    with closing(get_conn()) as conn, conn:
        return conn.execute(
            "UPDATE questions SET clean_status=? WHERE clean_status=?", (status_to, status_from)
        ).rowcount


def list_tags() -> list[tuple[str, int]]:
    """返回全部标签及出现次数（按次数降序，供筛选下拉框使用）。"""
    counts: dict[str, int] = {}
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT tags FROM questions WHERE tags IS NOT NULL AND tags != ''"
        ).fetchall()
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)


def search_questions(
    tags: list[str] | None = None,
    difficulty: str | None = None,
    source: str | None = None,
    company: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """按标签/难度/来源/公司/标题关键词检索题目。"""
    sql = "SELECT * FROM questions WHERE 1=1"
    params: list = []
    if tags:
        conds = []
        for t in tags:
            conds.append("tags LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(t)}%")
        sql += " AND (" + " OR ".join(conds) + ")"
    if difficulty:
        sql += " AND difficulty = ?"
        params.append(difficulty)
    if source:
        sql += " AND source = ?"
        params.append(source)
    if company:
        sql += " AND company = ?"
        params.append(company)
    if keyword:
        sql += " AND title LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(keyword)}%")
    sql += " ORDER BY fetched_at DESC LIMIT ?"
    params.append(limit)
    with closing(get_conn()) as conn:
        return _rows_to_dicts(conn.execute(sql, params).fetchall())


def browse_questions(
    keyword: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    difficulty: str | None = None,
    company: str | None = None,
    favorite_only: bool = False,
    user_id: int | None = None,
    limit: int = 30,
) -> list[dict]:
    """题库浏览检索：关键词走 FTS5（trigram → unicode61），可叠加来源/难度/公司过滤；
    全部失败时回退 标题/题干/答案/标签 LIKE。favorite_only 按 user_id 过滤收藏。"""
    where: list[str] = []
    params: list = []
    if source:
        where.append("q.source = ?")
        params.append(source)
    if difficulty:
        where.append("q.difficulty = ?")
        params.append(difficulty)
    if company:
        where.append("q.company = ?")
        params.append(company)
    if tags:
        conds = []
        for t in tags:
            conds.append("q.tags LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(t)}%")
        where.append("(" + " OR ".join(conds) + ")")
    if favorite_only:
        where.append("q.id IN (SELECT question_id FROM favorites WHERE user_id IS ?)")
        params.append(user_id)
    cond = (" AND " + " AND ".join(where)) if where else ""
    kw = (keyword or "").strip()
    if not kw:
        sql = f"SELECT q.* FROM questions q WHERE 1=1{cond} ORDER BY q.fetched_at DESC LIMIT ?"
        params.append(limit)
        with closing(get_conn()) as conn:
            return _rows_to_dicts(conn.execute(sql, params).fetchall())

    # 1) trigram 命中（中文子串/组合词，≥3 字符）
    trig_q = _fts_trigram_query(kw)
    if trig_q:
        sql = (
            "SELECT q.* FROM questions q JOIN questions_fts_tr f ON q.id = f.rowid "
            f"WHERE questions_fts_tr MATCH ?{cond} ORDER BY bm25(questions_fts_tr) LIMIT ?"
        )
        try:
            with closing(get_conn()) as conn:
                rows = conn.execute(sql, [trig_q, *params, limit]).fetchall()
            if rows:
                return _rows_to_dicts(rows)
        except sqlite3.OperationalError:
            pass
    # 2) unicode61 命中
    uq = _fts_query(kw)
    sql = (
        "SELECT q.* FROM questions q JOIN questions_fts f ON q.id = f.rowid "
        f"WHERE questions_fts MATCH ?{cond} ORDER BY bm25(questions_fts) LIMIT ?"
    )
    try:
        with closing(get_conn()) as conn:
            rows = conn.execute(sql, [uq, *params, limit]).fetchall()
        if rows:
            return _rows_to_dicts(rows)
    except sqlite3.OperationalError:
        pass
    # 3) LIKE 兜底：标题/题干/答案/标签任一包含
    like = f"%{_escape_like(kw)}%"
    sql = (
        "SELECT q.* FROM questions q WHERE "
        "(q.title LIKE ? ESCAPE '\\' OR q.content LIKE ? ESCAPE '\\' "
        "OR q.answer LIKE ? ESCAPE '\\' OR q.tags LIKE ? ESCAPE '\\')"
        f"{cond} ORDER BY q.fetched_at DESC LIMIT ?"
    )
    with closing(get_conn()) as conn:
        return _rows_to_dicts(
            conn.execute(sql, [like, like, like, like, *params, limit]).fetchall()
        )


def pick_random_question(
    tags: list[str] | None = None,
    difficulty: str | None = None,
    source: str | None = None,
    exclude_ids: set[int] | None = None,
    limit: int = 1,
) -> list[dict]:
    """按条件在 SQL 层随机选题，避免全表捞回内存过滤。

    SELECT 需带 source_id/answer：点评环节 _ensure_reference_answer 依赖
    这两列做"缺答案同步补抓"兜底，漏列会让兜底永不可达（bug #17）。
    """
    sql = "SELECT id, title, tags, difficulty, source, source_id, answer FROM questions WHERE 1=1"
    params: list = []
    if tags:
        conds = []
        for t in tags:
            conds.append("tags LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(t)}%")
        sql += " AND (" + " OR ".join(conds) + ")"
    if difficulty:
        sql += " AND difficulty = ?"
        params.append(difficulty)
    if source:
        sql += " AND source = ?"
        params.append(source)
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        sql += f" AND id NOT IN ({placeholders})"
        params.extend(exclude_ids)
    sql += " ORDER BY RANDOM() LIMIT ?"
    params.append(limit)
    with closing(get_conn()) as conn:
        return _rows_to_dicts(conn.execute(sql, params).fetchall())


def get_question_by_id(qid: int):
    """按 id 取单条题目（题库浏览→出这道题 用）。"""
    with closing(get_conn()) as conn:
        return _row_to_dict(conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone())


def list_companies() -> list[str]:
    """题库中已有的公司标签（去重，按名称排序）。"""
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT company FROM questions "
            "WHERE company IS NOT NULL AND company != '' ORDER BY company"
        ).fetchall()
    return [r["company"] for r in rows]


def create_session(
    mode: str,
    job_title: str = "",
    jd: str = "",
    source: str = "",
    persona: str = "",
    started_at: str | None = None,
    user_id: int | None = None,
    state_json: str | None = None,
    status: str = "done",
) -> int:
    """创建一条面试记录，返回 session_id。多用户下可带 user_id/state_json/status。"""
    started_at = started_at or datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO sessions (mode, job_title, jd, source, persona, started_at, user_id, "
            "state_json, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                mode,
                job_title or None,
                jd or None,
                source or None,
                persona or None,
                started_at,
                user_id,
                state_json,
                status,
                started_at,
            ),
        )
        return cur.lastrowid


def add_session_answers(session_id: int, answers: list[dict]) -> None:
    """批量写入一轮面试的问答记录。"""
    if not answers:
        return
    rows = [(session_id, a.get("stage"), a.get("title"), a.get("answer")) for a in answers]
    with closing(get_conn()) as conn, conn:
        conn.executemany(
            "INSERT INTO session_answers (session_id, stage, question_title, answer) "
            "VALUES (?,?,?,?)",
            rows,
        )


def finish_session(
    session_id: int, score: int | None, report: str, weak_points: str | None
) -> None:
    """面试结束后回填评分、报告与薄弱点。"""
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE sessions SET score=?, report=?, weak_points=? WHERE id=?",
            (score, report, weak_points, session_id),
        )


def list_sessions(limit: int = 50) -> list[dict]:
    """已完成的面试记录（按开始时间倒序，供侧边栏复盘）。"""
    with closing(get_conn()) as conn:
        return _rows_to_dicts(
            conn.execute(
                "SELECT * FROM sessions WHERE report IS NOT NULL ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )


def get_session_answers(session_id: int) -> list[dict]:
    """按会话取逐题问答记录。"""
    with closing(get_conn()) as conn:
        return _rows_to_dicts(
            conn.execute(
                "SELECT * FROM session_answers WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
        )


# ---------------------------------------------------------------- 会话状态持久化（多用户）


def get_active_session(user_id: int):
    """取某用户当前活跃会话（status='active'）。"""
    with closing(get_conn()) as conn:
        return _row_to_dict(
            conn.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND status = 'active' "
                "ORDER BY started_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        )


def update_session_state(
    session_id: int,
    state_json: str,
    *,
    score: int | None = None,
    report: str | None = None,
    weak_points: str | None = None,
    status: str | None = None,
) -> None:
    """更新会话状态（state_json 及可选评分/报告/状态）。"""
    sets = ["state_json = ?", "updated_at = ?"]
    params: list = [state_json, datetime.now(timezone.utc).isoformat()]
    if score is not None:
        sets.append("score = ?")
        params.append(score)
    if report is not None:
        sets.append("report = ?")
        params.append(report)
    if weak_points is not None:
        sets.append("weak_points = ?")
        params.append(weak_points)
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    params.append(session_id)
    with closing(get_conn()) as conn, conn:
        conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)


def archive_active_session(user_id: int) -> None:
    """把某用户当前活跃会话标记为已完成（status='done'），用于开始新会话前归档。"""
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE sessions SET status='done', updated_at=? WHERE user_id=? AND status='active'",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )


def list_sessions_by_user(user_id: int, limit: int = 50) -> list[dict]:
    """某用户的面试历史（含进行中与已完成的，按开始时间倒序）。

    显式列清单：历史列表不需要 state_json / jd 等大字段，
    SELECT * 会在 limit 异常放大时把几十 KB/行的数据全部拉回（bug #11）。
    """
    with closing(get_conn()) as conn:
        return _rows_to_dicts(
            conn.execute(
                "SELECT id, mode, job_title, source, persona, started_at, score, "
                "report, weak_points, status FROM sessions "
                "WHERE user_id = ? ORDER BY started_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        )


def add_favorite(question_id: int, user_id: int | None = None) -> bool:
    """收藏题目，返回是否为新收藏（已收藏返回 False）。user_id 为空时为遗留全局收藏。

    用 WHERE NOT EXISTS 保证（user_id, question_id）唯一：SQLite 的 UNIQUE 对
    NULL user_id 不生效，需显式查重。
    """
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO favorites (user_id, question_id, created_at) "
            "SELECT ?, ?, ? WHERE NOT EXISTS ("
            "SELECT 1 FROM favorites WHERE question_id = ? AND user_id IS ?)",
            (user_id, question_id, now, question_id, user_id),
        )
        return cur.rowcount > 0


def remove_favorite(question_id: int, user_id: int | None = None) -> None:
    """取消收藏。"""
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "DELETE FROM favorites WHERE question_id = ? AND user_id IS ?",
            (question_id, user_id),
        )


def is_favorite(question_id: int, user_id: int | None = None) -> bool:
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT 1 FROM favorites WHERE question_id = ? AND user_id IS ?",
            (question_id, user_id),
        ).fetchone()
        return row is not None


def list_favorite_ids(user_id: int | None = None) -> set[int]:
    """取某用户收藏的题目 id 集合（用于批量高亮/收藏列表）。"""
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT question_id FROM favorites WHERE user_id IS ?", (user_id,)
        ).fetchall()
    return {r["question_id"] for r in rows}


# ---------------------------------------------------------------- 用户与登录令牌


def create_user(
    username: str, password_hash: str, nickname: str | None = None, persona: str = ""
) -> int | None:
    """创建账号，返回 user_id；用户名已存在返回 None。"""
    with closing(get_conn()) as conn, conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, nickname, persona, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    username,
                    password_hash,
                    nickname or None,
                    persona or None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def get_user_by_username(username: str):
    """按用户名取用户（用户名不区分大小写）。"""
    with closing(get_conn()) as conn:
        return _row_to_dict(
            conn.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
        )


def get_user_by_id(user_id: int):
    with closing(get_conn()) as conn:
        return _row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def update_user_persona(user_id: int, persona: str) -> None:
    """更新用户默认面试官人格。"""
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET persona = ? WHERE id = ?", (persona or None, user_id))


def update_user_nickname(user_id: int, nickname: str) -> None:
    """更新用户昵称。"""
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET nickname = ? WHERE id = ?", (nickname or None, user_id))


def touch_user_login(user_id: int) -> None:
    """记录最近登录时间。"""
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )


def _token_hash(token: str) -> str:
    """令牌/票据落库前统一哈希：SHA-256。

    令牌为 secrets.token_urlsafe(32) 高熵随机串，无需密码级 KDF 抗爆破；
    确定性哈希使存量明文行可无损迁移（客户端令牌在服务端哈希后照常匹配）。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_auth_token(user_id: int, token: str, expires_at: str) -> None:
    """保存登录令牌（落库前哈希，明文不持久化）。

    签发时顺带清理该库全部过期令牌（bug #9：过期行只增不减）。
    """
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO auth_tokens (token_hash, user_id, created_at, expires_at) "
            "VALUES (?,?,?,?)",
            (_token_hash(token), user_id, now, expires_at),
        )


def get_user_by_token(token: str):
    """按令牌取用户（校验有效期）；无效/过期返回 None。查询值同样先哈希。"""
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn:
        return _row_to_dict(
            conn.execute(
                "SELECT u.* FROM auth_tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.token_hash = ? AND t.expires_at > ?",
                (_token_hash(token), now),
            ).fetchone()
        )


def revoke_token(token: str) -> None:
    """注销单个令牌。"""
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (_token_hash(token),))


def revoke_all_tokens(user_id: int) -> None:
    """注销某用户全部令牌（如修改密码/退出所有端）。"""
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------- WS 一次性票据


def create_ws_ticket(user_id: int, ticket: str, expires_at: str) -> None:
    """保存 WS 一次性连接票据（落库前哈希）。签发时顺带清理全部过期票据。"""
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM ws_tickets WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO ws_tickets (ticket_hash, user_id, created_at, expires_at) "
            "VALUES (?,?,?,?)",
            (_token_hash(ticket), user_id, now, expires_at),
        )


def consume_ws_ticket(ticket: str) -> int | None:
    """消费一次性票据：删除即返回（原子），保证单次有效，返回 user_id。

    无效 / 过期 / 已消费返回 None。

    用 `DELETE ... RETURNING` 把"校验 + 删除"合并为一条原子写操作，
    避免"先 SELECT 再 DELETE"的 TOCTOU 竞态——并发消耗同一票据时只会有
    一个成功，其余语句命中 0 行（bug #36）。
    """
    now = datetime.now(timezone.utc).isoformat()
    h = _token_hash(ticket)
    with closing(get_conn()) as conn, conn:
        row = conn.execute(
            "DELETE FROM ws_tickets WHERE ticket_hash = ? AND expires_at > ? RETURNING user_id",
            (h, now),
        ).fetchone()
        return row["user_id"] if row else None


# ---------------------------------------------------------------- 定制面试（按用户）


def save_custom_interview(user_id: int, job_title: str, jd: str, questions: list[str]) -> None:
    """保存某用户最新一份定制面试（覆盖旧值）。"""
    payload = json.dumps([q for q in (questions or []) if q and q.strip()], ensure_ascii=False)
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO custom_interviews (user_id, job_title, jd, questions_json, created_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET job_title=excluded.job_title, "
            "jd=excluded.jd, questions_json=excluded.questions_json, created_at=excluded.created_at",
            (
                user_id,
                job_title or None,
                jd or None,
                payload,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def load_custom_interview(user_id: int) -> dict | None:
    """读取某用户最新定制面试；不存在或无题目时返回 None。"""
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM custom_interviews WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    try:
        questions = json.loads(row["questions_json"] or "[]")
    except json.JSONDecodeError:
        questions = []
    if not questions:
        return None
    return {
        "job_title": row["job_title"] or "",
        "jd": row["jd"] or "",
        "questions": questions,
        "created_at": row["created_at"],
    }


def clear_custom_interview(user_id: int) -> None:
    """清除某用户的定制面试。"""
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM custom_interviews WHERE user_id = ?", (user_id,))


def update_question_details(
    source: str,
    source_id: str,
    *,
    answer: str | None = None,
    difficulty: str | None = None,
) -> int:
    """按 source+source_id 更新题目答案/难度（详情页补全用），返回受影响行数。"""
    sets: list[str] = []
    params: list = []
    if answer is not None:
        sets.append("answer = ?")
        params.append(answer)
    if difficulty is not None:
        sets.append("difficulty = ?")
        params.append(difficulty)
    if not sets:
        return 0
    params += [source, source_id]
    sql = f"UPDATE questions SET {', '.join(sets)} WHERE source = ? AND source_id = ?"

    def _run() -> int:
        with closing(get_conn()) as conn, conn:
            return conn.execute(sql, params).rowcount

    try:
        return _run()
    except sqlite3.DatabaseError:
        # FTS 外部内容表与主表 rowid 错位时，UPDATE 触发器会报 malformed：重建索引后重试
        logger.warning("更新题目详情触发 FTS 异常，重建全文索引后重试")
        _rebuild_fts()
        return _run()


def _rebuild_fts() -> None:
    """重建 FTS5 索引（外部内容表 rowid 与主表错位时用于修复）。"""
    with closing(get_conn()) as conn, conn:
        for t in ("questions_fts", "questions_fts_tr"):
            with suppress(sqlite3.OperationalError):
                conn.execute(f"INSERT INTO {t}({t}) VALUES('rebuild')")


def list_favorites(limit: int = 50) -> list[dict]:
    """收藏的题目列表（含收藏时间，按收藏先后倒序）。"""
    with closing(get_conn()) as conn:
        return _rows_to_dicts(
            conn.execute(
                "SELECT q.*, f.created_at AS faved_at FROM favorites f "
                "JOIN questions q ON q.id = f.question_id ORDER BY f.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )


def latest_questions(source: str | None = None, limit: int = 20) -> list[dict]:
    """最新的题目（模拟面试候选题库）。"""
    sql = "SELECT * FROM questions"
    params: list = []
    if source:
        sql += " WHERE source = ?"
        params.append(source)
    sql += " ORDER BY fetched_at DESC LIMIT ?"
    params.append(limit)
    with closing(get_conn()) as conn:
        return _rows_to_dicts(conn.execute(sql, params).fetchall())


def _fts_query(keyword: str) -> str:
    """把用户关键词转成 FTS5 MATCH 短语查询（引号转义 + AND 连接）。"""
    tokens = [t for t in _FTS_TOKEN_RE.findall(keyword) if t.strip()]
    if not tokens:
        return f'"{_escape_fts(keyword)}"'
    return " AND ".join(f'"{_escape_fts(t)}"' for t in tokens)


def _escape_fts(text: str) -> str:
    return text.replace('"', '""')


def _fts_trigram_query(keyword: str) -> str | None:
    """trigram 查询串：要求所有词都 ≥3 字符（trigram 不支持短词），否则返回 None。"""
    tokens = [t for t in _FTS_TOKEN_RE.findall(keyword) if t.strip()]
    if not tokens or any(len(t) < 3 for t in tokens):
        return None
    return " AND ".join(f'"{_escape_fts(t)}"' for t in tokens)


def _fts_trigram_search(keyword: str, limit: int = 5) -> list[dict]:
    """trigram 索引检索：中文子串/组合词匹配（≥3 字符），按 bm25 排序。"""
    query = _fts_trigram_query(keyword)
    if not query:
        return []
    sql = """SELECT q.* FROM questions q
             JOIN questions_fts_tr f ON q.id = f.rowid
             WHERE questions_fts_tr MATCH ?
             ORDER BY bm25(questions_fts_tr) LIMIT ?"""
    try:
        with closing(get_conn()) as conn:
            return _rows_to_dicts(conn.execute(sql, (query, limit)).fetchall())
    except sqlite3.OperationalError:
        return []


def fts_search(keyword: str, limit: int = 5) -> list[dict]:
    """全文检索：trigram（中文子串，≥3 字）→ unicode61 → LIKE 三级回退。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    rows = _fts_trigram_search(keyword, limit)
    if rows:
        return rows
    query = _fts_query(keyword)
    sql = """SELECT q.* FROM questions q
             JOIN questions_fts f ON q.id = f.rowid
             WHERE questions_fts MATCH ?
             ORDER BY bm25(questions_fts) LIMIT ?"""
    try:
        with closing(get_conn()) as conn:
            rows = _rows_to_dicts(conn.execute(sql, (query, limit)).fetchall())
    except sqlite3.OperationalError:
        rows = []
    if rows:
        return rows
    return search_questions(keyword=keyword, limit=limit)
