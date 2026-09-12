"""BUG 检测报告第一批修复的回归测试。

覆盖问题编号（见 BUG 检测报告）：
- #1  题库 tags 查询参数契约（axios 序列化修复后的后端回归）
- #8  SQLite WAL 模式
- #11 /api/session/history 的 limit 越界（-1 拉全表）
- #18 LLM 重试范围收紧（4xx 不再重试）
- #2  javaguide 乱码存量修复工具（repair_text 判定与还原）
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.agent import llm
from app.agent.coach import (
    InterviewSession,
    _extract_dimensions,
    _extract_weak_points,
)
from app.core import config, db
from app.core.ratelimit import reset_rate_limits
from app.crawler.fix_mojibake import repair_text
from app.main import app
from app.stores import auth
from fastapi.testclient import TestClient
from openai import APIStatusError, RateLimitError


def _isolate_rate_limit():
    """注册类测试与既有用例共享 /api/auth 限流桶（5 次/60s），前后清空防串扰。"""
    reset_rate_limits()


def _api_error(status: int) -> APIStatusError:
    """构造带 status_code 的 APIStatusError（openai SDK 签名）。"""
    resp = SimpleNamespace(status_code=status, headers={}, request=SimpleNamespace())
    return APIStatusError(f"http {status}", response=resp, body=None)


class TokenCleanupTests(unittest.TestCase):
    """bug #9：签发令牌时惰性清理过期行，auth_tokens 不再只增不减。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "token.db")
        self._patch.start()
        db.init_db()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_expired_rows_purged_on_issue(self):
        from datetime import datetime, timedelta, timezone

        uid = db.create_user("tcu", auth.hash_password("pass123456"))
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        db.create_auth_token(uid, "expired-tok", past)
        db.create_auth_token(uid, "live-tok", future)
        # 再签发一次：应顺带清理 expired-tok，保留 live-tok
        db.create_auth_token(uid, "new-tok", future)
        conn = db.get_conn()
        try:
            # bug #25 后库内只存哈希（token_hash），明文不落库；按行数断言清理效果
            tokens = {
                r["token_hash"]
                for r in conn.execute("SELECT token_hash FROM auth_tokens WHERE user_id=?", (uid,))
            }
        finally:
            conn.close()
        self.assertEqual(len(tokens), 2)  # expired-tok 被清理，live-tok/new-tok 保留
        self.assertTrue(all(len(t) == 64 for t in tokens), "落库值应为 64 位哈希")


class ChatRateLimitAndLockTests(unittest.TestCase):
    """bug #10（chat 限流）/#21（并发锁拒绝第二流）/#13（错误不回显）。"""

    def setUp(self):
        _isolate_rate_limit()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "chat.db")
        self._patch.start()
        db.init_db()
        self.client = TestClient(app)
        r = self.client.post(
            "/api/auth/register", json={"username": "chatuser", "password": "pass123456"}
        )
        self.hdr = {"Authorization": f"Bearer {r.json()['token']}"}

    def tearDown(self):
        self._patch.stop()
        _isolate_rate_limit()
        self._tmpdir.cleanup()

    def test_chat_rate_limited(self):
        with (
            mock.patch("app.agent.llm.chat_stream", return_value=iter(["回复。"])),
            mock.patch("app.services.tts.synth"),
            mock.patch("app.core.config.VOICE_TEXT_RATE_LIMIT", 1),
        ):
            ok = self.client.post("/api/chat", json={"message": "你好"}, headers=self.hdr)
            self.assertEqual(ok.status_code, 200)
            blocked = self.client.post("/api/chat", json={"message": "再来"}, headers=self.hdr)
        self.assertEqual(blocked.status_code, 429)

    def test_second_stream_rejected_while_first_active(self):
        """bug #21：锁被占用时同用户第二条流直接 429。"""
        from app.routers import session as session_router

        uid = db.get_user_by_username("chatuser")["id"]
        lock = session_router._get_chat_lock(uid)
        # 白盒置位：模拟"上一条流正在生成"（asyncio.Lock 绑定事件循环，
        # 跨线程真实 acquire 不可行，这里只验证端点的 locked 分支）
        lock._locked = True
        try:
            with mock.patch("app.agent.llm.chat_stream", return_value=iter(["回复。"])):
                blocked = self.client.post("/api/chat", json={"message": "并发"}, headers=self.hdr)
        finally:
            lock._locked = False
        self.assertEqual(blocked.status_code, 429)

    def test_error_event_hides_internal_details(self):
        """bug #13：SSE error 事件不含内部异常细节，只发固定文案。"""
        with (
            mock.patch(
                "app.agent.llm.chat_stream",
                side_effect=RuntimeError("secret-db-path-internal"),
            ),
            self.client.stream(
                "POST", "/api/chat", json={"message": "你好"}, headers=self.hdr
            ) as resp,
        ):
            body = "".join(
                chunk for chunk in resp.iter_text() if "error" in chunk or "delta" in chunk
            )
        self.assertIn("生成回复时出错", body)
        self.assertNotIn("secret-db-path-internal", body)


