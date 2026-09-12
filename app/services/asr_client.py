"""DashScope 实时语音识别（Paraformer）客户端。

替代 Chrome 内置 SpeechRecognition（依赖 Google 服务，国内网络不可用）。
前端把麦克风 PCM 音频流经 WebSocket 送到本服务，这里用 dashscope SDK
做流式识别，把"句子结束"的识别结果通过回调桥接到 asyncio 事件循环，
再回传给前端（{"type":"asr_text","content":...}）。

- 每通语音通话创建一个 DashScopeASR（一次 start 持续识别，服务端 VAD 自动断句）；
- 前端在小P播报时暂停发送音频（防回声被识别），用户开口（VAD 打断）后恢复发送；
- SDK 回调运行在内部 worker 线程，用 run_coroutine_threadsafe 桥接到事件循环。
"""

import asyncio
import logging
from contextlib import suppress

try:
    import dashscope
    from dashscope.audio.asr import Recognition, RecognitionCallback

    _DASHSCOPE_AVAILABLE = True
except ImportError:  # 未安装 dashscope 时降级：仅 ASR 不可用，语音服务仍可启动（TTS/对话正常）
    dashscope = None
    Recognition = None
    RecognitionCallback = object
    _DASHSCOPE_AVAILABLE = False

from app.core import config

logger = logging.getLogger("interview_coach.asr")


class _AsrCallback(RecognitionCallback):
    """SDK 回调：句子结束/中间结果的识别文本、错误事件桥接到 asyncio。"""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_sentence,
        on_partial=None,
        on_error=None,
    ) -> None:
        self._loop = loop
        self._on_sentence = on_sentence  # 异步回调（text：识别文本），句子结束
        self._on_partial = on_partial  # 异步回调（text：识别文本），中间结果（供提前打断）
        self._on_error = on_error  # 异步回调（code, message：错误码与错误信息）

    def on_event(self, result) -> None:
        try:
            sentence = result.get_sentence()
        except Exception:
            sentence = None
        if not sentence:
            return
        try:
            is_end = result.is_sentence_end(sentence)
        except Exception:
            is_end = True
        text = (sentence.get("text") or "").strip() if isinstance(sentence, dict) else ""
        if not text:
            return
        if is_end and self._on_sentence is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._on_sentence(text), self._loop)
            except RuntimeError:
                logger.warning("事件循环已关闭，丢弃识别结果: %s", text[:20])
        elif not is_end and self._on_partial is not None:
            # 中间结果：前端据此在用户开口时立即打断，不等整句识别完成
            with suppress(RuntimeError):
                asyncio.run_coroutine_threadsafe(self._on_partial(text), self._loop)

    def on_error(self, result) -> None:
        code = getattr(result, "code", None)
        message = getattr(result, "message", None)
        logger.warning("ASR 识别错误: code=%s message=%s", code, message)
        if self._on_error is not None:
            with suppress(RuntimeError):
                asyncio.run_coroutine_threadsafe(self._on_error(code, message), self._loop)


def asr_permanently_unavailable() -> bool:
    """ASR 是否"永久不可用"（缺依赖 / 缺 API Key），重连也不会变好。

    供语音链路区分瞬时故障（网络抖动，值得指数退避重连）与配置类故障
    （重试再多次也不可能成功，应停止空转并告知用户终态，bug #23）。
    """
    return not _DASHSCOPE_AVAILABLE or not config.DASHSCOPE_API_KEY


class DashScopeASR:
    """每通语音通话一个实例：start 后持续 send PCM，句子结束回调识别文本。"""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_sentence,
        on_partial=None,
        on_error=None,
        sample_rate: int | None = None,
        model: str | None = None,
    ) -> None:
        self._loop = loop
        self._rec = None
        self._started = False
        if not _DASHSCOPE_AVAILABLE:
            return
        dashscope.api_key = config.DASHSCOPE_API_KEY
        self._rec = Recognition(
            model=model or config.ASR_MODEL,
            callback=_AsrCallback(loop, on_sentence, on_partial, on_error),
            format="pcm",
            sample_rate=sample_rate or config.ASR_SAMPLE_RATE,
            language_hints=["zh"],
        )

    def start(self) -> bool:
        """启动识别。失败返回 False（不阻断通话，仅提示）。"""
        if self._rec is None:
            logger.warning("未安装 dashscope 依赖，语音识别不可用（pip install dashscope 后重启）")
            return False
        if not config.DASHSCOPE_API_KEY:
            logger.warning("未配置 DASHSCOPE_API_KEY，语音识别不可用")
            return False
        try:
            self._rec.start()
            self._started = True
            return True
        except Exception:
            logger.exception("ASR 启动失败")
            return False

    def send(self, data: bytes) -> None:
        """推送一段 PCM 音频（线程安全，内部入队）。"""
        if self._started and data:
            try:
                self._rec.send_audio_frame(data)
            except Exception:
                logger.exception("ASR 发送音频失败")

    def stop(self) -> None:
        """停止识别并释放连接。"""
        if self._rec is None or not self._started:
            return
        try:
            self._rec.stop()
        except Exception:
            logger.exception("ASR 停止失败")
        finally:
            self._started = False
