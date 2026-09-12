"""语音通话 WebSocket 端点 /ws/voice（从统一服务 main.py 拆出）。

职责：连接生命周期（认证、单用户互踢、会话恢复、ASR 管理、消息分发、
文本限流）与回复生成（LLM 流式 + TTS 推送，支持 barge-in 取消）。

协议（URL 需 ?ticket=<一次性票据>，经 REST POST /api/auth/ws-ticket 用
Bearer 令牌换取；长效令牌不出请求头，bug #23）：
  客户端 -> 服务端：
    文本帧 {"type":"text","content":...} / {"type":"stop"} /
            {"type":"asr_start","sample_rate":N}
    二进制帧：Int16 LE PCM 麦克风音频（asr_ready 后持续发送）
  服务端 -> 客户端（JSON 文本帧）：
    {"type":"reply_start","first_sid":N}                回复开始（音频 sid 起点）
    {"type":"delta","content":...}                      文本增量（实时字幕）
    {"type":"audio_start"/"audio"/"audio_end","sid":N}  音频单元三连帧
    {"type":"tts_error","sid":N}                        该段合成失败，回退本地语音
    {"type":"asr_ready"/"asr_error"/"asr_text"/"asr_partial"}  ASR 事件
    {"type":"done"} / {"type":"cancelled"}
    {"type":"error","code":"rate_limit"|"internal","message":...}  结构化错误
"""

import asyncio
import json
import logging
import re
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.agent.coach import InterviewSession
from app.core import config
from app.core.ratelimit import hit
from app.services import prompts, tts
from app.services.asr_client import DashScopeASR, asr_permanently_unavailable
from app.stores import auth, session_store, voice_store

logger = logging.getLogger("interview_coach.voice_ws")

_END = object()  # 生成器结束哨兵（StopIteration 不能跨 asyncio.to_thread 传播）

router = APIRouter()

#: ASR 断线自动重连：初始延迟与最大退避（秒）。
#: 阿里云实时识别是长连接 WebSocket，网络抖动会断开（心跳 PONG 超时），需要自动恢复
ASR_RETRY_DELAY = 3.0
ASR_RETRY_MAX = 20.0

#: 单用户重复接通时踢掉旧连接的 close code（前端据此提示"已在其他页面接通"）。
WS_CLOSE_KICKED = 4409

#: 在线连接注册表：user_id -> WebSocket（单用户互踢）。
_CONNECTIONS: dict[int, WebSocket] = {}

#: 首条消息触发"模拟面试"的明确意图（不含"模拟面试是什么"这类提问）
_MOCK_START_RE = re.compile(
    r"开始面试"
    r"|开始模拟面试"
    r"|^(?:我(?:想|要))?模拟面试(?:吧|一下|下|了)?$"
    r"|(?:我想|我要|来|做|进行|试试|开启|帮我|开始一[场次]).{0,4}模拟面试"
)

REOPEN_GREETING = (
    "欢迎回来，我们继续刚才的对话。你可以直接提问，也可以说“开始面试”开始一场新的模拟面试。"
)


def maybe_switch_to_mock(session: InterviewSession, text: str) -> InterviewSession:
    """用户说"开始面试/模拟面试"时，从答疑模式切换到模拟面试模式。

    不限首条消息（bug #15）：恢复的活跃答疑会话必然已有多条消息，而
    REOPEN_GREETING 明确承诺"可以说'开始面试'开始新模拟面试"；
    误切防护由 _MOCK_START_RE 保证（提问式如"模拟面试是什么"不命中）。
    """
    match = _MOCK_START_RE.search(text)
    if session.mode == "coach" and match is not None:
        return InterviewSession("mock", persona=session.persona, user_id=session.user_id)
    return session


def _build_custom_greeting(custom: dict) -> str:
    """定制面试接通时的开场白：告知岗位、题数与流程。"""
    title = (custom.get("job_title") or "").strip() or "自定义岗位"
    count = len(custom.get("questions") or [])
    return (
        f"你好，我是面试官小P。已为你准备好「{title}」的定制面试，共 {count} 道题。"
        "先做个 1 分钟自我介绍吧，我会由浅入深逐题提问，全程点评，"
        "全部结束后为你输出评分报告。"
    )