class SecurityHeadersTests(unittest.TestCase):
    """bug #22：统一安全响应头。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "hdr.db")
        self._patch.start()
        db.init_db()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_headers_present(self):
        with TestClient(app) as client:
            resp = client.get("/health")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")


class QuestionRowColumnsTests(unittest.TestCase):
    """bug #17：pick_random_question 返回 source_id/answer，参考答案兜底可达。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "pick.db")
        self._patch.start()
        db.init_db()
        db.upsert_question(
            source="mianshiya",
            title="什么是 GIL",
            source_id="mianshiya:123",
            answer=None,
            tags=["Python"],
        )

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_columns_present(self):
        rows = db.pick_random_question(tags=["Python"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_id"], "mianshiya:123")
        self.assertIsNone(rows[0]["answer"])


class ParserHeadingTests(unittest.TestCase):
    """bug #16：解析器标题判定收紧后的回归。"""

    REPORT = (
        "## 【总分】85/100\n"
        "- 技术正确性：40\n"
        "- 表达清晰度：建议加强 30\n"
        "## 知识薄弱点\n"
        "- 需要改进对索引薄弱点的理解\n"
        "## 改进建议\n"
        "- 每天刷 3 道题\n"
    )

    def test_dimensions_survive_keyword_in_item(self):
        dims = _extract_dimensions(self.REPORT)
        labels = [d["label"] for d in dims]
        # 核心回归：维度行含"建议"字样不再提前截断，两条维度都被抽出
        self.assertEqual(labels, ["技术正确性", "表达清晰度：建议加强"])
        self.assertEqual(dims[1]["score"], 30)

    def test_weak_points_keep_keyword_items(self):
        weak = _extract_weak_points(self.REPORT)
        self.assertIn("索引薄弱点", weak)


class P3TailTests(unittest.TestCase):
    """P3 尾巴批次：#24/#25/#26/#27/#28/#31。"""

    def setUp(self):
        _isolate_rate_limit()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "p3.db")
        self._patch.start()
        db.init_db()
        self.client = TestClient(app)
        r = self.client.post(
            "/api/auth/register",
            json={"username": "p3user", "password": "pass123456", "nickname": "老昵称"},
        )
        self.token = r.json()["token"]
        self.hdr = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        self._patch.stop()
        _isolate_rate_limit()
        self._tmpdir.cleanup()

    def test_foreign_keys_on_and_ghost_favorite_404(self):
        """bug #24：FK 开启 + 收藏不存在的题目返回 404，不再产生幽灵收藏。"""
        conn = db.get_conn()
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            conn.close()
        resp = self.client.post("/api/favorites/999999999", headers=self.hdr)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.client.get("/api/favorites", headers=self.hdr).json()["ids"], [])

    def test_login_body_length_capped(self):
        """bug #25a：登录用户名/密码超长 422。"""
        resp = self.client.post(
            "/api/auth/login", json={"username": "x" * 100, "password": "y" * 200}
        )
        self.assertEqual(resp.status_code, 422)

    def test_add_question_tags_sanitized(self):
        """bug #25b：含逗号/空白的标签被清洗，不污染标签体系。"""
        self.client.post(
            "/api/questions",
            json={"title": "测试题", "tags": ["  Redis ", "a,b", "  ", "缓存"]},
            headers=self.hdr,
        )
        names = {
            t["name"]
            for t in self.client.get("/api/questions/meta", headers=self.hdr).json()["tags"]
        }
        self.assertIn("Redis", names)
        self.assertIn("缓存", names)
        self.assertNotIn("a,b", names)

    def test_nickname_can_be_cleared(self):
        """bug #26：显式传空昵称可清空；不传 nickname 字段则保持不变。"""
        me = self.client.put(
            "/api/auth/me", json={"nickname": "", "persona": ""}, headers=self.hdr
        ).json()
        self.assertNotEqual(me.get("nickname"), "老昵称")
        # 不传 nickname：persona 更新、昵称保持清空后的状态
        me2 = self.client.put("/api/auth/me", json={"persona": "严肃"}, headers=self.hdr).json()
        self.assertNotEqual(me2.get("nickname"), "老昵称")
        self.assertEqual(me2.get("persona"), "严肃")

    def test_finished_session_hint_no_report_dup(self):
        """bug #27：报告出来后再发言，展示与持久化不再重复/覆盖报告。"""
        import app.stores.session_store as session_store

        uid = 616161
        s = InterviewSession("mock", user_id=uid)
        s.turn = "report"
        s.finished = True
        s.answers = [{"stage": "基础", "title": "Q1", "answer": "A1"}]
        s.messages = [{"role": "assistant", "content": _REPORT_TEXT}]
        session_store.start_session(uid, s)
        from app.agent.coach import FINISHED_HINT

        outs = list(s.handle_stream("谢谢，再见"))
        self.assertEqual("".join(outs), FINISHED_HINT)
        self.assertEqual(s.messages[-1]["content"], FINISHED_HINT, "hint 应进上下文")
        session_store.save_session(uid, s)
        row = db.list_sessions_by_user(uid)[0]
        self.assertIn("总分", row["report"], "真报告不应被 hint 覆盖")

    def test_greeting_intro_enters_llm_context(self):
        """bug #28：开场自我介绍进 LLM 上下文。"""
        s = InterviewSession("mock")
        with (
            mock.patch(
                "app.agent.coach._pick_question",
                return_value={"id": 3, "title": "T", "difficulty": "中"},
            ),
            mock.patch.object(s, "_chat_stream", return_value=iter(["第一题：…"])),
        ):
            list(s.handle_stream("我叫张三，三年后端经验"))
        joined = "".join(m["content"] for m in s.messages)
        self.assertIn("我叫张三", joined)

    def test_stable_source_id(self):
        """bug #31：javaguide source_id 按标题哈希，不随页内位置偏移。"""
        from app.crawler.javaguide import _stable_source_id

        a = _stable_source_id("topic", "同一道题")
        self.assertEqual(a, _stable_source_id("topic", "同一道题"))
        self.assertTrue(a.startswith("topic:"))
        self.assertNotEqual(a, _stable_source_id("topic", "另一道题"))


