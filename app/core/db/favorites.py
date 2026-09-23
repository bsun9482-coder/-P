"""收藏：按用户收藏题目。

`user_id` 为 NULL 表示多用户改造之前遗留的"全局收藏"，查询一律用
`user_id IS ?` 才能同时命中 NULL 行（`= NULL` 在 SQL 里恒为假）。
"""

from contextlib import closing
from datetime import datetime, timezone

from app.core.db.conn import get_conn, rows_to_dicts


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


def list_favorites(limit: int = 50) -> list[dict]:
    """收藏的题目列表（含收藏时间，按收藏先后倒序）。"""
    with closing(get_conn()) as conn:
        return rows_to_dicts(
            conn.execute(
                "SELECT q.*, f.created_at AS faved_at FROM favorites f "
                "JOIN questions q ON q.id = f.question_id ORDER BY f.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )
