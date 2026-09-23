"""SQLite 数据层的对外门面。

拆分前这里是一个 1327 行的单体文件，塞了题库、会话、收藏、账号、定制面试
以及爬虫的清洗记账六个互不相干的领域。现在按领域拆成同包内的模块：

    conn.py       连接与 Row→dict（各模块共用的最底层）
    schema.py     建表 DDL + schema 版本 + 列取值枚举
    migrate.py    建库与版本迁移（追加式，历史段不可改写）
    fts.py        FTS5 查询构造、检索原语、索引重建
    questions.py  题库：入库去重、条件检索、全文检索三级回退、标签与公司
    cleaning.py   清洗状态机 + 懒加载补抓记录
    sessions.py   面试会话与逐题问答
    favorites.py  收藏（按用户）
    users.py      账号、登录令牌、WS 一次性票据
    custom.py     定制面试（按用户）

**本模块只做转出，不含任何实现。** 对外的名字与拆分前完全一致 ——
调用方（`app/main.py`、`app/routers/*`、`app/stores/*`、`app/crawler/*`、
`app/agent/*`、`tests/*`）一律 `import app.core.db as db` 后 `db.xxx` 访问，
所以拆分没有触达任何一个调用点；`tests/test_crawler.py` 里的
`mock.patch("app.core.db.update_question_details")` 也照旧生效。

要加新能力：改对应的领域模块，并在这里补一行转出。
"""

import logging

from app.core.db.cleaning import (
    clean_stats,
    is_category_fetched,
    list_pending_rule_clean,
    list_pending_semantic_clean,
    mark_category_fetched,
    mark_clean,
    reset_clean_status,
    set_question_tags,
)
from app.core.db.conn import get_conn
from app.core.db.custom import (
    clear_custom_interview,
    load_custom_interview,
    save_custom_interview,
)
from app.core.db.favorites import (
    add_favorite,
    is_favorite,
    list_favorite_ids,
    list_favorites,
    remove_favorite,
)

# 兼容垫片：拆分前这个名字就在 db 模块上，tests/test_question_bank.py 直接引用它，
# 保留同名以免改动既有测试；它不是对外 API，故不进 __all__。
from app.core.db.fts import build_trigram_query as _fts_trigram_query  # noqa: F401
from app.core.db.migrate import init_db
from app.core.db.questions import (
    browse_questions,
    count_by_source,
    count_questions,
    fts_search,
    get_question_by_id,
    latest_questions,
    list_companies,
    list_tags,
    make_hash,
    pick_random_question,
    search_questions,
    update_question_details,
    update_question_fields,
    upsert_many,
    upsert_question,
)
from app.core.db.schema import (
    CLEAN_STATUS_RAW,
    CLEAN_STATUS_READY,
    CLEAN_STATUS_RULE,
    CLEAN_STATUS_SEMANTIC,
    CLEAN_STATUSES,
    FTS_SCHEMA,
    FTS_TRIGRAM_SCHEMA,
    SCHEMA,
    SCHEMA_VERSION,
)
from app.core.db.sessions import (
    add_session_answers,
    archive_active_session,
    create_session,
    finish_session,
    get_active_session,
    get_session_answers,
    list_sessions,
    list_sessions_by_user,
    update_session_state,
)
from app.core.db.users import (
    _token_hash,  # noqa: F401  兼容垫片：tests/test_auth_token_hash.py 直接引用它
    consume_ws_ticket,
    create_auth_token,
    create_user,
    create_ws_ticket,
    get_user_by_id,
    get_user_by_token,
    get_user_by_username,
    revoke_all_tokens,
    revoke_token,
    touch_user_login,
    update_user_nickname,
    update_user_persona,
)

#: 与拆分前同名的模块级 logger（各子模块也用这个名字，日志归属不变）
logger = logging.getLogger("interview_coach.db")

__all__ = [
    # 结构
    "SCHEMA",
    "SCHEMA_VERSION",
    "FTS_SCHEMA",
    "FTS_TRIGRAM_SCHEMA",
    "CLEAN_STATUS_RAW",
    "CLEAN_STATUS_RULE",
    "CLEAN_STATUS_SEMANTIC",
    "CLEAN_STATUS_READY",
    "CLEAN_STATUSES",
    # 连接与建库
    "get_conn",
    "init_db",
    # 题库
    "make_hash",
    "upsert_question",
    "upsert_many",
    "count_questions",
    "count_by_source",
    "get_question_by_id",
    "update_question_fields",
    "update_question_details",
    "search_questions",
    "browse_questions",
    "fts_search",
    "pick_random_question",
    "latest_questions",
    "list_tags",
    "list_companies",
    # 清洗与补抓记账
    "list_pending_rule_clean",
    "list_pending_semantic_clean",
    "mark_clean",
    "set_question_tags",
    "clean_stats",
    "reset_clean_status",
    "is_category_fetched",
    "mark_category_fetched",
    # 会话
    "create_session",
    "add_session_answers",
    "finish_session",
    "list_sessions",
    "get_session_answers",
    "get_active_session",
    "update_session_state",
    "archive_active_session",
    "list_sessions_by_user",
    # 收藏
    "add_favorite",
    "remove_favorite",
    "is_favorite",
    "list_favorite_ids",
    "list_favorites",
    # 账号与认证
    "create_user",
    "get_user_by_username",
    "get_user_by_id",
    "update_user_persona",
    "update_user_nickname",
    "touch_user_login",
    "create_auth_token",
    "get_user_by_token",
    "revoke_token",
    "revoke_all_tokens",
    "create_ws_ticket",
    "consume_ws_ticket",
    # 定制面试
    "save_custom_interview",
    "load_custom_interview",
    "clear_custom_interview",
]
