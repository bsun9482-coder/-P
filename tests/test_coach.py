"""教练层状态机测试：完整模拟面试流程 + 流式输出（mock LLM，不触网）。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.agent import coach
from app.agent.coach import InterviewSession
from app.core import config, db

FAKE_QUESTION = {
    "id": 1,
    "title": "谈谈 Python 的 GIL",
    "tags": "Python基础",
    "difficulty": "简单",
    "source": "mianshiya",
}

LONG_ANSWER = "（我的详细回答）" * 30  # 超过 SHALLOW_MIN_CHARS，且不含模糊词


def test_parse_report_dimensions_scoped_to_score_section():
    """薄弱点/改进清单里以数字结尾的条目不能被误判成维度分。"""
    report = (
        "【总分】78/100\n"
        "- 技术正确性：32\n"
        "- 表达清晰度 24\n"
        "知识薄弱点：\n"
        "- 熟悉 Python 3\n"
        "改进建议：\n"
        "- 阅读 Flask 源码 2 遍\n"
    )
    data = coach.parse_report(report)
    assert data["dimensions"] == [
        {"label": "技术正确性", "score": 32},
        {"label": "表达清晰度", "score": 24},
    ]
    assert data["weak_points"] == ["熟悉 Python 3"]
    assert data["improvements"] == ["阅读 Flask 源码 2 遍"]


def test_extract_section_does_not_activate_on_keyword_in_list_item():
    """薄弱点条目含"改进"字样时，不得误激活"改进建议"章节（bug #30）。"""
    report = (
        "【总分】78/100\n"
        "- 技术正确性：32\n"
        "知识薄弱点：\n"
        "- 需要改进算法理解\n"
        "- 熟悉 Python 3\n"
        "改进建议：\n"
        "- 阅读 Flask 源码\n"
    )
    data = coach.parse_report(report)
    assert data["weak_points"] == ["需要改进算法理解", "熟悉 Python 3"]
    assert data["improvements"] == ["阅读 Flask 源码"]


