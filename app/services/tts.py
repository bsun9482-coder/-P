"""TTS 合成与音频推送（从统一服务 main.py 拆出）。

职责：文本清洗、句切分、TTS 引擎策略（CosyVoice / edge-tts）、
连接级熔断、音频聚合推送（audio_start/audio/audio_end 三连帧）。

设计：
- TTSEngine 策略：两种引擎都实现 stream(sentence) 异步迭代音频块。
  CosyVoice 一次性返回整段，edge-tts 流式返回小块；聚合与推送逻辑公共；
- 聚合推送：edge-tts 小块聚合成 ~8KB 单元再推，减少浏览器解码调度，
  保持"边合成边播"；
- 降级链：CosyVoice 失败后本回复锁定 edge（防混音），edge 失败发
  tts_error 由浏览器回退本地语音；
- 熔断：连续失败 N 次后暂停在线合成一段时间（TtsState 按连接隔离，
  一个连接的网络抖动不拖累其他连接）。
"""

import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol

import requests

from app.core import config

try:
    import edge_tts
except ImportError:  # 未安装时降级：浏览器回退 speechSynthesis
    edge_tts = None

logger = logging.getLogger("interview_coach.tts")

# ---- 聚合与并发参数 ----

#: edge-tts 原始音频块非常小（约 0.13s/块），逐块推送会让浏览器频繁解码调度导致卡顿；
#: 服务端聚合成 ~8KB（约 1.5-2s 语音）再推一块，既保持"边合成边播"，又大幅减少播放单元数。
TTS_CHUNK_TARGET = 8 * 1024

#: 多句合并合成：每个 edge-tts 连接承载约 2-3 句话（连接数少 5 倍，失败面小；
#: 句与句之间的语气不再被割裂，听起来更自然、不机械）。
TTS_FIRST_CHARS = 45  # 首块阈值：尽快开播
TTS_CHUNK_CHARS = 90  # 后续块阈值：约 2-3 句

#: 并行合成：多段同时交给 CosyVoice（每段请求 4-6 秒），整条回复时间大幅缩短；
#: 合成快于播放，浏览器队列不会断流。失败降级仍按"段"判定，个别失败只影响该段。
TTS_MAX_CONCURRENCY = 3

#: edge-tts 跨回复熔断：连续失败 N 次后暂停在线合成一段时间，直接降级本地语音
#: （避免每次干等超时）。熔断状态在 TtsState 里（按连接隔离）。
TTS_CIRCUIT_FAILS = 2
TTS_CIRCUIT_COOLDOWN = 60

_SENT_END = re.compile(r"[。！？；\n.!?;]")

#: 模型常见舞台指示，如（点头微笑）（皱眉）——TTS 会原样念出来，非常出戏。
#: 只去掉"纯中文、短、不含数字/字母"的括号内容，保留 (O(n))、TCP/IP 这类技术内容。
_TTS_STAGE_DIR = re.compile(r"[（(][^（）()A-Za-z0-9]{0,8}[）)]")


@dataclass
class TtsState:
    """TTS 推送状态（每连接/每次回复独立，字段显式可检查）。"""

    sid: int = 0  # 音频单元 sid 分配器（同一回复内递增，保证唯一且有序）
    ok: bool = True  # 本回复是否仍走在线合成；False 后剩余段全部降级本地语音
    voice: str = ""  # 本回复锁定的引擎（"cosyvoice"/"edge"），防止混音
    fails: int = 0  # 连续失败计数（连接级熔断）
    open_until: float = 0.0  # 熔断截止时刻（time.monotonic()）


@dataclass
class TtsUnit:
    """一段已合成、待推送的语音。

    合成（synth）与推送（push）分离，是为了让调用方能对两者分别加锁：
    合成受并发信号量限制、可并行；推送必须按文本顺序串行以保证 sid 顺序
    （此前两者被同一把顺序锁圈住，合成本身被串行化）。
    """

    text: str  # 原始文本（降级时前端据此本地朗读）
    blocks: list[bytes] = field(default_factory=list)  # 按序推送的音频单元
    ok: bool = False  # 在线合成是否完整成功；False 且 blocks 非空 = 只合成了部分


class Sender(Protocol):
    """推送目标：FastAPI WebSocket 与测试 FakeWS 都满足。"""

    async def send_text(self, data: str) -> None: ...


