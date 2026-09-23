"""数据库结构定义：建表 DDL、schema 版本号，以及结构上的取值枚举。

本模块是库结构的**唯一权威描述** —— 下面每一张表的字段，与迁移代码
（`migrate.py`）跑完之后库里的真实结构**逐列一致**。所以想知道
「sessions 表到底有哪些字段」，读这里就够了，不需要再去把迁移代码从头读到尾。

两条建库路径的关系（读 DDL 时请记住这一点）：

- **全新库**：`init_db()` 先按 `SCHEMA` 一次性建全（建表语句里已经包含历次迁移
  新增的列），再跑 `_migrate()` 把 `user_version` 推到 `SCHEMA_VERSION`。
- **存量库**：表已经存在，`CREATE TABLE IF NOT EXISTS` 不生效，缺的列由
  `_migrate()` 按版本号逐级补齐。**迁移代码必须保留、不能删** —— 删掉老库就升不上来。

字段注释里的 `(vN)` 表示该列是第 N 次迁移新增的，便于对照历史。
`tests/test_schema_sync.py` 守卫「SCHEMA 与迁移结果一致」这条不变量。
"""

#: 当前 schema 版本，与 _migrate 的最终 PRAGMA user_version 保持一致；变更 schema 时同步更新
SCHEMA_VERSION = 8

SCHEMA = """
-- ============================================================ 题库
CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,              -- 来源标识：mianshiya / javaguide / leetcode / custom
    source_id     TEXT,                       -- 源站题号/页面ID（便于反查）
    title         TEXT NOT NULL,              -- 题干（短问题）
    content       TEXT,                       -- 详细题干/描述
    answer        TEXT,                       -- 参考答案（源站如有，可为空）
    tags          TEXT,                       -- 逗号分隔的标签，如 "Python,GIL"
    difficulty    TEXT,                       -- 难度：简单/中等/困难（或算法题 easy/medium/hard）
    company       TEXT,                       -- 公司维度标签（如 字节跳动 / 阿里 / 腾讯）(v3)
    url           TEXT,                       -- 来源链接
    content_hash  TEXT NOT NULL UNIQUE,       -- 归一化去重哈希
    fetched_at    TEXT NOT NULL,              -- 抓取时间（ISO 8601）
    clean_status  TEXT NOT NULL DEFAULT 'raw',-- 清洗状态机：(v6) raw/rule_cleaned/semantic_cleaned/ready
    clean_version TEXT,                       -- 清洗规则版本 (v6)：规则升级后可精准重洗
    cleaned_at    TEXT                        -- 最近一次清洗时间 (v6)
);

CREATE INDEX IF NOT EXISTS idx_questions_source ON questions(source);
CREATE INDEX IF NOT EXISTS idx_questions_tags ON questions(tags);
CREATE INDEX IF NOT EXISTS idx_questions_difficulty ON questions(difficulty);
-- idx_questions_clean_status 由 migrate.py 的 v6 段落创建，**不能**写在这里：
-- 旧库的 questions 还没 clean_status 这一列，而 init_db() 是先执行 SCHEMA、再跑迁移，
-- executescript 遇错即停 —— DDL 里建这个索引会让 v6 之前的老库直接起不来。

-- 懒加载补抓记录：记录某个数据源的某分类是否已按需抓取过（避免重复抓取）
CREATE TABLE IF NOT EXISTS fetched_categories (
    source      TEXT NOT NULL,
    category    TEXT NOT NULL,
    new_count   INTEGER NOT NULL DEFAULT 0,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (source, category)
);

-- ============================================================ 面试会话
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL,              -- mock / coach
    job_title   TEXT,                       -- 定制面试目标岗位
    jd          TEXT,                       -- 定制面试招聘信息
    source      TEXT,                       -- 题库 / 定制
    persona     TEXT,                       -- 面试官人格（一面/二面/三面）(v3)
    started_at  TEXT NOT NULL,              -- 开始时间（ISO 8601）
    score       INTEGER,                    -- 报告总分（0-100）
    report      TEXT,                       -- 总结报告全文
    weak_points TEXT,                       -- 薄弱点清单（每行一条）
    user_id     INTEGER,                    -- 所属用户 (v7)：NULL=多用户改造前的历史会话
    state_json  TEXT,                       -- 进行中会话的状态快照 (v7)
    status      TEXT NOT NULL DEFAULT 'done', -- active=进行中 / done=已归档 (v7)
    updated_at  TEXT                        -- 状态最近更新时间 (v7)
);

-- 逐题问答记录（一场面试的多条回答）
CREATE TABLE IF NOT EXISTS session_answers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL REFERENCES sessions(id),
    stage          TEXT,                    -- 阶段名（如 Python基础 / 定制题 1）
    question_title TEXT,
    answer         TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
-- idx_sessions_user_status 建在 v7 才新增的列上，同理交给 v7 段落创建（原因见 questions 表上方）
CREATE INDEX IF NOT EXISTS idx_session_answers_sid ON session_answers(session_id);

-- ============================================================ 收藏
CREATE TABLE IF NOT EXISTS favorites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,                             -- 所属用户（NULL=历史遗留的全局收藏）
    question_id INTEGER NOT NULL REFERENCES questions(id),
    created_at  TEXT NOT NULL,
    UNIQUE (user_id, question_id)
);
-- 注：UNIQUE 对 NULL user_id 不生效，add_favorite 额外用 WHERE NOT EXISTS 显式查重
-- idx_favorites_user 同样建在 v7 才新增的 user_id 上，交给 v7 段落创建

-- ============================================================ 账号与认证
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- 登录名（不区分大小写）
    password_hash TEXT NOT NULL,                         -- pbkdf2 密码散列
    nickname      TEXT,                                  -- 昵称（显示名）
    persona       TEXT,                                  -- 默认面试官人格
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- 登录令牌：只存 SHA-256 哈希，明文不落库 (v8)
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash TEXT PRIMARY KEY,                          -- 令牌的 SHA-256 哈希
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id);

-- 语音 WS 一次性连接票据（URL 只出现短时票据，长效令牌不出 Bearer 头）(v8)
CREATE TABLE IF NOT EXISTS ws_tickets (
    ticket_hash TEXT PRIMARY KEY,                         -- 票据的 SHA-256 哈希（单次消费）
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ws_tickets_user ON ws_tickets(user_id);

-- ============================================================ 定制面试
-- 文字版生成、语音接通时读取；按用户隔离（user_id 即主键，一人一份，取代旧单文件 voice_store）
CREATE TABLE IF NOT EXISTS custom_interviews (
    user_id        INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    job_title      TEXT,
    jd             TEXT,
    questions_json TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
"""

#: FTS5 外部内容表：与 questions 通过 rowid 关联，触发器保持同步。
#: FTS5 是可选的（部分 SQLite 构建不启用），因此单独一条脚本、由迁移 try/except 执行，
#: 建不上时全文检索回退 LIKE，不影响主流程。
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
#: 要求查询词 ≥3 字符（短词由 fts.search 回退到 unicode61 / LIKE）。
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


# ---------------------------------------------------------------- 列取值枚举

#: 题目清洗状态机（questions.clean_status 的取值）：
#: raw(未洗) → rule_cleaned(规则清洗) → semantic_cleaned(LLM 语义清洗) / ready(标签达标)
#: 流转逻辑与查询条件在 cleaning.py。
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