def _clear_custom_when_finished(user_id: int, session: InterviewSession) -> None:
    """定制面试正常结束后，清除该用户待执行状态，避免下次接通重复同一套题。"""
    if (
        getattr(session, "finished", False)
        and session.mode == "mock"
        and getattr(session, "custom_questions", None)
    ):
        voice_store.clear_custom_interview(user_id)


class VoiceConnection:
    """一通语音通话：连接状态、会话恢复、ASR 生命周期与消息分发。"""

    def __init__(self, ws: WebSocket, user: dict) -> None:
        self.ws = ws
        self.user_id = user["id"]
        self.persona = user.get("persona") or ""
        self.session: InterviewSession | None = None
        self.generation: asyncio.Task | None = None
        self.loop = asyncio.get_running_loop()
        self.asr: DashScopeASR | None = None
        self.asr_retry: asyncio.Task | None = None
        self.asr_stopping = False
        self.audio_seen = False  # 是否收到过麦克风音频帧（诊断用）

    # ---- 生命周期 ----

    async def run(self) -> None:
        """认证通过并 accept 之后的全部连接生命周期。"""
        await self._register()
        self.session, greeting = self._restore_session()
        # 新建会话（语音发起的默认答疑 / 语音定制）落库，供文字版与后续语音共享
        if getattr(self.session, "session_id", None) is None:
            session_store.start_session(self.user_id, self.session)
        self.generation = asyncio.create_task(_produce_greeting(self.ws, greeting))
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await self._handle_text_frame(msg["text"])
                elif msg.get("bytes") is not None:
                    self._handle_audio_frame(msg["bytes"])
        except WebSocketDisconnect:
            logger.info("语音通话已断开")
        finally:
            self._cleanup()

    async def _register(self) -> None:
        """单用户互踢：先原子登记自己，再关闭同账号旧连接（bug #14）。

        旧顺序（先查旧→await close→再登记）存在挂起间隙：两个新连接同时接通时
        后恢复者覆盖先恢复者，先恢复的连接变成活跃但脱管，绕过互踢常驻在线。
        现在登记与覆盖在同一同步段内完成（无 await），清理处已有 `is self.ws`
        身份校验，被顶掉的连接自行清理，不会误删后到者的登记。
        """
        old = _CONNECTIONS.get(self.user_id)
        _CONNECTIONS[self.user_id] = self.ws
        if old is not None and old is not self.ws:
            with suppress(Exception):
                await old.close(code=WS_CLOSE_KICKED, reason="已在其他页面接通")

    def _restore_session(self) -> tuple[InterviewSession, str]:
        """按用户恢复/新建会话，返回（会话, 开场白）。"""
        custom = voice_store.load_custom_interview(self.user_id)
        if custom:
            # 已准备定制面试 → 接通即进入模拟面试，用定制题目
            session = InterviewSession(
                "mock",
                questions=custom["questions"],
                job_title=custom.get("job_title", ""),
                jd=custom.get("jd", ""),
                persona=self.persona,
                user_id=self.user_id,
            )
            return session, _build_custom_greeting(custom)
        # 该用户未结束的活跃会话 → 接着聊；否则默认辅导答疑
        session = session_store.load_active_session(self.user_id)
        if session is not None and not session.finished:
            return session, REOPEN_GREETING
        return InterviewSession("coach", persona=self.persona, user_id=self.user_id), (
            prompts.VOICE_GREETING
        )

    def _cleanup(self) -> None:
        self.asr_stopping = True
        if self.asr_retry is not None:
            self.asr_retry.cancel()
        if self.generation is not None and not self.generation.done():
            self.generation.cancel()
        if self.asr is not None:
            self.asr.stop()
        if _CONNECTIONS.get(self.user_id) is self.ws:
            _CONNECTIONS.pop(self.user_id, None)

    # ---- 消息分发 ----

    async def _handle_text_frame(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        if mtype == "stop":
            self._cancel_generation()
            return
        if mtype == "asr_start":
            # 前端告知采集采样率 → 创建 DashScope 流式 ASR；
            # 后续断线由 _asr_supervisor 自动重连，无需前端反复重发
            if self.asr is None and (self.asr_retry is None or self.asr_retry.done()):
                sr = int(msg.get("sample_rate") or config.ASR_SAMPLE_RATE)
                # 配置类故障（缺依赖/缺 Key）重试也无效，直接告知终态、不启动重连（bug #23）
                if asr_permanently_unavailable():
                    await self._send(
                        {
                            "type": "asr_error",
                            "message": "语音识别不可用（未配置识别服务），请检查服务端配置",
                        }
                    )
                    return
                if not await self._start_asr(sr):
                    await self._send(
                        {
                            "type": "asr_error",
                            "message": "语音识别启动失败，自动重试中…",
                        }
                    )
                self.asr_retry = asyncio.create_task(self._asr_supervisor(sr))
            return
        if mtype != "text":
            return
        await self._handle_user_text((msg.get("content") or "").strip())

    def _handle_audio_frame(self, data: bytes) -> None:
        """二进制 PCM 音频帧 → ASR 流式识别。"""
        if self.asr is not None:
            if not self.audio_seen:
                self.audio_seen = True
                logger.info("收到麦克风音频帧（ASR 识别中）")
            try:
                self.asr.send(data)
            except Exception:
                logger.exception("audio 帧处理失败")
        elif not self.audio_seen:
            self.audio_seen = True
            logger.info("收到麦克风音频帧但 ASR 未就绪（自动重连中，稍候恢复）")

    async def _handle_user_text(self, text: str) -> None:
        if not text:
            return
        # 限流：防止单连接刷 text 烧 LLM 余额（超限返回结构化错误）
        if not hit(
            f"ws:text:{self.user_id}",
            config.VOICE_TEXT_RATE_LIMIT,
            config.VOICE_TEXT_RATE_WINDOW,
        ):
            await self._send(
                {
                    "type": "error",
                    "code": "rate_limit",
                    "message": "请求过于频繁，请稍候再试",
                }
            )
            return
        session = self.session
        new_session = maybe_switch_to_mock(session, text)
        if new_session is not session:
            # 从答疑切换为模拟面试：新会话入库，替换当前会话
            self.session = session = new_session
            session_store.start_session(self.user_id, session)
        # 用户开口 → 取消上一轮未完成的生成（barge-in）
        self._cancel_generation()
        task = asyncio.create_task(_produce(self.ws, session, text))

        def _on_produce_done(t, s=session):
            # 被打断（barge-in）或生成异常的任务不落库：前者的半截回复由下一轮
            # 正常完成的任务一并持久化；后者生成器已回滚，保留上一轮已存状态，
            # 避免"finished=True + 空 assistant 消息"的损坏状态出库（bug #4）
            if t.cancelled() or t.exception() is not None:
                return
            # 定制面试结束（输出总结报告）后清除待执行状态，避免下次接通重复同一套题
            _clear_custom_when_finished(self.user_id, s)
            # 每次回复完成即持久化会话状态（断线/刷新可恢复）
            with suppress(Exception):
                session_store.save_session(self.user_id, s)

        task.add_done_callback(_on_produce_done)
        self.generation = task

    def _cancel_generation(self) -> None:
        if self.generation is not None and not self.generation.done():
            self.generation.cancel()

    async def _send(self, obj: dict) -> None:
        with suppress(Exception):
            await self.ws.send_text(json.dumps(obj, ensure_ascii=False))

    # ---- ASR ----

    async def _on_asr_sentence(self, text: str) -> None:
        """ASR 识别出完整句子 → 回传前端（前端按用户输入处理）。"""
        await self._send({"type": "asr_text", "content": text})

    async def _on_asr_partial(self, text: str) -> None:
        """ASR 中间结果 → 回传前端，用于"开口即打断"（不等整句识别完）。"""
        await self._send({"type": "asr_partial", "content": text})

    async def _on_asr_error(self, code=None, message=None) -> None:
        """ASR 识别流中途出错：置 None，由监督任务自动重连（指数退避）。"""
        self.asr = None
        detail = f"{code}: {message}" if code else (message or "未知错误")
        logger.warning("ASR 识别流中断: %s", detail)
        await self._send(
            {
                "type": "asr_error",
                "message": f"语音识别中断（{detail}），正在自动重试…",
            }
        )

    async def _start_asr(self, sr: int) -> bool:
        """创建并启动 DashScope ASR；成功回传 asr_ready。"""
        try:
            self.asr = DashScopeASR(
                self.loop,
                self._on_asr_sentence,
                on_partial=self._on_asr_partial,
                on_error=self._on_asr_error,
                sample_rate=sr,
            )
        except Exception as e:
            self.asr = None
            logger.exception("ASR 实例创建失败: %s", e)
            return False
        if self.asr.start():
            logger.info("ASR 已启动 (sample_rate=%s)", sr)
            await self._send({"type": "asr_ready"})
            return True
        self.asr = None
        return False

    async def _asr_supervisor(self, sr: int) -> None:
        """ASR 断线后自动重连（指数退避 3s→20s），直到通话结束或永久不可用。"""
        delay = ASR_RETRY_DELAY
        while not self.asr_stopping:
            await asyncio.sleep(delay)
            if self.asr_stopping:
                return
            # 配置类故障（缺依赖/缺 Key）重试也无效，停止空转并告知终态（bug #23）
            if asr_permanently_unavailable():
                await self._send(
                    {
                        "type": "asr_error",
                        "message": "语音识别不可用（未配置识别服务），请检查服务端配置",
                    }
                )
                return
            if self.asr is None:
                ok = await self._start_asr(sr)
                delay = ASR_RETRY_DELAY if ok else min(delay * 2, ASR_RETRY_MAX)
            else:
                delay = ASR_RETRY_DELAY


@router.websocket("/ws/voice")
async def voice(ws: WebSocket) -> None:
    # 多用户认证：浏览器 WebSocket 无法携带请求头，URL 只携带一次性短时票据
    # （消费即删除，单次有效）；长效登录令牌不再出现在 URL（bug #23）
    user = auth.resolve_ws_ticket(ws.query_params.get("ticket"))
    if user is None:
        await ws.close(code=4401, reason="未登录或登录已过期")
        return
    await ws.accept()
    await VoiceConnection(ws, user).run()


# ---- 回复生成（LLM 流式 + TTS 推送）----


async def _produce(ws: WebSocket, session: InterviewSession, text: str) -> None:
    """把用户文本推进会话，流式生成文本+语音；任务可被取消（barge-in）。"""
    gen = session.handle_stream(text)

    def _next_chunk():
        try:
            return next(gen)
        except StopIteration:
            return _END

    buf = ""  # 未切分句的流式残余
    tts_buf = ""  # 待合成的多句缓冲（合并后一次合成，减少连接数、保留句间语气）
    state = tts.TtsState()
    tts_tasks: list[asyncio.Task] = []
    sem = asyncio.Semaphore(tts.TTS_MAX_CONCURRENCY)
    # 语音回合也要维护共享文字历史，否则 REST 侧 history_for_display 优先读
    # display_history 时，语音回合会从文字历史中永久缺失（bug #7）
    session.display_history.append(["user", text])

    # 推送串行化：并发合成的任务按"创建顺序"依次推送，保证 sid 分配顺序 ==
    # 音频内容顺序。否则先合成完成的任务先 flush，sid 会被后创建的内容抢占，
    # 浏览器按 sid 排序播放时内容就会乱序（语音与字幕不同步）。
    turn = 0
    cond = asyncio.Condition()

    async def _await_turn(my: int) -> None:
        async with cond:
            await cond.wait_for(lambda: turn == my)

    async def _end_turn() -> None:
        nonlocal turn
        async with cond:
            turn += 1
            cond.notify_all()

    _next_slot = [0]

    def _flush_tts() -> None:
        nonlocal tts_buf
        if not tts_buf.strip():
            return
        slot = _next_slot[0]
        _next_slot[0] += 1
        tts_tasks.append(asyncio.create_task(_tts(tts_buf, slot)))
        tts_buf = ""

    async def _tts(chunk: str, slot: int) -> None:
        """合成一段（约 2-3 句）并按序推送。

        两个阶段的锁粒度必须分开（bug #12）：
        - 合成受 sem 限流，**可并发**（CosyVoice 单段要 4-6 秒，串行会慢到不可用）；
        - 推送受 _await_turn 顺序锁保护，保证 sid 分配顺序 == 音频内容顺序。
        此前顺序锁套在合成之外，把合成本身也一起串行化了，sem 形同虚设。

        降级开关 state.ok 在临界区内读取并置位：某段在线合成失败后，其余段
        立即跳过在线合成改走本地语音，不必各自再等一次超时（并发后尤其重要，
        否则同批进 sem 的几段会各自白等一次）。
        """
        try:
            chunk = tts.clean_tts_text(chunk)
            if not chunk:
                return
            async with sem:
                if state.ok:
                    unit = await tts.synth(state, chunk)
                    if not unit.ok:
                        state.ok = False
                else:
                    unit = tts.TtsUnit(chunk)
            # 取得推送顺序权后再推：sid 必须按文本顺序分配
            await _await_turn(slot)
            await tts.push(ws, state, unit)
        except asyncio.CancelledError:
            raise
        finally:
            # 无论成功/失败/取消都让出推送权，避免卡死后续任务
            await _end_turn()

    try:
        # 先告知浏览器本段回复第一个音频块的 sid，即使音频块乱序到达也能按序播放
        await ws.send_text(json.dumps({"type": "reply_start", "first_sid": state.sid + 1}))
        while True:
            # OpenAI 同步流在生成器内部阻塞，放到线程执行，避免卡住事件循环
            delta = await asyncio.to_thread(_next_chunk)
            if delta is _END:
                break
            if delta:
                await ws.send_text(
                    json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)
                )
                buf += delta
                sentences, buf = tts.split_sentences(buf)
                # 多句合并合成：首块尽快开播，后续按 ~90 字合并，减少连接数与句间语气割裂
                for s in sentences:
                    if not s.strip():
                        continue
                    tts_buf += s
                    limit = tts.TTS_FIRST_CHARS if not tts_tasks else tts.TTS_CHUNK_CHARS
                    if len(tts_buf) >= limit:
                        _flush_tts()
        if buf.strip():
            tts_buf += buf
        _flush_tts()
        if tts_tasks:
            await asyncio.gather(*tts_tasks)
        # 助手回复补进共享文字历史（与 REST chat 一致，刷新后文字版能看到语音回合，bug #7）
        if session.messages and session.messages[-1].get("role") == "assistant":
            session.display_history.append(["assistant", session.messages[-1]["content"]])
        await ws.send_text(json.dumps({"type": "done"}))
    except asyncio.CancelledError:
        for t in tts_tasks:
            t.cancel()
        logger.info("回复生成被用户打断")
        with suppress(Exception):
            await ws.send_text(json.dumps({"type": "cancelled"}))
    except Exception:
        for t in tts_tasks:
            t.cancel()
        logger.exception("回复生成失败")
        # 不向前端泄漏内部异常细节，只发结构化错误码 + 友好文案
        with suppress(Exception):
            await ws.send_text(
                json.dumps(
                    {
                        "type": "error",
                        "code": "internal",
                        "message": "生成回复时出错，请稍后再试",
                    },
                    ensure_ascii=False,
                )
            )


async def _produce_greeting(ws: WebSocket, greeting: str) -> None:
    """接通后先播报开场白，像真实通话一样不等用户开口。可被 barge-in 取消。"""
    sentences, rest = tts.split_sentences(greeting)
    if rest.strip():
        sentences.append(rest.strip())
    state = tts.TtsState()
    try:
        await ws.send_text(json.dumps({"type": "reply_start", "first_sid": 1}))
        # 文本逐句展示；语音整段一次合成：同一句开场白只有一个音色，
        # 不会出现"前一句 CosyVoice、后一句 edge-tts"的两种声音
        for s in sentences:
            if not s.strip():
                continue
            await ws.send_text(json.dumps({"type": "delta", "content": s}, ensure_ascii=False))
        unit = await tts.synth(state, greeting.strip())
        await tts.push(ws, state, unit)
        await ws.send_text(json.dumps({"type": "done"}))
    except asyncio.CancelledError:
        logger.info("开场白被用户打断")
        with suppress(Exception):
            await ws.send_text(json.dumps({"type": "cancelled"}))
