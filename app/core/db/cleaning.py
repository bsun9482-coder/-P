"""题目清洗状态机与懒加载补抓记录。

这两件事都属于**爬虫侧的记账**：题目入库后怎么洗、某个数据源的某个分类是否已按需抓过。
放在数据库层旁边而不是 `app/crawler/` 里，是因为它们直接决定 `questions.clean_status`
与 `fetched_categories` 两张表的读写口径，调用方（`app/crawler/clean.py`、`app/crawler/lazy.py`）
只做编排、不直接拼 SQL。
"""

from contextlib import closing
from datetime import datetime, timezone

from app.core.db.conn import get_conn, rows_to_dicts
from app.core.db.schema import (
    CLEAN_STATUS_RAW,
    CLEAN_STATUS_READY,
    CLEAN_STATUS_RULE,
    CLEAN_STATUS_SEMANTIC,
    CLEAN_STATUSES,
)

# ---------------------------------------------------------------- 清洗状态机


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
        return rows_to_dicts(conn.execute(sql, params).fetchall())


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
        return rows_to_dicts(conn.execute(sql, params).fetchall())


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


# ---------------------------------------------------------------- 懒加载补抓记录


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