class CosyVoiceUnavailable(Exception):
    """CosyVoice 在线合成失败（欠费/配额/网络），应降级 edge-tts。"""


def _jmsg(obj: dict) -> str:
    """统一 JSON 序列化（中文不转义）。"""
    return json.dumps(obj, ensure_ascii=False)


# ---- 文本处理 ----


def split_sentences(buf: str) -> tuple[list[str], str]:
    """按句子边界切分，返回（完整句子列表, 剩余缓冲）。"""
    out: list[str] = []
    while True:
        m = _SENT_END.search(buf)
        if not m:
            break
        idx = m.end()
        out.append(buf[:idx].strip())
        buf = buf[idx:]
    return out, buf


def clean_tts_text(text: str) -> str:
    """合成前清洗：去掉舞台指示等不应被朗读的内容。"""
    return _TTS_STAGE_DIR.sub("", text or "").strip()


# ---- 引擎策略 ----


class TtsEngine(Protocol):
    """TTS 引擎统一接口：迭代返回音频块，失败抛异常。"""

    async def stream(self, sentence: str) -> AsyncIterator[bytes]: ...


class EdgeTTS:
    """edge-tts 流式合成：小块输出，配合聚合推送实现边合成边播。"""

    async def stream(self, sentence: str) -> AsyncIterator[bytes]:
        comm = edge_tts.Communicate(
            sentence,
            voice=config.VOICE_NAME,
            rate=config.VOICE_RATE,
            pitch=config.VOICE_PITCH,
        )
        async for chunk in comm.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                yield chunk["data"]


class CosyVoiceTTS:
    """CosyVoice 在线合成：一次返回整段音频，作为一个播放单元推送。"""

    async def stream(self, sentence: str) -> AsyncIterator[bytes]:
        audio = await _cosyvoice_synthesize(sentence)
        if not audio:
            raise CosyVoiceUnavailable(sentence)
        yield audio


# ---- CosyVoice HTTP 客户端（同步，在线程中调用）----


