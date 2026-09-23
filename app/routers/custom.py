"""定制面试 REST API：按岗位/JD 生成题目（SSE 进度流），生成后自动开始模拟面试，
并把定制题保存到该用户名下供语音接通使用；以及定制面试状态查询/清除。

生成流程与旧 Streamlit 的 st.status 一致：识别技术栈 → 检索题库 → 零命中懒加载补抓
→ AI 生成；进度通过 `data: {"type":"progress","message":...}` 事件流式返回。
"""

import asyncio
import json
import logging
import queue as _queue

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import app.stores.session_store as session_store
import app.stores.voice_store as voice_store
from app.agent.coach import InterviewSession
from app.agent.customizer import generate_interview_questions_with_meta
from app.stores import auth

logger = logging.getLogger("interview_coach.api.custom")

router = APIRouter(prefix="/api/custom", tags=["custom"])


class CustomBody(BaseModel):
    job_title: str = Field(default="", max_length=100)
    jd: str = Field(default="", max_length=8000)


def _build_note(questions: list[str], meta: dict) -> str:
    """生成来源说明（题库真题 / AI 生成 / 懒加载补抓）。"""
    sources = meta.get("sources") or []
    n_bank = sources.count("题库")
    n_ai = len(questions) - n_bank
    lines: list[str] = []
    if meta.get("lazy_fetched"):
        lines.append(f"🔍 {meta.get('lazy', {}).get('detail') or '已补抓相关真题'}")
    if meta.get("answer_backfill"):
        lines.append("🔁 已补抓真题的参考答案正在后台补全，答题期间即可用于点评。")
    if n_ai and n_ai == len(questions):
        lines.append("⚠️ 本地题库暂无该岗位真题，以下题目为 **AI 生成（非真题）**，仅供参考。")
    elif n_bank:
        lines.append(f"📚 本批题目已参考本地题库 **{n_bank} 道真题** 生成。")
    return "\n".join(lines)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/generate")
async def generate(body: CustomBody, user_row=auth.CurrentUser):
    """按岗位/JD 生成定制面试题，并直接进入该定制面试会话。"""
    job_title = body.job_title.strip()
    jd = body.jd.strip()
    if not job_title and not jd:
        raise HTTPException(status_code=400, detail="请填写目标岗位或招聘信息")

    prog_q: _queue.Queue = _queue.Queue()

    def _run() -> tuple[list[str], dict]:
        qs, meta = generate_interview_questions_with_meta(
            job_title, jd, progress=lambda msg: prog_q.put(msg), user_id=user_row["id"]
        )
        return qs, meta

    async def _stream():
        task = asyncio.create_task(asyncio.to_thread(_run))
        try:
            while not task.done():
                try:
                    yield _sse({"type": "progress", "message": prog_q.get_nowait()})
                except _queue.Empty:
                    await asyncio.sleep(0.08)
            # 线程结束：把可能残留的最后一条进度也发出去
            while True:
                try:
                    yield _sse({"type": "progress", "message": prog_q.get_nowait()})
                except _queue.Empty:
                    break
            qs, meta = task.result()
        except Exception:  # noqa: BLE001
            logger.exception("定制面试生成失败")
            # 内部异常细节（上游端点/配额/路径等）不回显给客户端
            yield _sse({"type": "error", "message": "生成失败，请稍后重试"})
            return

        if not qs:
            yield _sse({"type": "error", "message": "生成失败，请稍后重试"})
            return

        note = _build_note(qs, meta)
        greeting = (
            f"已按「{job_title or '自定义'}」为你生成 **{len(qs)} 道定制题**。\n\n"
            f"{note}\n\n"
            "先做个 1 分钟自我介绍吧（姓名 / 经验 / 相关项目）😊"
        )

        # 保存一份给语音通话（该用户接通后直接以此题目开始语音面试）
        voice_store.save_custom_interview(user_row["id"], job_title, jd, qs)

        # 归档旧会话并进入定制模拟面试
        session_store.archive_current(user_row["id"])
        try:
            persona = user_row["persona"] or ""
        except (KeyError, IndexError, TypeError):
            persona = ""
        session = InterviewSession(
            "mock",
            questions=qs,
            job_title=job_title,
            jd=jd,
            persona=persona,
            user_id=user_row["id"],
        )
        session.display_history = [["assistant", greeting]]
        session_store.start_session(user_row["id"], session)

        yield _sse(
            {
                "type": "done",
                "session_id": session.session_id,
                "mode": session.mode,
                "history": session.history_for_display(),
                "note": note,
                "custom_voice_ready": True,
                "job_title": job_title,
            }
        )

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/status")
def custom_status(user_row=auth.CurrentUser) -> dict:
    """该用户是否有待执行的语音定制面试（语音页徽标/提示用）。"""
    custom = voice_store.load_custom_interview(user_row["id"])
    return {
        "ready": custom is not None,
        "job_title": (custom or {}).get("job_title", ""),
    }


@router.delete("")
def clear_custom(user_row=auth.CurrentUser) -> dict:
    """清除该用户的语音定制面试。"""
    voice_store.clear_custom_interview(user_row["id"])
    return {"ok": True}
