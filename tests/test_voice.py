"""语音通话服务测试：模式切换 + WebSocket 流式回复（mock LLM，不触网）。"""

import asyncio
import base64
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.agent.coach import InterviewSession
from app.core import config, db
from app.core.ratelimit import reset_rate_limits
from app.main import app, health
from app.services.tts import TtsState, TtsUnit, _cosyvoice_synthesize
from app.services.tts import push as _push
from app.services.tts import synth as _synth
from app.stores import auth
from app.voice_ws import maybe_switch_to_mock
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect


def _test_token() -> str:
    """创建/取测试用户并签发令牌（多用户语音 WS 需登录）。"""
    user = db.get_user_by_username("voice_test")
    if user is None:
        db.create_user("voice_test", auth.hash_password("testpass123"))
        user = db.get_user_by_username("voice_test")
    db.archive_active_session(user["id"])  # 清理上一轮测试的活跃会话
    return auth.issue_token(user["id"])


async def _synthesize(ws, state, sentence):
    """测试便捷封装：合成 + 按序推送（等价于拆分前的单步 synthesize）。

    生产路径把两步分开，是为了让合成可并发、推送按序（bug #12）；
    单连接的单元测试无并发需求，直接串联即可。
    """
    unit = await _synth(state, sentence)
    await _push(ws, state, unit)
    return unit.ok


async def _fake_synth(state, sentence):
    """假合成：只产出占位音频，推送交给真实 push（保证 sid 逻辑被覆盖）。"""
    return TtsUnit(text=sentence, blocks=[b"FAKE-AUDIO"], ok=True)


def _recv_until_done(ws):
    """接收消息直到 'done'，返回 (deltas, audio_started, audio_ended, tts_errors)。"""
    deltas: list[str] = []
    audio_started = audio_ended = 0
    tts_errors = 0
    while True:
        msg = json.loads(ws.receive_text())
        t = msg["type"]
        if t == "delta":
            deltas.append(msg["content"])
        elif t == "audio_start":
            audio_started += 1
        elif t == "audio_end":
            audio_ended += 1
        elif t == "tts_error":
            tts_errors += 1
        elif t == "done":
            break
    return deltas, audio_started, audio_ended, tts_errors


#: 足够长、会被多句合并逻辑拆成两段（>TTS_FIRST_CHARS 触发首段）的回复文本
LONG_SPLIT_TEXT = (
    "第一句，这是用于测试合成中途失败的长句子内容，字数要足够多。"
    "第二句，这也是用于测试的较长句子内容。"
    "第三句继续。第四句继续。"
)


