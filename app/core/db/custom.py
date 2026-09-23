"""定制面试（按用户）：岗位/JD 对应的专属题目清单。

一人一份（`user_id` 即主键），保存即覆盖旧值；题目以 JSON 数组存在一列里，
因为它只是整体读写、从不单独检索。语音页接通时读取这份数据。
"""

import json
from contextlib import closing
from datetime import datetime, timezone

from app.core.db.conn import get_conn


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