def _cosyvoice_request(sentence: str) -> bytes | None:
    """同步请求阿里云百炼 CosyVoice，返回音频字节；失败返回 None（在线程中调用）。"""
    key = config.DASHSCOPE_API_KEY
    if not key:
        return None
    payload = {
        "model": config.COSYVOICE_MODEL,
        "input": {
            "text": sentence,
            "voice": config.COSYVOICE_VOICE,
            "format": config.COSYVOICE_FORMAT,
            "sample_rate": config.COSYVOICE_SAMPLE_RATE,
            "rate": config.COSYVOICE_RATE,
            "pitch": config.COSYVOICE_PITCH,
        },
    }
    try:
        resp = requests.post(
            "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("CosyVoice 请求失败: %s", e)
        return None
    audio = (data.get("output") or {}).get("audio") or {}
    if audio.get("data"):
        try:
            return base64.b64decode(audio["data"])
        except Exception as e:
            logger.warning("CosyVoice 音频解码失败: %s", e)
            return None
    url = audio.get("url")
    if not url:
        logger.warning("CosyVoice 响应缺少音频: %s", str(data)[:200])
        return None
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.warning("CosyVoice 音频下载失败: %s", e)
        return None


async def _cosyvoice_synthesize(sentence: str) -> bytes | None:
    """异步包装：在线程中请求 CosyVoice，返回完整音频字节。"""
    return await asyncio.to_thread(_cosyvoice_request, sentence)


# ---- 熔断（连接级，状态在 TtsState）----


def _circuit_open(state: TtsState) -> bool:
    return time.monotonic() < state.open_until


def _note_failure(state: TtsState) -> None:
    state.fails += 1
    if state.fails >= TTS_CIRCUIT_FAILS:
        state.open_until = time.monotonic() + TTS_CIRCUIT_COOLDOWN
        logger.warning(
            "本连接 edge-tts 连续失败 %s 次，熔断 %s 秒，期间直接使用浏览器本地语音",
            state.fails,
            TTS_CIRCUIT_COOLDOWN,
        )


def _note_success(state: TtsState) -> None:
    state.fails = 0


# ---- 推送 ----


def _next_sid(state: TtsState) -> int:
    state.sid += 1
    return state.sid


async def _send_fail(ws: Sender, state: TtsState, sentence: str) -> None:
    """通知前端该段合成失败（先发 audio_start 携带原文，供浏览器回退朗读）。"""
    my_sid = _next_sid(state)
    await ws.send_text(_jmsg({"type": "audio_start", "sid": my_sid, "text": sentence}))
    await ws.send_text(_jmsg({"type": "tts_error", "sid": my_sid}))


async def _flush(ws: Sender, state: TtsState, data: bytes, text: str) -> None:
    """把一块音频作为一个播放单元推送（audio_start/audio/audio_end 三连）。"""
    my_sid = _next_sid(state)
    payload = base64.b64encode(data).decode("ascii")
    await ws.send_text(_jmsg({"type": "audio_start", "sid": my_sid, "text": text}))
    await ws.send_text(_jmsg({"type": "audio", "sid": my_sid, "data": payload}))
    await ws.send_text(_jmsg({"type": "audio_end", "sid": my_sid}))


async def _synth_once(state: TtsState, sentence: str) -> tuple[list[bytes], bool]:
    """尝试一次在线合成，返回（按序的音频单元列表, 是否完整成功）。

    只合成、不推送：推送由调用方在取得顺序权后执行。
    """
    if config.VOICE_TTS == "cosyvoice" and state.voice != "edge":
        blocks: list[bytes] = []
        try:
            async for block in CosyVoiceTTS().stream(sentence):
                blocks.append(block)
        except CosyVoiceUnavailable:
            # CosyVoice 不可用 → 本回复后续段落统一用 edge-tts，
            # 避免同一条回复里混两种音色（听起来像"两个声音"）
            state.voice = "edge"
            logger.warning("CosyVoice 不可用，本回复后续段落降级 edge-tts 保持音色一致")
        else:
            state.voice = "cosyvoice"
            return blocks, True
    if edge_tts is None:
        return [], False
    units: list[bytes] = []
    buf: list[bytes] = []
    size = 0
    try:
        async for block in EdgeTTS().stream(sentence):
            buf.append(block)
            size += len(block)
            if size >= TTS_CHUNK_TARGET:
                units.append(b"".join(buf))
                buf = []
                size = 0
        if buf:
            units.append(b"".join(buf))
        return units, True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("TTS 在线合成失败（sentence=%s）: %s", sentence[:30], e)
        if buf:
            # 已合成出部分音频：一并返回推送，避免浏览器整句重播造成重复/卡顿
            units.append(b"".join(buf))
        return units, False


async def synth(state: TtsState, sentence: str) -> TtsUnit:
    """合成一段语音但**不推送**，返回待推送单元。

    连接级失败（尚未出音频）会重试一次；失败/成功都会更新连接级熔断状态。
    与 push() 分离，是为了让调用方把"合成"（可并发）与"推送"（必须按文本
    顺序串行）分别加锁——此前两者被同一把顺序锁圈住，合成本身被串行化。
    """
    if not sentence.strip():
        return TtsUnit(sentence)
    if config.VOICE_TTS not in ("edge", "cosyvoice") or _circuit_open(state):
        return TtsUnit(sentence)
    if config.VOICE_TTS == "cosyvoice" and not config.DASHSCOPE_API_KEY:
        return TtsUnit(sentence)
    blocks, ok = await _synth_once(state, sentence)
    if not ok and not blocks:
        # 连接级失败且完全没出音频：重试一次（edge-tts 多为瞬时连接失败）
        blocks, ok = await _synth_once(state, sentence)
    if ok:
        _note_success(state)
    else:
        _note_failure(state)
    return TtsUnit(sentence, blocks, ok)


async def push(ws: Sender, state: TtsState, unit: TtsUnit) -> None:
    """推送一个已合成单元：按序分配 sid 并发送音频三连帧。

    无音频（在线合成失败/本回复已降级）时改发 audio_start + tts_error，
    由浏览器回退本地朗读。调用方必须先取得推送顺序权，否则 sid 会被后创建的
    内容抢占，浏览器按 sid 排序播放时内容就会乱序。
    """
    if not unit.blocks:
        with suppress(Exception):
            await _send_fail(ws, state, unit.text)
        return
    for data in unit.blocks:
        await _flush(ws, state, data, unit.text)