class VoiceServerTests(unittest.TestCase):
    def setUp(self):
        # 隔离数据库：语音测试不污染真实 data/questions.db
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "test_voice.db"
        self._db_patch = mock.patch.object(config, "DB_PATH", self._db_path)
        self._db_patch.start()
        db.init_db()
        self.token = _test_token()
        # ws-ticket 签发接口与注册/登录共享进程级限流桶，测试间清空防串扰
        reset_rate_limits()

    def tearDown(self):
        self._db_patch.stop()
        reset_rate_limits()
        self._tmpdir.cleanup()

    def _ws_url(self, client) -> str:
        """经 REST 用 Bearer 令牌换取一次性票据，返回新协议 WS 连接地址（bug #23）。

        每次调用签发新票据（单次消费），多连接场景各自独立取票。
        """
        r = client.post("/api/auth/ws-ticket", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.status_code, 200)
        return f"/ws/voice?ticket={r.json()['ticket']}"

    def test_old_token_query_param_rejected(self):
        """回归断言：旧协议 ?token= 携带有效令牌也必须被拒（bug #23 防旧协议回归）。"""
        with (
            TestClient(app) as client,
            self.assertRaises(WebSocketDisconnect),
            client.websocket_connect(f"/ws/voice?token={self.token}"),
        ):
            pass

    def test_maybe_switch_to_mock_on_first_message(self):
        s = InterviewSession("coach")
        s2 = maybe_switch_to_mock(s, "我想开始面试")
        self.assertEqual(s2.mode, "mock")
        # bug #15 修复后：恢复的活跃会话（多条消息）说"开始面试"也应切换，
        # 与 REOPEN_GREETING 的承诺一致
        s = InterviewSession("coach")
        s.messages.append({"role": "user", "content": "之前问过"})
        s.messages.append({"role": "assistant", "content": "回答"})
        self.assertEqual(maybe_switch_to_mock(s, "开始面试").mode, "mock")

    def test_maybe_switch_to_mock_no_false_positive(self):
        """首条消息只是提问"模拟面试"相关概念，不应误切成模拟面试模式。"""
        for t in ("模拟面试和辅导答疑有什么区别？", "模拟面试是什么", "模拟面试怎么开始"):
            s = InterviewSession("coach")
            self.assertEqual(maybe_switch_to_mock(s, t).mode, "coach", t)
        # 明确的开始意图仍应切换
        for t in ("模拟面试", "模拟面试吧", "我想模拟面试", "来一场模拟面试", "帮我模拟面试"):
            s = InterviewSession("coach")
            self.assertEqual(maybe_switch_to_mock(s, t).mode, "mock", t)

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch("app.agent.llm.chat_stream", return_value=iter(["标准", "答案"]))
    def test_websocket_streams_reply(self, mock_chat_stream, mock_fts):
        with (
            TestClient(app) as client,
            mock.patch("app.services.tts.synth", side_effect=_fake_synth),
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            # 接通后先收到开场白（像打电话一样），消化完再提问
            greeting_deltas, *_ = _recv_until_done(ws)
            ws.send_text(
                json.dumps({"type": "text", "content": "Redis 怎么答"}, ensure_ascii=False)
            )
            deltas, audio_started, audio_ended, _ = _recv_until_done(ws)
        self.assertTrue("".join(greeting_deltas).strip())
        self.assertEqual("".join(deltas), "标准答案")
        self.assertTrue(audio_started and audio_ended, "应推送音频开始/结束帧")

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch("app.agent.llm.chat_stream", return_value=iter([LONG_SPLIT_TEXT]))
    def test_tts_degrade_after_first_failure(self, mock_chat_stream, mock_fts):
        """第一段在线合成失败后，后续段落直接降级为本地语音（tts_error），不再反复尝试。"""

        async def fail_once(state, sentence):
            """假合成：总是失败（无音频），推送时改走降级 tts_error。"""
            return TtsUnit(text=sentence)

        with (
            TestClient(app) as client,
            mock.patch("app.services.tts.synth", side_effect=fail_once),
            mock.patch("app.services.tts.TTS_MAX_CONCURRENCY", 1),  # 串行：保证降级判定确定
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            _recv_until_done(ws)  # 消化开场白（同样走降级，不计数）
            ws.send_text(json.dumps({"type": "text", "content": "你好"}, ensure_ascii=False))
            _, audio_starts, _, tts_errors = _recv_until_done(ws)
        self.assertEqual(tts_errors, 2)  # 两句话都降级
        self.assertEqual(audio_starts, 2)  # 降级路径每句都先发 audio_start（供浏览器回退对应文本）

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch("app.agent.llm.chat_stream", return_value=iter(["流式回复。"]))
    def test_synthesize_streams_audio_chunks(self, mock_chat_stream, mock_fts):
        """edge-tts 小音频块被聚合成 ~8KB 大单元推送：单元数少、字节不丢、sid 有序。"""
        CHUNK = b"X" * 3000

        class FakeComm:
            def __init__(self, *args, **kwargs):
                pass

            async def stream(self):
                for _ in range(5):
                    yield {"type": "audio", "data": CHUNK}

        with (
            mock.patch("app.services.tts.edge_tts", SimpleNamespace(Communicate=FakeComm)),
            mock.patch("app.services.tts.config.VOICE_TTS", "edge"),
            TestClient(app) as client,
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            _recv_until_done(ws)  # 消化开场白
            first_sid = None
            ws.send_text(json.dumps({"type": "text", "content": "你好"}, ensure_ascii=False))
            sids: list[int] = []
            starts = ends = 0
            total_bytes = 0
            while True:
                m = json.loads(ws.receive_text())
                t = m["type"]
                if t == "reply_start":
                    first_sid = m["first_sid"]
                elif t == "audio_start":
                    starts += 1
                    sids.append(m["sid"])
                elif t == "audio":
                    total_bytes += len(base64.b64decode(m["data"]))
                elif t == "audio_end":
                    ends += 1
                elif t == "done":
                    break
        self.assertEqual(starts, 2)  # 5×3000B 聚合成 2 个单元（9000B + 6000B）
        self.assertEqual(ends, 2)
        self.assertEqual(total_bytes, 5 * 3000, "聚合不能丢字节")
        self.assertEqual(len(set(sids)), 2, "每个音频单元 sid 必须唯一")
        self.assertEqual(sorted(sids), list(range(first_sid, first_sid + 2)))

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch("app.agent.llm.chat_stream", return_value=iter([LONG_SPLIT_TEXT]))
    def test_synthesize_midstream_failure_no_replay(self, mock_chat_stream, mock_fts):
        """合成中途失败：只推送已合成的部分，不整句重播；后续句子降级本地语音。"""

        class FakeCommFail:
            def __init__(self, *args, **kwargs):
                pass

            async def stream(self):
                yield {"type": "audio", "data": b"X" * 3000}
                raise ConnectionError("boom")

        with (
            mock.patch("app.services.tts.edge_tts", SimpleNamespace(Communicate=FakeCommFail)),
            mock.patch("app.services.tts.config.VOICE_TTS", "edge"),
            # 本测试只关心"中途失败不重播"，禁用熔断避免开场白失败提前打开熔断
            mock.patch("app.services.tts._circuit_open", return_value=False),
            mock.patch("app.services.tts.TTS_MAX_CONCURRENCY", 1),  # 串行：保证断言确定
            TestClient(app) as client,
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            _recv_until_done(ws)  # 消化开场白（同样中途失败，先清空）
            ws.send_text(json.dumps({"type": "text", "content": "你好"}, ensure_ascii=False))
            starts = ends = tts_errors = total_bytes = 0
            while True:
                m = json.loads(ws.receive_text())
                t = m["type"]
                if t == "audio_start":
                    starts += 1
                elif t == "audio":
                    total_bytes += len(base64.b64decode(m["data"]))
                elif t == "audio_end":
                    ends += 1
                elif t == "tts_error":
                    tts_errors += 1
                elif t == "done":
                    break
        self.assertEqual(starts, 2)  # 第一段部分音频 + 第二段降级
        self.assertEqual(ends, 1)  # 只有第一句推送了实际音频
        self.assertEqual(total_bytes, 3000)  # 只推已合成的部分，不整句重播
        self.assertEqual(tts_errors, 1)  # 第二句走降级

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch(
        "app.agent.llm.chat_stream", return_value=iter(["第一句。第二句。第三句。第四句。"])
    )
    def test_produce_merges_sentences_into_fewer_tts_calls(self, mock_chat_stream, mock_fts):
        """多句合并合成：短回复只发起 1 次在线合成（减少连接数、保留句间语气）。"""
        calls: list[str] = []

        async def fake_synth(state, sentence):
            calls.append(sentence)
            return TtsUnit(text=sentence, blocks=[b"AUDIO"], ok=True)

        with (
            mock.patch("app.services.tts.synth", side_effect=fake_synth),
            TestClient(app) as client,
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            _recv_until_done(ws)  # 消化开场白
            calls.clear()
            ws.send_text(json.dumps({"type": "text", "content": "你好"}, ensure_ascii=False))
            while True:
                if json.loads(ws.receive_text())["type"] == "done":
                    break
        self.assertEqual(len(calls), 1, "四句话应合并为一次合成")
        self.assertIn("第一句", calls[0])
        self.assertIn("第四句", calls[0])

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch(
        "app.agent.llm.chat_stream", return_value=iter(["第一句。第二句。第三句。第四句。"])
    )
    def test_produce_splits_long_text_into_chunks(self, mock_chat_stream, mock_fts):
        """长回复按阈值分批合成（阈值调小模拟长文本），句子不丢失。"""
        calls: list[str] = []

        async def fake_synth(state, sentence):
            calls.append(sentence)
            return TtsUnit(text=sentence, blocks=[b"AUDIO"], ok=True)

        with (
            mock.patch("app.services.tts.synth", side_effect=fake_synth),
            mock.patch("app.services.tts.TTS_FIRST_CHARS", 1),
            mock.patch("app.services.tts.TTS_CHUNK_CHARS", 1),
            TestClient(app) as client,
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            _recv_until_done(ws)  # 消化开场白
            calls.clear()
            ws.send_text(json.dumps({"type": "text", "content": "你好"}, ensure_ascii=False))
            while True:
                if json.loads(ws.receive_text())["type"] == "done":
                    break
        self.assertEqual(len(calls), 4, "四句话应各自成块")
        joined = "".join(calls)
        for s in ("第一句。", "第二句。", "第三句。", "第四句。"):
            self.assertIn(s, joined)

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch(
        "app.agent.llm.chat_stream", return_value=iter(["第一句。第二句。第三句。第四句。"])
    )
    def test_synth_runs_concurrently(self, mock_chat_stream, mock_fts):
        """bug #12 回归：合成阶段必须并发（受 sem 限流），不能被推送顺序锁串行化。

        修复前 _await_turn 位于合成之前，同一时刻只有 1 段在合成（sem 形同虚设）；
        修复后 sem=3 时应有 ≥2 段同时在合成。
        """
        in_flight = 0
        max_in_flight = 0

        async def counting_synth(state, sentence):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.05)  # 模拟在线合成的真实网络耗时
            finally:
                in_flight -= 1
            return TtsUnit(text=sentence, blocks=[b"AUDIO"], ok=True)

        with (
            mock.patch("app.services.tts.synth", side_effect=counting_synth),
            mock.patch("app.services.tts.TTS_FIRST_CHARS", 1),
            mock.patch("app.services.tts.TTS_CHUNK_CHARS", 1),
            TestClient(app) as client,
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            _recv_until_done(ws)  # 消化开场白（单段，不参与统计）
            max_in_flight = 0
            ws.send_text(json.dumps({"type": "text", "content": "你好"}, ensure_ascii=False))
            while True:
                if json.loads(ws.receive_text())["type"] == "done":
                    break
        self.assertGreaterEqual(max_in_flight, 2, "四段文本应有多段同时在合成（sem=3）")

    def test_circuit_breaker_skips_online(self):
        """熔断期间 _synthesize 直接降级，不再调用 edge-tts（避免每次干等）；状态按连接隔离。"""

        class FakeWS:
            def __init__(self):
                self.msgs = []

            async def send_text(self, s):
                self.msgs.append(json.loads(s))

        called = []

        class FakeComm:
            def __init__(self, *args, **kwargs):
                called.append(1)

            async def stream(self):
                yield {"type": "audio", "data": b"X"}

        async def run():
            with (
                mock.patch("app.services.tts.edge_tts", SimpleNamespace(Communicate=FakeComm)),
                mock.patch("app.services.tts.config.VOICE_TTS", "edge"),
            ):
                ws = FakeWS()
                # 该连接熔断已打开
                ok = await _synthesize(
                    ws,
                    TtsState(sid=0, fails=3, open_until=time.monotonic() + 60),
                    "你好",
                )
            return ok, ws.msgs, called

        ok, msgs, called = asyncio.run(run())
        self.assertFalse(ok)
        self.assertEqual([m["type"] for m in msgs], ["audio_start", "tts_error"])
        self.assertEqual(called, [], "熔断期间不应发起在线合成")

    def test_circuit_breaker_per_connection_state(self):
        """熔断状态互相隔离：一个连接熔断不影响另一个连接的在线合成。"""
        called = []

        class FakeWS:
            def __init__(self):
                self.msgs = []

            async def send_text(self, s):
                self.msgs.append(json.loads(s))

        class FakeComm:
            def __init__(self, *args, **kwargs):
                called.append(1)

            async def stream(self):
                yield {"type": "audio", "data": b"X" * 3000}

        ws = FakeWS()
        with (
            mock.patch("app.services.tts.edge_tts", SimpleNamespace(Communicate=FakeComm)),
            mock.patch("app.services.tts.config.VOICE_TTS", "edge"),
        ):
            # 连接 A 熔断打开
            ok_a = asyncio.run(
                _synthesize(
                    ws,
                    TtsState(sid=0, fails=3, open_until=time.monotonic() + 60),
                    "你好",
                )
            )
            # 连接 B 状态正常，仍走在线合成
            ok_b = asyncio.run(_synthesize(ws, TtsState(), "你好"))
        self.assertFalse(ok_a)
        self.assertTrue(ok_b)
        self.assertEqual(len(called), 1, "只有正常状态的连接发起在线合成")

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch("app.agent.llm.chat_stream", return_value=iter(["CosyVoice 测试。"]))
    def test_cosyvoice_synthesize_sends_unit(self, mock_chat_stream, mock_fts):
        """VOICE_TTS=cosyvoice：整段音频作为一个播放单元推送。"""
        FAKE_AUDIO = b"\x00\x01\x02fake-cosy-audio"

        with (
            mock.patch("app.services.tts.config.VOICE_TTS", "cosyvoice"),
            mock.patch("app.services.tts.config.DASHSCOPE_API_KEY", "sk-test"),
            mock.patch("app.services.tts._cosyvoice_synthesize", return_value=FAKE_AUDIO),
            TestClient(app) as client,
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            _recv_until_done(ws)  # 消化开场白（同样走 CosyVoice 路径）
            ws.send_text(json.dumps({"type": "text", "content": "你好"}, ensure_ascii=False))
            starts = ends = tts_errors = 0
            audio_bytes = b""
            while True:
                m = json.loads(ws.receive_text())
                t = m["type"]
                if t == "audio_start":
                    starts += 1
                elif t == "audio":
                    audio_bytes += base64.b64decode(m["data"])
                elif t == "audio_end":
                    ends += 1
                elif t == "tts_error":
                    tts_errors += 1
                elif t == "done":
                    break
        self.assertEqual(starts, 1)
        self.assertEqual(ends, 1)
        self.assertEqual(audio_bytes, FAKE_AUDIO)
        self.assertEqual(tts_errors, 0)

    def test_cosyvoice_failure_locks_reply_to_edge(self):
        """CosyVoice 失败后本回复锁定 edge-tts：不再反复请求 CosyVoice，保证同回复音色一致。"""

        class FakeWS:
            def __init__(self):
                self.msgs = []

            async def send_text(self, s):
                self.msgs.append(json.loads(s))

        calls = []

        async def flaky(text):
            calls.append(text)
            return None

        ws = FakeWS()
        with (
            mock.patch("app.services.tts.config.VOICE_TTS", "cosyvoice"),
            mock.patch("app.services.tts.config.DASHSCOPE_API_KEY", "sk-test"),
            mock.patch("app.services.tts.edge_tts", None),  # 回退路径确定失败，才能测重试
            mock.patch("app.services.tts._cosyvoice_synthesize", side_effect=flaky),
        ):
            ok = asyncio.run(_synthesize(ws, TtsState(), "你好"))
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1, "失败后不应再次请求 CosyVoice（锁定 edge-tts）")
        self.assertEqual([m["type"] for m in ws.msgs], ["audio_start", "tts_error"])

        # 同一回复的下一段：直接走 edge-tts，不再尝试 CosyVoice
        ws2 = FakeWS()
        with (
            mock.patch("app.services.tts.config.VOICE_TTS", "cosyvoice"),
            mock.patch("app.services.tts.config.DASHSCOPE_API_KEY", "sk-test"),
            mock.patch("app.services.tts.edge_tts", None),
            mock.patch("app.services.tts._cosyvoice_synthesize", side_effect=flaky),
        ):
            ok2 = asyncio.run(_synthesize(ws2, TtsState(voice="edge"), "第二句"))
        self.assertFalse(ok2)
        self.assertEqual(len(calls), 1, "锁定后不应再请求 CosyVoice")

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch("app.agent.llm.chat_stream", return_value=iter(["标准", "答案"]))
    def test_greeting_synthesized_as_single_unit(self, mock_chat_stream, mock_fts):
        """开场白整段一次合成：只有一个音色，不会出现"两个声音"。"""
        import app.voice_ws as voice_ws

        calls: list[str] = []

        async def recording_synth(state, sentence):
            calls.append(sentence)
            return TtsUnit(text=sentence, blocks=[b"AUDIO"], ok=True)

        with (
            TestClient(app) as client,
            mock.patch("app.services.tts.synth", side_effect=recording_synth),
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            _recv_until_done(ws)
        self.assertEqual(len(calls), 1, "开场白应整段一次合成")
        self.assertEqual(calls[0], voice_ws.prompts.VOICE_GREETING.strip())

    def test_cosyvoice_no_key_fast_fail(self):
        """VOICE_TTS=cosyvoice 但缺少 DASHSCOPE_API_KEY：直接降级，不发网络请求。"""

        class FakeWS:
            def __init__(self):
                self.msgs = []

            async def send_text(self, s):
                self.msgs.append(json.loads(s))

        called = []

        async def never_called(text):
            called.append(text)
            return b"X"

        ws = FakeWS()
        with (
            mock.patch("app.services.tts.config.VOICE_TTS", "cosyvoice"),
            mock.patch("app.services.tts.config.DASHSCOPE_API_KEY", ""),
            mock.patch("app.services.tts._cosyvoice_synthesize", side_effect=never_called),
        ):
            ok = asyncio.run(_synthesize(ws, TtsState(), "你好"))
        self.assertFalse(ok)
        self.assertEqual(called, [], "缺少 Key 时不应发起请求")
        self.assertEqual([m["type"] for m in ws.msgs], ["audio_start", "tts_error"])

    @mock.patch(
        "app.voice_ws.voice_store.load_custom_interview",
        return_value={
            "job_title": "Python 后端工程师",
            "jd": "熟悉 Django / Redis",
            "questions": ["定制题一：解释 GIL", "定制题二：缓存设计"],
        },
    )
    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    def test_custom_interview_voice_flow(self, mock_fts, mock_load):
        """已准备定制面试时接通：播报定制开场白，首句回答后直接出定制题。"""
        seen: dict[str, str] = {}

        def fake_stream(messages, **kw):
            last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
            seen["last_user"] = last_user
            yield "（mock）定制题一：解释 GIL"

        with (
            mock.patch("app.agent.llm.chat_stream", side_effect=fake_stream),
            TestClient(app) as client,
            mock.patch("app.services.tts.synth", side_effect=_fake_synth),
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            greeting_deltas, *_ = _recv_until_done(ws)
            ws.send_text(json.dumps({"type": "text", "content": "我叫张三"}, ensure_ascii=False))
            deltas, audio_started, audio_ended, _ = _recv_until_done(ws)
        greeting_text = "".join(greeting_deltas)
        self.assertIn("定制面试", greeting_text)
        self.assertIn("Python 后端工程师", greeting_text)
        self.assertTrue(audio_started and audio_ended, "定制面试应推送音频")
        self.assertIn("定制题一", seen.get("last_user", ""), "应使用已保存的定制题目")

    @mock.patch(
        "app.routers.custom.voice_store.load_custom_interview",
        return_value={"job_title": "后端开发", "questions": ["Q1"]},
    )
    def test_custom_status_endpoint(self, mock_load):
        """语音页通过该接口判断该用户是否已准备定制面试（需登录）。"""
        with TestClient(app) as client:
            resp = client.get(
                "/api/custom/status",
                headers={"Authorization": f"Bearer {self.token}"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ready": True, "job_title": "后端开发"})

    @mock.patch("app.routers.custom.voice_store.load_custom_interview", return_value=None)
    def test_custom_status_endpoint_empty(self, mock_load):
        with TestClient(app) as client:
            resp = client.get(
                "/api/custom/status",
                headers={"Authorization": f"Bearer {self.token}"},
            )
        self.assertEqual(resp.json(), {"ready": False, "job_title": ""})

    def test_cosyvoice_request_downloads_url(self):
        """非流式 CosyVoice 响应返回音频 URL，需二次下载后返回字节。"""

        class FakeResp:
            def __init__(self, json_data=None, content=None):
                self._json = json_data
                self.content = content

            def raise_for_status(self):
                pass

            def json(self):
                return self._json

        with (
            mock.patch("app.services.tts.config.DASHSCOPE_API_KEY", "sk-test"),
            mock.patch(
                "app.services.tts.requests.post",
                return_value=FakeResp(
                    json_data={"output": {"audio": {"url": "http://audio.example/x.mp3"}}}
                ),
            ),
            mock.patch("app.services.tts.requests.get", return_value=FakeResp(content=b"MP3DATA")),
        ):
            data = asyncio.run(_cosyvoice_synthesize("你好"))
        self.assertEqual(data, b"MP3DATA")

    def test_cosyvoice_request_accepts_base64_data(self):
        """若响应直接带 base64 音频，无需二次下载。"""

        class FakeResp:
            def __init__(self, json_data=None):
                self._json = json_data
                self.content = None

            def raise_for_status(self):
                pass

            def json(self):
                return self._json

        with (
            mock.patch("app.services.tts.config.DASHSCOPE_API_KEY", "sk-test"),
            mock.patch(
                "app.services.tts.requests.post",
                return_value=FakeResp(
                    json_data={
                        "output": {
                            "audio": {
                                "data": base64.b64encode(b"INLINE").decode("ascii"),
                                "url": "",
                            }
                        }
                    }
                ),
            ),
            mock.patch("app.services.tts.requests.get", side_effect=AssertionError("不应下载")),
        ):
            data = asyncio.run(_cosyvoice_synthesize("你好"))
        self.assertEqual(data, b"INLINE")

    def test_health(self):
        with TestClient(app) as client:
            resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_health_returns_503_when_schema_missing(self):
        """就绪探针：数据库文件存在但未初始化 schema 时应返回 503。"""
        from fastapi import FastAPI

        tmpdir = tempfile.TemporaryDirectory()
        empty_db = Path(tmpdir.name) / "empty.db"
        sqlite3.connect(empty_db).close()  # 仅创建空库文件，无任何表
        probe = FastAPI()
        probe.add_api_route("/health", health)
        try:
            with (
                mock.patch.object(db, "get_conn", lambda: sqlite3.connect(empty_db)),
                TestClient(probe) as client,
            ):
                resp = client.get("/health")
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(resp.json()["detail"], "database schema missing")
        finally:
            tmpdir.cleanup()

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch("app.agent.llm.chat_stream", return_value=iter(["标准", "答案"]))
    def test_asr_start_failure_auto_retries(self, mock_chat_stream, mock_fts):
        """ASR 启动失败后由服务端监督任务自动重连（指数退避），无需前端反复重发。"""
        # 记录每个实例收到的 sample_rate，并让前两次 start 失败、第三次成功
        start_calls: list[tuple] = []
        inst_id = {"n": 0}

        class FakeRec:
            def __init__(self, *args, **kwargs):
                inst_id["n"] += 1
                self.id = inst_id["n"]
                self.sample_rate = kwargs.get("sample_rate")
                start_calls.append((self.id, self.sample_rate))
                self.started = False

            def start(self):
                # 前两次失败，第三次成功
                if self.id <= 2:
                    return False
                self.started = True
                return True

            def send_audio_frame(self, data):
                pass

            def stop(self):
                self.started = False

        class FakeASR:
            instances = []

            def __init__(self, loop, on_sentence, on_partial=None, on_error=None, sample_rate=None):
                self.loop = loop
                self.on_sentence = on_sentence
                self.on_partial = on_partial
                self.on_error = on_error
                self.sample_rate = sample_rate
                self.rec = FakeRec(sample_rate=sample_rate)
                FakeASR.instances.append(self)

            def start(self):
                return self.rec.start()

            def send(self, data):
                self.rec.send_audio_frame(data)

            def stop(self):
                self.rec.stop()

        with (
            TestClient(app) as client,
            mock.patch("app.services.tts.synth", side_effect=_fake_synth),
            mock.patch("app.voice_ws.DashScopeASR", FakeASR),
            mock.patch("app.voice_ws.ASR_RETRY_DELAY", 0.05),
            # 必须显式声明"识别服务已配置"：未配置 Key 时服务端判定为配置类故障、
            # 直接终态返回且不启动监督重连（bug #23），这些用例就会永远等不到
            # asr_ready。不能依赖真实 DASHSCOPE_API_KEY——没有 .env 的 CI 环境
            # 会让 ws.receive_text() 无限阻塞，把 job 挂到超时（曾挂满 6 小时）。
            mock.patch("app.core.config.DASHSCOPE_API_KEY", "sk-test"),
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            _recv_until_done(ws)  # 消化开场白
            # asr_start：处理器首次启动失败 → asr_error，监督任务接管
            ws.send_text(json.dumps({"type": "asr_start", "sample_rate": 48000}))
            self.assertEqual(json.loads(ws.receive_text())["type"], "asr_error")
            # 监督任务静默重试（失败不重复打扰前端），成功后才回传 asr_ready
            self.assertEqual(json.loads(ws.receive_text())["type"], "asr_ready")
        # 每个实例都拿到前端上报的采样率
        self.assertEqual(len(FakeASR.instances), 3, "失败后应重建实例而非复用")
        for inst in FakeASR.instances:
            self.assertEqual(inst.sample_rate, 48000)

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch("app.agent.llm.chat_stream", return_value=iter(["标准", "答案"]))
    def test_asr_midstream_error_auto_reconnects(self, mock_chat_stream, mock_fts):
        """ASR 识别流中途断开（如心跳 PONG 超时）：服务端自动重连并回传 asr_ready。"""
        frames = {"n": 0}

        class FakeASR:
            instances = []

            def __init__(self, loop, on_sentence, on_partial=None, on_error=None, sample_rate=None):
                self.on_error = on_error
                self.sample_rate = sample_rate
                FakeASR.instances.append(self)

            def start(self):
                return True

            def send(self, data):
                frames["n"] += 1
                if frames["n"] == 1:
                    # 模拟 SDK 在工作线程回调：把错误桥接到服务端事件循环
                    asyncio.get_running_loop().create_task(self.on_error(400, "stream closed"))

            def stop(self):
                pass

        with (
            TestClient(app) as client,
            mock.patch("app.services.tts.synth", side_effect=_fake_synth),
            mock.patch("app.voice_ws.DashScopeASR", FakeASR),
            mock.patch("app.voice_ws.ASR_RETRY_DELAY", 0.05),
            # 必须显式声明"识别服务已配置"：未配置 Key 时服务端判定为配置类故障、
            # 直接终态返回且不启动监督重连（bug #23），这些用例就会永远等不到
            # asr_ready。不能依赖真实 DASHSCOPE_API_KEY——没有 .env 的 CI 环境
            # 会让 ws.receive_text() 无限阻塞，把 job 挂到超时（曾挂满 6 小时）。
            mock.patch("app.core.config.DASHSCOPE_API_KEY", "sk-test"),
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            _recv_until_done(ws)  # 消化开场白
            ws.send_text(json.dumps({"type": "asr_start", "sample_rate": 16000}))
            self.assertEqual(json.loads(ws.receive_text())["type"], "asr_ready")
            # 第一帧音频（二进制 PCM）触发识别流中断 → asr_error
            ws.send_bytes(b"\x00" * 64)
            self.assertEqual(json.loads(ws.receive_text())["type"], "asr_error")
            # 监督任务自动重连（无需前端重发 asr_start）
            self.assertEqual(json.loads(ws.receive_text())["type"], "asr_ready")
        self.assertEqual(len(FakeASR.instances), 2, "识别中断后应重建实例")

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    def test_session_reused_across_reconnects(self, mock_fts):
        """挂断重连后延续上一轮会话：开场白改为"欢迎回来"，上下文包含历史消息。"""
        seen = {"user_msgs": 0}

        def fake_stream(messages, **kw):
            seen["user_msgs"] = sum(1 for m in messages if m["role"] == "user")
            yield "（mock）回复。"

        with (
            TestClient(app) as client,
            mock.patch("app.agent.llm.chat_stream", side_effect=fake_stream),
            mock.patch("app.services.tts.synth", side_effect=_fake_synth),
        ):
            with client.websocket_connect(self._ws_url(client)) as ws:
                greeting1, *_ = _recv_until_done(ws)
                ws.send_text(
                    json.dumps({"type": "text", "content": "第一个问题"}, ensure_ascii=False)
                )
                _recv_until_done(ws)
            self.assertEqual(seen["user_msgs"], 1)
            with client.websocket_connect(self._ws_url(client)) as ws2:
                greeting2, *_ = _recv_until_done(ws2)
                ws2.send_text(
                    json.dumps({"type": "text", "content": "第二个问题"}, ensure_ascii=False)
                )
                _recv_until_done(ws2)
        self.assertIn("欢迎回来", "".join(greeting2), "重连应沿用上一轮会话")
        self.assertNotIn("欢迎回来", "".join(greeting1))
        self.assertEqual(seen["user_msgs"], 2, "第二次回复的上下文应包含第一轮的用户消息")

    def test_spa_served_at_root(self):
        """统一服务在 / 托管 Vue3 SPA；/voice 走 history 回退到 index.html；语音配置接口可用。"""
        dist_index = Path(__file__).resolve().parents[1] / "frontend" / "dist" / "index.html"
        if not dist_index.exists():
            self.skipTest("前端未构建，跳过 SPA 托管检查")
        with TestClient(app) as client:
            root = client.get("/")
            voice = client.get("/voice")
            cfg = client.get("/api/config/voice")
        self.assertEqual(root.status_code, 200)
        self.assertIn("text/html", root.headers["content-type"])
        self.assertIn('id="app"', root.text)
        self.assertEqual(voice.status_code, 200)
        self.assertIn('id="app"', voice.text)
        self.assertEqual(cfg.status_code, 200)
        self.assertIn("vad_threshold", cfg.json())

    def test_ws_rejects_missing_token(self):
        """多用户：语音 WS 无有效令牌应被拒绝（4401），不会进入通话。"""
        from starlette.websockets import WebSocketDisconnect

        with (
            TestClient(app) as client,
            self.assertRaises(WebSocketDisconnect),
            client.websocket_connect("/ws/voice") as ws,
        ):
            ws.receive_text()

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    def test_ws_text_rate_limited(self, mock_fts):
        """WS text 消息按用户限流：超限后返回结构化 rate_limit 错误，不再调用 LLM。"""
        from app.core.ratelimit import reset_rate_limits

        calls = {"n": 0}

        def fake_stream(messages, **kw):
            calls["n"] += 1
            yield "回复。"

        with (
            TestClient(app) as client,
            mock.patch("app.agent.llm.chat_stream", side_effect=fake_stream),
            mock.patch("app.services.tts.synth", side_effect=_fake_synth),
            mock.patch("app.core.config.VOICE_TEXT_RATE_LIMIT", 2),
            mock.patch("app.core.config.VOICE_TEXT_RATE_WINDOW", 60),
        ):
            reset_rate_limits()
            with client.websocket_connect(self._ws_url(client)) as ws:
                _recv_until_done(ws)  # 消化开场白（不计入 text 限流）
                for _ in range(2):
                    ws.send_text(
                        json.dumps({"type": "text", "content": "问题"}, ensure_ascii=False)
                    )
                    _recv_until_done(ws)
                # 第 3 条：超限 → 结构化错误，不再触发 LLM
                ws.send_text(json.dumps({"type": "text", "content": "问题"}, ensure_ascii=False))
                err = None
                while True:
                    m = json.loads(ws.receive_text())
                    if m["type"] == "error":
                        err = m
                        break
        self.assertEqual(err["code"], "rate_limit")
        self.assertEqual(calls["n"], 2, "超限后不应再调用 LLM")

    def test_second_connection_kicks_first(self):
        """同账号第二个连接接通时踢掉旧连接（close 4409），防会话双写分叉。"""
        from starlette.websockets import WebSocketDisconnect

        # 嵌套 with 是必要的：ws1 必须保持打开，才能验证 ws2 接通时被踢
        with TestClient(app) as client:  # noqa: SIM117
            with client.websocket_connect(self._ws_url(client)) as ws1:
                _recv_until_done(ws1)  # 消化开场白
                with client.websocket_connect(self._ws_url(client)) as ws2:
                    _recv_until_done(ws2)  # 新连接正常接通
                # 旧连接应已被服务端关闭（4409 互踢）
                with self.assertRaises(WebSocketDisconnect):
                    while True:
                        ws1.receive_text()

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    def test_stop_cancels_generation(self, mock_fts):
        """barge-in：客户端发 stop 后，未完成的回复任务被取消并收到 cancelled（非 done）。"""
        import threading

        produced = threading.Event()
        release = threading.Event()

        def slow_stream(messages, **kw):
            yield "第一段。"
            produced.set()
            release.wait(10)  # 模拟长时间生成，等待被取消
            yield "不应到达。"

        with (
            TestClient(app) as client,
            mock.patch("app.agent.llm.chat_stream", side_effect=slow_stream),
            mock.patch("app.services.tts.synth", side_effect=_fake_synth),
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            try:
                _recv_until_done(ws)  # 消化开场白
                ws.send_text(json.dumps({"type": "text", "content": "你好"}, ensure_ascii=False))
                # 收到第一条 delta（生成已开始）后发 stop
                while json.loads(ws.receive_text())["type"] != "delta":
                    pass
                self.assertTrue(produced.wait(2))
                ws.send_text(json.dumps({"type": "stop"}))
                types = []
                while True:
                    t = json.loads(ws.receive_text())["type"]
                    types.append(t)
                    if t == "cancelled":
                        break
                    self.assertNotEqual(t, "done", "stop 后不应收到 done")
            finally:
                release.set()  # 释放被取消的生成线程，避免悬挂
        self.assertIn("cancelled", types)

    @mock.patch("app.agent.coach.db.fts_search", return_value=[])
    @mock.patch("app.agent.llm.chat_stream", return_value=iter([LONG_SPLIT_TEXT]))
    def test_audio_push_order_follows_task_order(self, mock_chat_stream, mock_fts):
        """并发合成的音频段按文本顺序推送：慢的第一段不能被快的第一段抢占 sid。

        回归测试：修复前 sid 在 flush 时才分配，谁先合成完谁先拿到 sid，
        浏览器按 sid 排序播放时内容会乱序（语音与字幕不同步）。
        """
        synth_order: list[str] = []

        async def slow_first(state, sentence):
            synth_order.append(sentence)
            if len(synth_order) == 1:
                await asyncio.sleep(0.3)  # 第一段故意慢，模拟网络抖动
            return TtsUnit(text=sentence, blocks=[sentence.encode("utf-8")], ok=True)

        pushed: list[str] = []

        with (
            TestClient(app) as client,
            mock.patch("app.services.tts.synth", side_effect=slow_first),
            mock.patch("app.services.tts.TTS_FIRST_CHARS", 1),
            mock.patch("app.services.tts.TTS_CHUNK_CHARS", 1),
            client.websocket_connect(self._ws_url(client)) as ws,
        ):
            _recv_until_done(ws)  # 消化开场白
            synth_order.clear()
            ws.send_text(json.dumps({"type": "text", "content": "你好"}, ensure_ascii=False))
            while True:
                m = json.loads(ws.receive_text())
                if m["type"] == "audio_start":
                    pushed.append(m["text"])
                elif m["type"] == "done":
                    break
        self.assertEqual(len(pushed), 4)
        self.assertEqual(pushed, synth_order, "推送顺序必须与文本顺序一致（sid 顺序即内容顺序）")


if __name__ == "__main__":
    unittest.main()
