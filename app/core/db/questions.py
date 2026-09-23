"""题库：题目入库与去重、多条件检索、全文检索、标签与公司维度。

检索请求的入口都在这里，包括 `fts_search` 的三级回退编排
（trigram → unicode61 → LIKE）—— FTS 的查询构造与索引原语在 `fts.py`。
"""

import hashlib
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from app.core.db import fts
from app.core.db.conn import get_conn, row_to_dict, rows_to_dicts
from app.core.db.schema import CLEAN_STATUS_RAW

logger = logging.getLogger("interview_coach.db")


def _normalize(text: str) -> str:
    """归一化：去空白/换行/大小写，用于生成稳定的内容哈希。"""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def make_hash(*parts: str) -> str:
    """由题干片段组合生成去重哈希。"""
    raw = "|".join(_normalize(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def escape_like(text: str) -> str:
    """转义 LIKE 通配符，防止用户输入 %/_ 干扰匹配。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------- 入库


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
        return rows_to_dicts(
            conn.execute(
                "SELECT source, COUNT(*) AS n FROM questions GROUP BY source ORDER BY n DESC"
            ).fetchall()
        )


# ---------------------------------------------------------------- 单条读写


def get_question_by_id(qid: int):
    """按 id 取单条题目（题库浏览→出这道题 用）。"""
    with closing(get_conn()) as conn:
        return row_to_dict(conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone())


def update_question_fields(question_id: int, fields: dict) -> int:
    """按字段字典更新题目任意列（清洗回写用），返回受影响行数。

    白名单限制可写列：这里是唯一一个"列名拼进 SQL"的入口，放开等于把注入面交给调用方。
    """
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
        fts.rebuild()
        return _run()


# ---------------------------------------------------------------- 条件检索


def search_questions(
    tags: list[str] | None = None,
    difficulty: str | None = None,
    source: str | None = None,
    company: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """按标签/难度/来源/公司/标题关键词检索题目（纯 LIKE，不走 FTS）。"""
    sql = "SELECT * FROM questions WHERE 1=1"
    params: list = []
    if tags:
        conds = []
        for t in tags:
            conds.append("tags LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(t)}%")
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
        params.append(f"%{escape_like(keyword)}%")
    sql += " ORDER BY fetched_at DESC LIMIT ?"
    params.append(limit)
    with closing(get_conn()) as conn:
        return rows_to_dicts(conn.execute(sql, params).fetchall())


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
            params.append(f"%{escape_like(t)}%")
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
            return rows_to_dicts(conn.execute(sql, params).fetchall())

    # 1) trigram 命中（中文子串/组合词，≥3 字符）
    trig_q = fts.build_trigram_query(kw)
    if trig_q:
        sql = (
            "SELECT q.* FROM questions q JOIN questions_fts_tr f ON q.id = f.rowid "
            f"WHERE questions_fts_tr MATCH ?{cond} ORDER BY bm25(questions_fts_tr) LIMIT ?"
        )
        try:
            with closing(get_conn()) as conn:
                rows = conn.execute(sql, [trig_q, *params, limit]).fetchall()
            if rows:
                return rows_to_dicts(rows)
        except sqlite3.OperationalError:
            pass
    # 2) unicode61 命中
    uq = fts.build_query(kw)
    sql = (
        "SELECT q.* FROM questions q JOIN questions_fts f ON q.id = f.rowid "
        f"WHERE questions_fts MATCH ?{cond} ORDER BY bm25(questions_fts) LIMIT ?"
    )
    try:
        with closing(get_conn()) as conn:
            rows = conn.execute(sql, [uq, *params, limit]).fetchall()
        if rows:
            return rows_to_dicts(rows)
    except sqlite3.OperationalError:
        pass
    # 3) LIKE 兜底：标题/题干/答案/标签任一包含
    like = f"%{escape_like(kw)}%"
    sql = (
        "SELECT q.* FROM questions q WHERE "
        "(q.title LIKE ? ESCAPE '\\' OR q.content LIKE ? ESCAPE '\\' "
        "OR q.answer LIKE ? ESCAPE '\\' OR q.tags LIKE ? ESCAPE '\\')"
        f"{cond} ORDER BY q.fetched_at DESC LIMIT ?"
    )
    with closing(get_conn()) as conn:
        return rows_to_dicts(conn.execute(sql, [like, like, like, like, *params, limit]).fetchall())


def fts_search(keyword: str, limit: int = 5) -> list[dict]:
    """全文检索：trigram（中文子串，≥3 字）→ unicode61 → LIKE 三级回退。

    最后一级落到 `search_questions`（标题 LIKE），因此本函数留在题库模块，
    而不是放在只做 FTS 原语的 `fts.py`。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    rows = fts.trigram_rows(keyword, limit)
    if rows:
        return rows
    rows = fts.unicode_rows(keyword, limit)
    if rows:
        return rows
    return search_questions(keyword=keyword, limit=limit)


def pick_random_question(
    tags: list[str] | None = None,
    difficulty: str | None = None,
    source: str | None = None,
    exclude_ids: set[int] | None = None,
    limit: int = 1,
) -> list[dict]:
    """按条件在 SQL 层随机选题，避免全表捞回内存过滤。

    SELECT 需带 source_id/answer：点评环节 _ensure_reference_answer 依赖
    这两列做"缺答案同步补抓"兜底，漏列会让兜底永不可达。
    """
    sql = "SELECT id, title, tags, difficulty, source, source_id, answer FROM questions WHERE 1=1"
    params: list = []
    if tags:
        conds = []
        for t in tags:
            conds.append("tags LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(t)}%")
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
        return rows_to_dicts(conn.execute(sql, params).fetchall())


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
        return rows_to_dicts(conn.execute(sql, params).fetchall())


# ---------------------------------------------------------------- 标签与公司


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


def list_companies() -> list[str]:
    """题库中已有的公司标签（去重，按名称排序）。"""
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT company FROM questions "
            "WHERE company IS NOT NULL AND company != '' ORDER BY company"
        ).fetchall()
    return [r["company"] for r in rows]
