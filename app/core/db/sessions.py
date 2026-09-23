"""面试会话：一场面试的记录、逐题问答、进行中状态快照。

`status` 区分「进行中」(`active`) 与「已归档」(`done`)：同一用户同时只应有一条
active 会话，`state_json` 存进行中的完整状态快照（供刷新后恢复）。
"""

from contextlib import closing
from datetime import datetime, timezone

from app.core.db.conn import get_conn, row_to_dict, rows_to_dicts


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
        return rows_to_dicts(
            conn.execute(
                "SELECT * FROM sessions WHERE report IS NOT NULL ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )


def get_session_answers(session_id: int) -> list[dict]:
    """按会话取逐题问答记录。"""
    with closing(get_conn()) as conn:
        return rows_to_dicts(
            conn.execute(
                "SELECT * FROM session_answers WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
        )


# ---------------------------------------------------------------- 进行中会话（多用户）


def get_active_session(user_id: int):
    """取某用户当前活跃会话（status='active'）。"""
    with closing(get_conn()) as conn:
        return row_to_dict(
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
    SELECT * 会在 limit 异常放大时把几十 KB/行的数据全部拉回。
    """
    with closing(get_conn()) as conn:
        return rows_to_dicts(
            conn.execute(
                "SELECT id, mode, job_title, source, persona, started_at, score, "
                "report, weak_points, status FROM sessions "
                "WHERE user_id = ? ORDER BY started_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        )