class WalModeTests(unittest.TestCase):
    """bug #8：init_db 后必须处于 WAL 模式（读写不互斥）。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "wal.db")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_wal_enabled_after_init(self):
        db.init_db()
        conn = sqlite3.connect(config.DB_PATH)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")


class HistoryLimitTests(unittest.TestCase):
    """bug #11：limit 无下界时 SQLite LIMIT -1 等价无限制，可拉全表。"""

    def setUp(self):
        _isolate_rate_limit()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "limit.db")
        self._patch.start()
        db.init_db()
        self.client = TestClient(app)
        r = self.client.post(
            "/api/auth/register", json={"username": "limituser", "password": "pass123456"}
        )
        self.hdr = {"Authorization": f"Bearer {r.json()['token']}"}

    def tearDown(self):
        self._patch.stop()
        _isolate_rate_limit()
        self._tmpdir.cleanup()

    def test_negative_and_zero_limit_rejected(self):
        for bad in (-1, 0, 201, 10**9):
            resp = self.client.get("/api/session/history", params={"limit": bad}, headers=self.hdr)
            self.assertEqual(resp.status_code, 422, f"limit={bad} 应被 422 拒绝")

    def test_valid_limit_accepted(self):
        resp = self.client.get("/api/session/history", params={"limit": 1}, headers=self.hdr)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["items"], [])

    def test_rows_exposed_fields_are_light(self):
        """瘦列回归：历史行不再携带 state_json/jd 等大字段。"""
        db.create_session("coach", job_title="t", user_id=999999, state_json='{"big": 1}')
        items = db.list_sessions_by_user(999999, limit=10)
        self.assertEqual(len(items), 1)
        self.assertNotIn("state_json", items[0])
        self.assertNotIn("jd", items[0])
        self.assertIn("score", items[0])


