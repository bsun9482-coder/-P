"""多用户会话持久化：把 InterviewSession 状态序列化到 SQLite（按 user_id 隔离）。

- 每个用户最多一份"活跃会话"（status='active'，存 state_json）；
- 开始新会话前先归档旧会话（status='done'），历史记录保留评分/报告/薄弱点；
- 每次对话回合结束即落库，刷新/断线/换设备可恢复继续。
"""

import json
import logging
from datetime import datetime, timezone

import app.core.db as db
from app.agent.coach import FINISHED_HINT, InterviewSession

logger = logging.getLogger("interview_coach.session_store")

#: 状态机在 users/sessions 迁移完成后才可用；init_db 幂等，可安全重复调用
_ACTIVE = "active"
_DONE = "done"


def _persist_session(session: InterviewSession) -> str:
    return json.dumps(session.to_dict(), ensure_ascii=False)


def load_active_session(user_id: int) -> InterviewSession | None:
    """取某用户当前活跃会话并恢复状态；没有则返回 None。"""
    row = db.get_active_session(user_id)
    if row is None or not row["state_json"]:
        return None
    try:
        session = InterviewSession.from_dict(json.loads(row["state_json"]))
        session.session_id = row["id"]
        session.user_id = user_id
        return session
    except Exception:
        logger.exception("恢复会话状态失败（session_id=%s），按新会话处理", row["id"])
        return None


def start_session(user_id: int, session: InterviewSession) -> int:
    """归档旧活跃会话并落库新会话，返回 session_id。"""
    db.archive_active_session(user_id)
    state = _persist_session(session)
    sid = db.create_session(
        mode=session.mode,
        job_title=session.job_title,
        jd=session.jd,
        source="定制" if session.custom_questions else ("题库" if session.mode == "mock" else ""),
        persona=session.persona,
        started_at=session.started_at,
        user_id=user_id,
        state_json=state,
        status=_ACTIVE,
    )
    session.session_id = sid
    return sid


def save_session(user_id: int, session: InterviewSession) -> None:
    """保存会话最新状态到 DB（含完成时的评分/报告/薄弱点）。"""
    sid = getattr(session, "session_id", None)
    if sid is None:
        return
    state = _persist_session(session)
    if session.finished and session.mode == "mock":
        from app.agent.coach import _extract_weak_points, _report_score

        # 报告取"最后一条非 FINISHED_HINT 的 assistant 消息"：报告出来后再发言，
        # messages[-1] 是 hint，直接取会覆盖掉真报告
        report_msg = next(
            (
                m
                for m in reversed(session.messages)
                if m.get("role") == "assistant" and m.get("content") != FINISHED_HINT
            ),
            None,
        )
        score = _report_score(report_msg["content"]) if report_msg else None
        weak = _extract_weak_points(report_msg["content"]) if report_msg else None
        db.update_session_state(
            sid,
            state,
            score=score,
            report=report_msg["content"] if report_msg else None,
            weak_points=weak,
            status="done",  # 完成的会话归档，否则 active 行永不完结
        )
    else:
        db.update_session_state(sid, state)


def archive_current(user_id: int) -> None:
    """归档某用户当前活跃会话（开始新会话前调用）。"""
    db.archive_active_session(user_id)


def list_history(user_id: int, limit: int = 50) -> list[dict]:
    """某用户的面试历史（含活跃会话，前端用于复盘/继续）。"""
    rows = db.list_sessions_by_user(user_id, limit=limit)
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "mode": r["mode"],
                "job_title": r["job_title"] or "",
                "source": r["source"] or "",
                "persona": r["persona"] or "",
                "started_at": r["started_at"],
                "score": r["score"],
                "report": r["report"],
                "weak_points": r["weak_points"],
                "status": r["status"],
                "active": r["status"] == _ACTIVE,
            }
        )
    return out


def touch_now() -> str:
    return datetime.now(timezone.utc).isoformat()
