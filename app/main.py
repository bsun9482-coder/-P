"""面试官小P 统一 Web 服务（FastAPI + WebSocket + edge-tts 神经语音）。

多用户改造后为唯一后端：同时提供
- REST API（认证 / 会话与聊天 / 题库 / 定制面试，见 app/routers/）；
- Vue3 前端静态托管（frontend/dist，SPA history 回退）；
- 语音通话 WebSocket（app/voice_ws.py，按用户认证与持久化）；
- TTS 合成与推送（app/tts.py，引擎策略 + 熔断）。

启动：
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
或安装后直接运行：
    xiaop-voice
"""

import logging
from contextlib import asynccontextmanager, closing
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import app.core.db as db
from app.agent import llm
from app.core import config
from app.core.scheduler import setup_logging
from app.routers import auth as auth_api
from app.routers import custom as custom_api
from app.routers import questions as questions_api
from app.routers import session as session_api
from app.routers.schemas import HealthOut, VoiceConfigOut
from app.voice_ws import router as voice_ws_router

logger = logging.getLogger("interview_coach.main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    db.init_db()
    if not llm.is_api_key_configured():
        logger.warning("未检测到有效的 DEEPSEEK_API_KEY，语音对话将无法使用（请在 .env 中配置）")
    if config.VOICE_TTS == "cosyvoice" and not config.DASHSCOPE_API_KEY:
        logger.warning("VOICE_TTS=cosyvoice 但未配置 DASHSCOPE_API_KEY，语音将回退浏览器本地语音")
    yield


app = FastAPI(title="面试官小P", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request, call_next):
    """统一安全响应头：防点击劫持与 MIME 嗅探。WS 不经此中间件。"""
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp


app.include_router(auth_api.router)
app.include_router(session_api.router)
app.include_router(questions_api.router)
app.include_router(custom_api.router)
app.include_router(voice_ws_router)

# 静态资源：虚拟人物头像（聊天页 / 语音页共用）
app.mount(
    "/assets",
    StaticFiles(directory=Path(__file__).resolve().parent / "ui" / "assets"),
    name="assets",
)


@app.get("/health")
async def health() -> HealthOut:
    """就绪探针：验证数据库可连接、schema 已初始化且版本匹配，否则返回 503。"""
    try:
        with closing(db.get_conn()) as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            version = conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable") from None
    if not {"users", "questions", "sessions"} <= tables:
        raise HTTPException(status_code=503, detail="database schema missing")
    if version != db.SCHEMA_VERSION:
        raise HTTPException(status_code=503, detail="database schema outdated")
    return {"status": "ok"}


@app.get("/api/config/voice")
async def voice_config() -> VoiceConfigOut:
    """前端语音页所需运行时配置（VAD 阈值等，替代旧 HTML 模板替换注入）。"""
    return {
        "vad_threshold": config.VOICE_VAD_THRESHOLD,
        "vad_hits": config.VOICE_VAD_HITS,
        "vad_quiet_frames": config.VOICE_VAD_QUIET_FRAMES,
        "vad_noise_margin": config.VOICE_VAD_NOISE_MARGIN,
        "tts": config.VOICE_TTS,
    }


#: Vue3 前端构建产物目录（frontend/dist）；未构建时前端路由返回提示
_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    """托管 Vue3 SPA：命中文件返回文件，其余交给 index.html（history 路由回退）。

    必须注册在所有具体路由之后（本文件最末），确保 /api、/ws/voice、/assets 优先匹配。
    """
    if full_path.startswith(("api/", "ws/")):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if full_path:
        candidate = (_FRONTEND_DIST / full_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(_FRONTEND_DIST.resolve()):
            return FileResponse(candidate)
    index = _FRONTEND_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse(
        {"detail": "前端尚未构建，请先在 frontend/ 下运行 npm run build"},
        status_code=404,
    )


def main() -> None:
    """命令行入口：启动统一 Web 服务（Vue3 前端 + REST + 语音，单端口）。"""
    uvicorn.run(app, host=config.APP_HOST, port=config.APP_PORT)


if __name__ == "__main__":
    main()