class LlmRetryScopeTests(unittest.TestCase):
    """bug #18：4xx 客户端错误立即抛出，瞬时错误才重试。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "llm.db")
        self._patch.start()
        db.init_db()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def _run_chat(self, exc: Exception, max_retries: int = 3) -> tuple[int, object]:
        """mock 客户端永远抛 exc，返回（create 调用次数, 抛出的异常）。"""
        calls = {"n": 0}

        def boom(**kwargs):
            calls["n"] += 1
            raise exc

        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
        with (
            mock.patch.object(llm, "get_client", return_value=fake),
            mock.patch.object(llm.time, "sleep"),
        ):
            try:
                llm.chat([{"role": "user", "content": "hi"}], max_retries=max_retries)
                raised = None
            except Exception as e:
                raised = e
        return calls["n"], raised

    def test_4xx_no_retry(self):
        for status in (400, 401, 403, 422):
            n, raised = self._run_chat(_api_error(status))
            self.assertEqual(n, 1, f"http {status} 不应重试")
            self.assertIsInstance(raised, APIStatusError)

    def test_5xx_and_ratelimit_retry(self):
        rate_limit = RateLimitError(
            "rate",
            response=SimpleNamespace(status_code=429, headers={}, request=SimpleNamespace()),
            body=None,
        )
        for exc in (_api_error(500), _api_error(503), rate_limit):
            n, raised = self._run_chat(exc)
            self.assertEqual(n, 3, f"{type(exc).__name__} 应重试满 3 次")
            self.assertIs(raised, exc)

    def test_connection_error_retry(self):
        n, _ = self._run_chat(ConnectionError("boom"))
        self.assertEqual(n, 3)


class TagsContractTests(unittest.TestCase):
    """bug #1 回归：tags 数组查询参数过滤正常（前端 axios 序列化已对齐 tags=a&tags=b）。"""

    def setUp(self):
        _isolate_rate_limit()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "tags.db")
        self._patch.start()
        db.init_db()
        db.upsert_question(source="probe", title="Redis 持久化方式", tags=["Redis"])
        db.upsert_question(source="probe", title="Django ORM", tags=["Django"])
        self.client = TestClient(app)
        r = self.client.post(
            "/api/auth/register", json={"username": "tagsuser", "password": "pass123456"}
        )
        self.hdr = {"Authorization": f"Bearer {r.json()['token']}"}

    def tearDown(self):
        self._patch.stop()
        _isolate_rate_limit()
        self._tmpdir.cleanup()

    def test_tags_filter_matches_contract(self):
        body = self.client.get(
            "/api/questions", params={"tags": ["Redis"]}, headers=self.hdr
        ).json()
        items = body.get("items", body)
        self.assertEqual(len(items), 1)
        self.assertIn("Redis", items[0]["title"])


def _mojibake(text: str) -> str:
    """按真实故障路径构造乱码样本：UTF-8 字节被按 latin-1 误解码。"""
    return text.encode("utf-8").decode("latin-1")


_REPORT_TEXT = "【总分】80/100\n## 知识薄弱点\n- 索引原理\n## 改进建议\n- 多刷题"