class CoachFlowTests(unittest.TestCase):
    def setUp(self):
        """把数据库指向临时文件，避免测试把面试记录写进真实题库。"""
        self._tmp_dir = tempfile.mkdtemp()
        self._orig_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp_dir) / "test.db"
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._orig_db_path

    def test_full_mock_interview_flow(self):
        s = InterviewSession("mock")
        with (
            mock.patch("app.agent.coach._pick_question", return_value=FAKE_QUESTION),
            mock.patch("app.agent.coach.llm.chat", return_value="小P回复"),
        ):
            s.handle("自我介绍：我是张三，3年后端")
            self.assertEqual(s.turn, "answering")
            self.assertEqual(s.stage_idx, 0)
            for i in range(6):
                s.handle("我的回答")
                self.assertEqual(s.turn, "followup", f"第{i + 1}题后应进入追问")
                s.handle(LONG_ANSWER)
                if i < 5:
                    self.assertEqual(s.stage_idx, i + 1)
                    self.assertEqual(s.turn, "answering")
                else:
                    self.assertTrue(s.finished)
                    self.assertEqual(s.turn, "report")
                    self.assertTrue(
                        any(m["role"] == "user" and "评分" in m["content"] for m in s.messages),
                        "总结报告提示词应包含评分要求",
                    )

    def test_coach_mode_rag(self):
        s = InterviewSession("coach")
        with (
            mock.patch("app.agent.coach.db.fts_search", return_value=[]),
            mock.patch("app.agent.coach.llm.chat", return_value="标准参考回答…"),
        ):
            reply = s.handle("Redis 缓存穿透怎么答")
        self.assertIn("标准参考回答", reply)
        self.assertEqual(s.mode, "coach")

    def test_handle_stream_yields_text(self):
        s = InterviewSession("coach")
        with (
            mock.patch("app.agent.coach.db.fts_search", return_value=[]),
            mock.patch(
                "app.agent.coach.llm.chat_stream", return_value=iter(["标", "准", "答", "案"])
            ),
        ):
            text = "".join(s.handle_stream("怎么答"))
        self.assertEqual(text, "标准答案")
        self.assertEqual(s.messages[-1]["role"], "assistant")

    def test_stream_abandon_mid_question_keeps_state_consistent(self):
        """语音 barge-in：出题流中途放弃后，状态已提交，下一句按本题回答路由而非追问回答。"""
        s = InterviewSession("mock", questions=["Q1", "Q2"])

        def fake_stream(messages, **kwargs):
            yield "（mock）"

        with (
            mock.patch("app.agent.coach.db.fts_search", return_value=[]),
            mock.patch("app.agent.coach.llm.chat_stream", side_effect=fake_stream),
        ):
            list(s.handle_stream("自我介绍：我是张三，3年后端"))  # 第一题
            list(s.handle_stream("第一题的回答"))  # 点评+追问（提交 turn=followup）
            # 追问回答触发进入下一题：出题流中途放弃（模拟用户打断）
            g = s.handle_stream(LONG_ANSWER)
            for _ in g:
                break
            self.assertEqual(s.turn, "answering", "出题前应已提交 answering 状态")
            self.assertEqual(s.stage_idx, 1)
            self.assertEqual(s.current_q["title"], "Q2")
            # 用户随后说出对第 2 题的回答 → 应被记录为第 2 题答案
            list(s.handle_stream("第二题的回答"))
        self.assertEqual(s.turn, "followup")
        self.assertEqual([a["stage"] for a in s.answers], ["定制题 1", "定制题 2"])

    def test_stream_exception_rolls_back_followup_state(self):
        """流式迭代中途抛异常：恢复 turn/followup_count，并移除未完成的占位消息。"""
        s = InterviewSession("mock", questions=["Q1"])
        state = {"calls": 0}

        def chat_stream(messages, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                return iter(["（mock）"])

            def exploding():
                yield "部分点评"
                raise RuntimeError("网络中断")

            return exploding()

        with (
            mock.patch("app.agent.coach.db.fts_search", return_value=[]),
            mock.patch("app.agent.coach.llm.chat_stream", side_effect=chat_stream),
        ):
            list(s.handle_stream("自我介绍：我是张三，3年后端"))  # 出题流正常
            prev_answers = len(s.answers)
            prev_messages = list(s.messages)
            g = s.handle_stream(LONG_ANSWER)  # 点评+追问，流中途抛异常
            with self.assertRaises(RuntimeError):
                for _ in g:
                    pass
        self.assertEqual(s.turn, "answering", "异常后应回滚到 answering")
        self.assertEqual(s.followup_count, 0, "异常后追问计数应回滚")
        self.assertEqual(len(s.answers), prev_answers, "异常后不应残留重复的答案记录")
        self.assertEqual(s.messages, prev_messages, "异常后消息列表应完整恢复")

    def test_stream_exception_rolls_back_shallow_followup(self):
        """浅回答触发的二次追问流中断：恢复 followup_count 并清掉追加的消息。"""
        s = InterviewSession("mock", questions=["Q1"])
        state = {"calls": 0}

        def chat_stream(messages, **kwargs):
            state["calls"] += 1
            if state["calls"] <= 2:
                return iter(["（mock）"])

            def exploding():
                yield "部分追问"
                raise RuntimeError("网络中断")

            return exploding()

        with (
            mock.patch("app.agent.coach.db.fts_search", return_value=[]),
            mock.patch("app.agent.coach.llm.chat_stream", side_effect=chat_stream),
        ):
            list(s.handle_stream("自我介绍：我是张三，3年后端"))
            list(s.handle_stream(LONG_ANSWER))  # 点评+追问正常
            self.assertEqual(s.followup_count, 1)
            prev_msgs = len(s.messages)
            g = s.handle_stream("忘了，没接触过")  # 浅回答 → 二次追问，流中断
            with self.assertRaises(RuntimeError):
                for _ in g:
                    pass
        self.assertEqual(s.followup_count, 1, "异常后应回滚到第一次追问")
        self.assertEqual(s.turn, "followup")
        self.assertEqual(len(s.messages), prev_msgs, "异常后消息列表应完整恢复")
        self.assertFalse(
            any(m["content"].startswith("（追问的回答）") for m in s.messages),
            "异常后应清掉追加的追问回答消息",
        )

    def test_stream_exception_restores_messages_after_compaction(self):
        """流式失败发生在上下文压缩之后：应恢复压缩前的完整消息列表，而非截断后的索引。"""
        s = InterviewSession("mock", questions=["Q1"])
        state = {"calls": 0}

        def chat_stream(messages, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                return iter(["（mock）"])

            def exploding():
                yield "部分点评"
                raise RuntimeError("网络中断")

            return exploding()

        with (
            mock.patch("app.agent.coach.db.fts_search", return_value=[]),
            mock.patch("app.agent.coach.llm.chat_stream", side_effect=chat_stream),
        ):
            list(s.handle_stream("自我介绍：我是张三，3年后端"))  # 出题流正常
            # 预置超长历史，确保 _maybe_compact 在流式调用前触发压缩
            s.messages = s.messages[:1] + [
                {"role": "user", "content": f"历史消息{i}"}
                for i in range(coach.MAX_CONTEXT_MESSAGES + 6)
            ]
            prev_messages = list(s.messages)
            prev_answers = len(s.answers)
            g = s.handle_stream(LONG_ANSWER)  # 点评+追问：压缩后流中途抛异常
            with self.assertRaises(RuntimeError):
                for _ in g:
                    pass
        self.assertEqual(s.messages, prev_messages, "异常后应恢复压缩前的完整消息列表")
        self.assertEqual(len(s.answers), prev_answers, "异常后不应残留重复的答案记录")
        self.assertEqual(s.turn, "answering")
        self.assertEqual(s.followup_count, 0)

    def test_input_truncated(self):
        s = InterviewSession("coach")
        long_text = "x" * 5000
        with (
            mock.patch("app.agent.coach.db.fts_search", return_value=[]),
            mock.patch("app.agent.coach.llm.chat", return_value="ok"),
        ):
            s.handle(long_text)
        self.assertLessEqual(len(s.messages[-2]["content"]), 4000)

    def test_custom_questions_flow(self):
        """定制题：按给定题目逐题推进，答完所有定制题后出报告。"""
        s = InterviewSession(
            "mock", questions=["Q1", "Q2"], job_title="高级 Python 后端", jd="精通 FastAPI"
        )
        with mock.patch("app.agent.coach.llm.chat", return_value="小P回复"):
            s.handle("自我介绍：我是张三")
            self.assertEqual(s.current_q["title"], "Q1")
            self.assertEqual(s._stage_name(), "定制题 1")
            s.handle("Q1 的答案")
            s.handle(LONG_ANSWER)
            self.assertEqual(s.current_q["title"], "Q2")
            s.handle("Q2 的答案")
            s.handle(LONG_ANSWER)
        self.assertTrue(s.finished)
        self.assertEqual(s.turn, "report")
        self.assertEqual([a["stage"] for a in s.answers], ["定制题 1", "定制题 2"])

    def test_shallow_followup_triggers_second_followup(self):
        """追问回答过短/含糊：追加一次更具体的追问，答完才进入下一题。"""
        s = InterviewSession("mock", questions=["Q1", "Q2"])
        with mock.patch("app.agent.coach.llm.chat", return_value="小P回复"):
            s.handle("自我介绍：我是张三，3年后端")
            s.handle(LONG_ANSWER)  # 第一题回答
            self.assertEqual(s.turn, "followup")
            s.handle("忘了，没接触过")  # 追问回答过浅
        self.assertEqual(s.turn, "followup", "浅回答应触发第二次追问")
        self.assertEqual(s.stage_idx, 0, "不应提前进入下一题")
        self.assertEqual(s.followup_count, 2)

    def test_deep_followup_advances_to_next_question(self):
        """追问回答足够详细：进入下一题（不追加追问）。"""
        s = InterviewSession("mock", questions=["Q1", "Q2"])
        with mock.patch("app.agent.coach.llm.chat", return_value="小P回复"):
            s.handle("自我介绍：我是张三，3年后端")
            s.handle(LONG_ANSWER)
            s.handle(LONG_ANSWER)  # 追问回答足够详细
        self.assertEqual(s.turn, "answering")
        self.assertEqual(s.stage_idx, 1)
        self.assertEqual(s.current_q["title"], "Q2")

    def test_persona_in_system_prompt(self):
        """面试官人格注入系统提示词，且 reset 后保留。"""
        s = InterviewSession("mock", persona="一面 · 同级工程师")
        self.assertIn("一面 · 同级工程师", s.messages[0]["content"])
        s.reset("mock")
        self.assertIn("一面 · 同级工程师", s.messages[0]["content"])

    def test_finish_report_persists_session(self):
        """模拟面试结束后：问答、评分、报告、薄弱点与人格写入数据库。"""
        s = InterviewSession("mock", questions=["Q1"], persona="二面 · 资深工程师")

        def fake_chat(messages, **kwargs):
            if messages[-1]["role"] == "user" and "总结报告" in messages[-1]["content"]:
                return "【总分】85/100\n知识薄弱点：\n- Redis 缓存穿透\n- 索引失效\n改进建议：\n- 多练习"
            return "小P回复"

        with mock.patch("app.agent.coach.llm.chat", side_effect=fake_chat):
            s.handle("自我介绍")
            s.handle("我的回答")
            s.handle(LONG_ANSWER)  # 深入回答后结束并出报告
        rows = db.list_sessions(limit=5)
        self.assertTrue(rows, "应有面试记录落库")
        row = rows[0]
        self.assertEqual(row["score"], 85)
        self.assertIn("Redis 缓存穿透", row["weak_points"] or "")
        self.assertEqual(row["persona"], "二面 · 资深工程师")
        answers = db.get_session_answers(row["id"])
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["question_title"], "Q1")

    def test_report_uses_report_model(self):
        """REPORT_MODEL 配置生效：总结报告用指定模型，普通对话用默认模型。"""
        s = InterviewSession("mock", questions=["Q1"])
        report_kwargs: dict = {}

        def fake_chat(messages, **kwargs):
            if messages[-1]["role"] == "user" and "总结报告" in messages[-1]["content"]:
                report_kwargs.update(kwargs)
                return "【总分】80/100\n知识薄弱点：\n- 测试\n改进建议：\n- 复习"
            return "小P回复"

        with (
            mock.patch("app.agent.coach.config.REPORT_MODEL", "deepseek-reasoner"),
            mock.patch("app.agent.coach.llm.chat", side_effect=fake_chat),
        ):
            s.handle("自我介绍")
            s.handle("我的回答")
            s.handle(LONG_ANSWER)
        self.assertEqual(report_kwargs.get("model"), "deepseek-reasoner")


class ReferenceAnswerTests(unittest.TestCase):
    """点评兜底：参考答案注入与 mianshiya 单题同步补答案。"""

    def test_existing_answer_returned(self):
        row = {"id": 1, "source": "mianshiya", "source_id": "42", "answer": "标准答案"}
        self.assertEqual(coach._ensure_reference_answer(row), "标准答案")

    def test_non_mianshiya_no_answer_returns_none(self):
        row = {"id": 2, "source": "定制", "source_id": None, "answer": None}
        self.assertIsNone(coach._ensure_reference_answer(row))

    def test_mianshiya_backfills_on_demand(self):
        """缺答案的 mianshiya 题：同步抓一次详情补答案。"""
        row = {"id": 3, "source": "mianshiya", "source_id": "42", "answer": None}
        with (
            mock.patch("app.crawler.mianshiya.MianShiYaAdapter") as M,
            mock.patch(
                "app.agent.coach.db.get_question_by_id",
                return_value={"id": 3, "answer": "补到的答案"},
            ),
        ):
            M.return_value.fetch_details_for.return_value = {"total": 1, "updated": 1}
            got = coach._ensure_reference_answer(row)
        self.assertEqual(got, "补到的答案")
        M.return_value.fetch_details_for.assert_called_once_with(["42"])

    def test_mianshiya_backfill_failure_returns_none(self):
        row = {"id": 3, "source": "mianshiya", "source_id": "42", "answer": None}
        with mock.patch("app.crawler.mianshiya.MianShiYaAdapter", side_effect=RuntimeError("boom")):
            self.assertIsNone(coach._ensure_reference_answer(row))

    def test_comment_injects_reference_answer(self):
        """点评环节把本题参考答案拼进点评 prompt（注入集成）。"""
        q = dict(FAKE_QUESTION, answer="GIL 是全局解释器锁，限制多线程并行执行。")
        s = InterviewSession("mock")
        with (
            mock.patch("app.agent.coach._pick_question", return_value=q),
            mock.patch("app.agent.coach.llm.chat", return_value="点评回复"),
        ):
            s.handle("自我介绍：我是张三，3年后端")
            s.handle("我的回答")
        comment = next(
            m
            for m in s.messages
            if m["role"] == "user" and "点评" in m["content"] and "参考答案" in m["content"]
        )
        self.assertIn("GIL 是全局解释器锁", comment["content"])

    def test_comment_without_answer_no_fetch(self):
        """无答案的非 mianshiya 定制题：点评不注入参考答案，也不触发抓取。"""
        q = dict(FAKE_QUESTION, source="定制", answer=None)
        s = InterviewSession("mock")
        with (
            mock.patch("app.agent.coach._pick_question", return_value=q),
            mock.patch("app.agent.coach.llm.chat", return_value="点评回复"),
            mock.patch("app.crawler.mianshiya.MianShiYaAdapter") as M,
        ):
            s.handle("自我介绍：我是张三")
            s.handle("我的回答")
        M.assert_not_called()


if __name__ == "__main__":
    unittest.main()