class ReportPersistenceTests(unittest.TestCase):
    """bug #3：报告落库复用活跃会话行，历史列表不再每场面试重复两条。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "DB_PATH", Path(self._tmpdir.name) / "report.db")
        self._patch.start()
        db.init_db()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def _make_finished_session(self, uid: int) -> InterviewSession:
        s = InterviewSession("mock", user_id=uid)
        s.turn = "report"
        s.finished = True
        s.answers = [{"stage": "自我介绍", "title": "Q1", "answer": "A1"}]
        s.messages = [{"role": "assistant", "content": _REPORT_TEXT}]
        return s

    def test_report_single_row(self):
        import app.stores.session_store as session_store

        uid = 424242
        s = self._make_finished_session(uid)
        session_store.start_session(uid, s)
        # 模拟语音侧完成回调链：_persist_report（报告落库）+ save_session（状态落库）
        s._persist_report(s.messages[-1]["content"])
        session_store.save_session(uid, s)
        rows = db.list_sessions_by_user(uid, limit=10)
        self.assertEqual(len(rows), 1, "报告只应落在活跃会话行，不得新建第二条")
        row = rows[0]
        self.assertEqual(row["status"], "done", "完成的会话应归档为 done")
        self.assertIn("80", str(row["score"]))
        self.assertIn("总分", row["report"])
        answers = db.get_session_answers(row["id"])
        self.assertEqual(len(answers), 1, "问答记录应落在同一行下")
        self.assertEqual(answers[0]["question_title"], "Q1")


class ReportStreamRollbackTests(unittest.TestCase):
    """bug #4：报告/出题流 LLM 失败 → 状态回滚；barge-in（CancelledError）不回滚。"""

    def setUp(self):
        self.s = InterviewSession("mock")
        self.s.turn = "followup"
        self.s.finished = False
        self.s.answers = [{"stage": "基础", "title": "Q1", "answer": "A1"}]
        self.s.messages = [{"role": "assistant", "content": "上一轮回复"}]
        self.n_before = len(self.s.messages)

    def test_report_stream_failure_rolls_back(self):
        with (
            mock.patch.object(self.s, "_chat_stream", side_effect=RuntimeError("llm down")),
            self.assertRaises(RuntimeError),
        ):
            list(self.s._finish_report_stream())
        self.assertFalse(self.s.finished, "报告流失败后 finished 必须回滚")
        self.assertEqual(self.s.turn, "followup")
        self.assertEqual(len(self.s.messages), self.n_before, "空 assistant 占位应被回滚")

    def test_report_stream_success_persists(self):
        def ok_stream(*args, **kwargs):
            yield "【总分】80/100"

        with (
            mock.patch.object(self.s, "_chat_stream", side_effect=ok_stream),
            mock.patch.object(self.s, "_persist_report") as spy,
        ):
            outs = list(self.s._finish_report_stream())
        self.assertEqual("".join(outs), "【总分】80/100")
        self.assertTrue(self.s.finished)
        spy.assert_called_once_with("【总分】80/100")

    def test_barge_in_keeps_state(self):
        """barge-in（CancelledError）走 BaseException 分支：状态保留，不打断重出报告语义。"""
        import asyncio

        def half_then_cancel(*args, **kwargs):
            yield "报告前半"
            raise asyncio.CancelledError()

        with (
            mock.patch.object(self.s, "_chat_stream", side_effect=half_then_cancel),
            self.assertRaises(asyncio.CancelledError),
        ):
            list(self.s._finish_report_stream())
        self.assertTrue(self.s.finished, "barge-in 不应回滚报告状态")
        self.assertEqual(self.s.turn, "report")

    def test_ask_question_failure_rolls_back(self):
        """出题流失败：asked_ids/current_q/messages 回滚，题目不被静默消费。"""
        s = self.s
        s.turn = "answering"
        s.current_q = {"id": 7, "title": "上一题", "difficulty": "中"}
        n_before = len(s.messages)
        with (
            mock.patch(
                "app.agent.coach._pick_question",
                return_value={"id": 9, "title": "新题", "difficulty": "中"},
            ),
            mock.patch.object(s, "_chat_stream", side_effect=RuntimeError("llm down")),
            self.assertRaises(RuntimeError),
        ):
            list(s._ask_next_question_stream())
        self.assertNotIn(9, s.asked_ids, "出题失败后新题不应被标记已问")
        self.assertEqual(s.current_q["id"], 7)
        self.assertEqual(len(s.messages), n_before)


class RepairTextTests(unittest.TestCase):
    """bug #2 存量乱码修复工具：latin-1 误解码的 UTF-8 文本可无损还原。"""

    def test_mojibake_restored(self):
        # 真实故障路径 round-trip：中文 -> mojibake -> repair 还原
        for original in (
            "Java 语言有哪些特点？",
            "⭐️ JVM vs JDK vs JRE",
            "为什么说 Java 语言“编译与解释并存”？",
        ):
            self.assertEqual(repair_text(_mojibake(original)), original)

    def test_ascii_unchanged(self):
        self.assertEqual(repair_text("Java SE vs Java EE"), "Java SE vs Java EE")
        self.assertEqual(repair_text(""), "")

    def test_normal_chinese_unchanged(self):
        text = "在项目中如何利用 Redis 实现分布式 Session？"
        self.assertEqual(repair_text(text), text)

    def test_none_and_latin1_passthrough(self):
        self.assertIsNone(repair_text(None))
        # latin-1 原生文本（非法 UTF-8 序列）不动
        raw = "caf\xe9"
        self.assertEqual(repair_text(raw), raw)


if __name__ == "__main__":
    unittest.main()
