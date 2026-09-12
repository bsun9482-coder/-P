"""面试官小P核心逻辑：模拟面试 / 辅导答疑双模式状态机。

- 角色、规则与阶段配置集中在 app/prompts.py（单一来源）；
- 模拟面试：自我介绍 → 六阶段递进出题（每题点评+追问）→ 总结报告（0-100 评分）；
- 辅导答疑：标准参考回答 + 加分点 + 变式题，RAG 检索本地题库（FTS5）；
- 上下文管理：超出阈值自动把早期对话压缩成摘要，控制 token 成本；
- 选题：SQL 层随机（带标签/难度/排除已出题），避免全表捞回内存过滤；
- 支持流式输出（handle_stream）与同步输出（handle）。
"""

import copy
import json
import logging
import re
from datetime import datetime, timezone

import app.core.db as db
from app.agent import llm
from app.core import config
from app.services import prompts

logger = logging.getLogger("interview_coach.coach")

MAX_INPUT_CHARS = 4000  # 单次用户输入上限（防粘贴长文烧 token）
MAX_CONTEXT_MESSAGES = 24  # 上下文消息数阈值（不含 system）
SUMMARY_CHUNK = 16  # 超阈值后，每次把最旧的 N 条压缩为摘要

EMPTY_BANK_HINT = "题库暂时为空，请先运行 python -m app.crawler.run 抓取题库。"
FINISHED_HINT = "本轮模拟面试已结束。可以开始新一轮，或切换到辅导答疑模式继续练习。"
NO_REPLY_FALLBACK = "（小P暂时无法回答，请稍后重试。）"

#: 深度感知追问：一道题最多追问 2 次；追问回答过短/含糊时追加一次更具体的追问
MAX_FOLLOWUPS = 2
SHALLOW_MIN_CHARS = 80  # ponytail: 长度+模糊词启发式，需要更准可换成 LLM 深度判定
_HEDGE_WORDS = (
    "不太确定",
    "不确定",
    "不知道",
    "没接触过",
    "没怎么用过",
    "可能吧",
    "大概",
    "记不清",
    "忘了",
    "不会",
    "没做过",
)

_SCORE_RE = re.compile(r"【总分】\s*(\d{1,3})\s*/\s*100")


def _is_shallow_answer(text: str) -> bool:
    """粗略判断回答是否浮于表面：过短或含模糊措辞。"""
    text = (text or "").strip()
    return len(text) < SHALLOW_MIN_CHARS or any(w in text for w in _HEDGE_WORDS)


def _report_score(report: str) -> int | None:
    """从报告首行【总分】NN/100 提取整数分，缺失时返回 None。"""
    m = _SCORE_RE.search(report or "")
    if not m:
        return None
    return min(int(m.group(1)), 100)


def _extract_weak_points(report: str) -> str | None:
    """抽取报告"知识薄弱点"段落的清单行（提示词要求以 - 开头）。

    标题判定仅在未激活时生效：激活后含"薄弱点"字样的清单行仍是条目，
    不会被误判成新标题而丢弃（bug #16）。
    """
    out: list[str] = []
    active = False
    for raw in (report or "").splitlines():
        line = raw.strip()
        if not active:
            if "薄弱点" in line:
                active = True
            continue
        if line.startswith(("-", "•")) or re.match(r"^\d+[.、]", line):
            out.append("- " + re.sub(r"^[-•\d.、\s]+", "", line).strip())
        elif line:
            break
    return "\n".join(out) if out else None


def _extract_section_items(report: str, section_key: str) -> list[str]:
    """抽取报告中某个小节的清单行（标题含 section_key，条目以 - / • / 数字开头）。

    标题判定仅在未激活时生效，且要求是"非清单行"（标题样式），
    避免「薄弱点」小节里含 section_key 字样的清单行（如"需要改进×"）被
    误判为新标题而提前激活，导致改进清单混入薄弱点条目（bug #30）。
    """
    out: list[str] = []
    active = False
    for raw in (report or "").splitlines():
        line = raw.strip()
        if not active:
            is_list_item = line.startswith(("-", "•")) or bool(re.match(r"^\d+[.、]", line))
            if not is_list_item and section_key in line:
                active = True
            continue
        if line.startswith(("-", "•")) or re.match(r"^\d+[.、]", line):
            out.append(re.sub(r"^[-•\d.、\s]+", "", line).strip())
        elif line:
            break
    return out


def _extract_dimensions(report: str) -> list[dict]:
    """抽取维度分：仅匹配【总分】之后、薄弱点/改进等章节之前的
    `- 技术正确性：40` 或 `- 技术正确性 40`，返回 [{"label","score"}]。

    章节终止仅认"标题样式行"（【..】/ ## 开头，或非清单前缀且含关键词的行）：
    维度行本身含"建议"等字样不再提前截断（bug #16）。
    """
    dims: list[dict] = []
    started = False
    for raw in (report or "").splitlines():
        line = raw.strip()
        if not started:
            if "总分" in line:
                started = True
            continue
        is_heading_style = bool(re.match(r"^(【|\#{1,6}\s)", line))
        if is_heading_style or (
            not line.startswith(("-", "•")) and any(k in line for k in ("薄弱点", "改进", "建议"))
        ):
            break
        m = re.match(r"^[-•]\s*(.+?)[:：]\s*(\d{1,3})\s*$", line)
        if not m:
            m = re.match(r"^[-•]\s*(.+?)\s+(\d{1,3})\s*$", line)
        if not m:
            continue
        label = m.group(1).strip()
        if "总分" in label:
            continue
        dims.append({"label": label, "score": min(int(m.group(2)), 100)})
    return dims


def parse_report(report: str) -> dict:
    """把总结报告 markdown 解析为结构化数据，供前端报告面板直接渲染。"""
    return {
        "score": _report_score(report),
        "dimensions": _extract_dimensions(report),
        "weak_points": _extract_section_items(report, "薄弱点"),
        "improvements": _extract_section_items(report, "改进"),
    }


def _sanitize_input(text: str) -> str:
    """去首尾空白 + 截断超长输入。"""
    return (text or "").strip()[:MAX_INPUT_CHARS]


def _ensure_reference_answer(row) -> str | None:
    """确保当前题有参考答案：已有直接用；mianshiya 题缺答案则同步抓一次详情补全。

    作为后台追答案的兜底：异步没赶上时，点评环节对当前这一道题同步补，
    保证点评/追问有标准参考答案可参考。非 mianshiya 题或补不到则返回 None。
    """
    if not row:
        return None
    answer = row.get("answer") or ""
    if answer:
        return answer
    if row.get("source") == "mianshiya" and row.get("source_id") and row.get("id"):
        try:
            from app.crawler.mianshiya import MianShiYaAdapter

            stats = MianShiYaAdapter().fetch_details_for([str(row["source_id"])])
            if stats.get("updated"):
                fresh = db.get_question_by_id(row["id"])
                if fresh and fresh["answer"]:
                    return fresh["answer"]
        except Exception:
            logger.exception("点评兜底补答案失败（当前题 %s）", row.get("source_id"))
    return None


def _pick_question(
    stage_tags: list[str],
    source: str | None,
    difficulty: str | None,
    exclude_ids: set[int],
):
    """按条件在 SQL 层随机选题；难度/标签逐步放宽，最后全量兜底。"""
    for kwargs in (
        {"tags": stage_tags, "source": source, "difficulty": difficulty},
        {"tags": stage_tags, "source": source},
        {},
    ):
        rows = db.pick_random_question(exclude_ids=exclude_ids, **kwargs)
        if rows:
            return rows[0]
    return None


class InterviewSession:
    """一次模拟面试/辅导会话的状态。"""

    def __init__(
        self,
        mode: str,
        questions: list[str] | None = None,
        job_title: str = "",
        jd: str = "",
        persona: str = "",
        user_id: int | None = None,
    ):
        self.mode = mode  # 'mock' | 'coach'
        self.stage_idx = 0  # 当前阶段下标（模拟）
        self.asked_ids: set[int] = set()  # 已出题 id
        self.messages: list[dict] = []  # LLM 完整对话历史
        self.current_q = None  # 当前题目行（sqlite3.Row 或 None）
        self.turn = "greeting"  # 模拟：greeting|answering|followup|report
        self.answers: list[dict] = []  # 记录用户答案，供评分
        self.finished = False
        self.custom_questions = questions or []
        self.job_title = job_title
        self.jd = jd
        self.persona = persona
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.followup_count = 0  # 当前题已追问次数（深度感知追问）
        self.session_id: int | None = None  # 多用户持久化：对应 sessions 表行 id
        self.user_id: int | None = user_id  # 多用户隔离：关联到具体用户
        # 干净的展示历史（前端渲染用，与 LLM 内部 messages 分离）：
        # 由调用方（REST/WS 层）维护，start 时放欢迎语，每回合追加用户原文与助手回复。
        self.display_history: list[list] = []

        rules = prompts.MOCK_RULES if mode == "mock" else prompts.COACH_RULES
        persona_block = f"\n\n【本轮面试官】{persona}" if persona else ""
        extra = ""
        if self.custom_questions:
            extra = (
                f"\n\n【本轮为定制面试】目标岗位：{job_title or '未知'}\n"
                f"招聘信息/JD：{(jd or '未提供')[:2000]}\n"
                f"本轮共 {len(self.custom_questions)} 道定制题，逐题推进，按流程点评与追问。"
            )
        self.messages = [
            {"role": "system", "content": prompts.ROLE + persona_block + "\n" + rules + extra}
        ]

    # ------------------------------------------------------------ 持久化（多用户）

    def to_dict(self) -> dict:
        """把会话状态序列化为可 JSON 持久化的字典（供 DB 存取/恢复）。"""
        return {
            "mode": self.mode,
            "stage_idx": self.stage_idx,
            "asked_ids": sorted(self.asked_ids),
            "messages": self.messages,
            "current_q": dict(self.current_q) if self.current_q is not None else None,
            "turn": self.turn,
            "answers": self.answers,
            "finished": self.finished,
            "custom_questions": self.custom_questions,
            "job_title": self.job_title,
            "jd": self.jd,
            "persona": self.persona,
            "started_at": self.started_at,
            "followup_count": self.followup_count,
            "display_history": self.display_history,
            "user_id": self.user_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InterviewSession":
        """从 to_dict 的产物恢复会话状态（新建，再回填各字段）。"""
        data = data or {}
        sess = cls(
            mode=data.get("mode", "coach"),
            questions=data.get("custom_questions") or None,
            job_title=data.get("job_title") or "",
            jd=data.get("jd") or "",
            persona=data.get("persona") or "",
        )
        sess.stage_idx = int(data.get("stage_idx", 0))
        sess.asked_ids = set(data.get("asked_ids") or [])
        sess.messages = data.get("messages") or sess.messages
        sess.current_q = data.get("current_q")
        sess.turn = data.get("turn", "greeting")
        sess.answers = data.get("answers") or []
        sess.finished = bool(data.get("finished"))
        sess.started_at = data.get("started_at") or sess.started_at
        sess.followup_count = int(data.get("followup_count", 0))
        sess.display_history = data.get("display_history") or []
        sess.user_id = data.get("user_id")
        return sess

    def history_for_display(self) -> list[tuple[str, str]]:
        """导出一份可直接展示的对话历史（优先干净的 display_history）。"""
        if self.display_history:
            return [(str(r), str(c)) for r, c in self.display_history]
        out: list[tuple[str, str]] = []
        for m in self.messages:
            role = m.get("role")
            if role == "system":
                continue
            out.append((role, (m.get("content") or "").strip()))
        return out

    # ------------------------------------------------------------ 定制面试

    def _stage_name(self) -> str:
        if self.custom_questions and self.stage_idx < len(self.custom_questions):
            return f"定制题 {self.stage_idx + 1}"
        if self.stage_idx < len(prompts.STAGES):
            return prompts.STAGES[self.stage_idx][0]
        return "总结"

    def _total_questions(self) -> int:
        return len(self.custom_questions) if self.custom_questions else len(prompts.STAGES)

    # ------------------------------------------------------------ 对外主入口

    def handle(self, user_text: str) -> str:
        """同步入口：接收用户输入，推进状态，返回完整 AI 回复。

        失败时回滚全部状态，与流式入口的"快照→提交→异常回滚"语义一致（bug #35），
        避免同步调用方在 LLM 异常后留下"已追加但 turn 未推进"的中间态。
        """
        user_text = _sanitize_input(user_text)
        snapshot = self._state_snapshot()
        try:
            if self.mode == "coach":
                return self._handle_coach(user_text)
            return self._handle_mock(user_text)
        except Exception:
            self._restore_snapshot(snapshot)
            raise

    def _state_snapshot(self) -> dict:
        """深拷贝当前会话全部可变状态，用于异常回滚。"""
        return {
            "turn": self.turn,
            "stage_idx": self.stage_idx,
            "followup_count": self.followup_count,
            "messages": copy.deepcopy(self.messages),
            "answers": copy.deepcopy(self.answers),
            "asked_ids": set(self.asked_ids),
            "current_q": self.current_q,
            "finished": self.finished,
            "display_history": copy.deepcopy(self.display_history),
        }

    def _restore_snapshot(self, snap: dict) -> None:
        for k, v in snap.items():
            setattr(self, k, v)

    def handle_stream(self, user_text: str):
        """流式入口：返回生成器，逐段产出回复文本（配合 st.write_stream）。"""
        user_text = _sanitize_input(user_text)
        if self.mode == "coach":
            return self._handle_coach_stream(user_text)
        return self._handle_mock_stream(user_text)

    # ------------------------------------------------------------ LLM 调用

    def _chat(
        self, max_tokens: int = 2048, temperature: float = 0.7, model: str | None = None
    ) -> str:
        """统一的同步 LLM 调用（先压缩上下文）。"""
        self._maybe_compact()
        return llm.chat(self.messages, max_tokens=max_tokens, temperature=temperature, model=model)

    def _chat_stream(
        self, max_tokens: int = 2048, temperature: float = 0.7, model: str | None = None
    ):
        """统一的流式 LLM 调用（先压缩上下文），返回增量迭代器。"""
        self._maybe_compact()
        return llm.chat_stream(
            self.messages, max_tokens=max_tokens, temperature=temperature, model=model
        )

    # ------------------------------------------------------------ 上下文压缩

    def _maybe_compact(self) -> None:
        """消息超阈值时，把最旧一批对话压缩成摘要，控制上下文长度。"""
        system = self.messages[0]
        rest = self.messages[1:]
        if len(rest) <= MAX_CONTEXT_MESSAGES:
            return
        to_summarize = rest[:SUMMARY_CHUNK]
        rest = rest[SUMMARY_CHUNK:]
        before = len(self.messages)
        try:
            summary = llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是对话压缩器。把以下面试对话压缩成 200 字以内的摘要，"
                            "保留：已问题目、用户回答要点、已给出的点评与追问。只输出摘要。"
                        ),
                    },
                    {"role": "user", "content": json.dumps(to_summarize, ensure_ascii=False)},
                ],
                max_tokens=300,
                temperature=0.3,
            )
        except Exception:
            logger.exception("上下文压缩失败，直接丢弃最旧对话")
            summary = f"（已省略 {len(to_summarize)} 条较早对话）"
        self.messages = [
            system,
            {"role": "system", "content": f"【早期对话摘要】{summary}"},
            *rest,
        ]
        logger.info("上下文已压缩：%s -> %s 条消息", before, len(self.messages))

    # ------------------------------------------------------------ 辅导答疑

    def _build_rag_block(self, relevant) -> str:
        if not relevant:
            return ""
        block = "\n\n【参考题库（以下题目与当前问题相关，可辅助回答）】\n"
        for i, r in enumerate(relevant, 1):
            block += f"{i}. {r['title']}  [{r['source']} · {r['difficulty'] or '未知'}]\n"
            if r["answer"]:
                block += f"   参考答案：{r['answer'][:500]}\n"
        block += "\n请结合以上题库参考内容，按辅导答疑模板（标准参考回答 + 加分点 + 变式题）回答用户。若题库内容与问题无关可忽略。\n"
        return block

    def _handle_coach(self, user_text: str) -> str:
        relevant = db.fts_search(keyword=user_text, limit=5)
        rag_block = self._build_rag_block(relevant)
        self.messages.append({"role": "user", "content": rag_block + user_text})
        reply = self._chat()
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def _handle_coach_stream(self, user_text: str):
        relevant = db.fts_search(keyword=user_text, limit=5)
        rag_block = self._build_rag_block(relevant)
        self.messages.append({"role": "user", "content": rag_block + user_text})
        stream = self._chat_stream()
        # 先把 assistant 占位消息写进历史并逐段追加：流被中途打断（语音 barge-in）时，
        # 对话历史仍是"用户消息 + 部分回复"的连贯状态，不会留下悬空的指令
        self.messages.append({"role": "assistant", "content": ""})
        msg = self.messages[-1]
        for delta in stream:
            msg["content"] += delta
            yield delta
        msg["content"] = msg["content"].strip() or NO_REPLY_FALLBACK
        return msg["content"]

    # ------------------------------------------------------------ 模拟面试

    def _handle_mock(self, user_text: str) -> str:
        # 1) 开场：用户自我介绍后 → 出第一题
        if self.turn == "greeting":
            self.turn = "answering"
            # 自我介绍进 LLM 上下文：否则"项目深挖"阶段无项目信息可挖（bug #28）
            self.messages.append({"role": "user", "content": f"（自我介绍）{user_text}"})
            return self._ask_next_question()

        # 2) 用户在答题 → 点评 + 追问
        if self.turn == "answering":
            if self.current_q is None:
                return self._ask_next_question()  # 题库为空兜底
            self.answers.append(
                {
                    "stage": self._stage_name(),
                    "title": self.current_q["title"],
                    "answer": user_text,
                }
            )
            self.messages.append(
                {"role": "user", "content": f"（第{self.stage_idx + 1}题我的回答）{user_text}"}
            )
            # 点评时同步注入本题参考答案（缺答案的 mianshiya 题先同步补一次）
            reference = _ensure_reference_answer(self.current_q)
            content = (
                "用户刚回答了当前问题。请：1) 点评（好的方面+不足，简洁）；2) 追问 1 个深挖细节。"
            )
            if reference:
                content += f"\n\n【本题参考答案（仅供点评参考，勿照念）】\n{reference[:800]}"
            self.messages.append({"role": "user", "content": content})
            reply = self._chat()
            self.messages.append({"role": "assistant", "content": reply})
            self.followup_count = 1
            self.turn = "followup"
            return reply

        # 3) 用户在答追问 → 进入下一题 或 结束出报告
        if self.turn == "followup":
            self.messages.append({"role": "user", "content": f"（追问的回答）{user_text}"})
            if self.followup_count < MAX_FOLLOWUPS and _is_shallow_answer(user_text):
                self.followup_count += 1
                self.messages.append(
                    {
                        "role": "user",
                        "content": "用户对追问的回答仍然比较浅。请简短点评这次回答，"
                        "再追问 1 个更具体的问题（引用用户原话）。",
                    }
                )
                reply = self._chat()
                self.messages.append({"role": "assistant", "content": reply})
                return reply
            self.followup_count = 0
            self.stage_idx += 1
            if self.stage_idx >= self._total_questions():
                return self._finish_report()
            return self._ask_next_question()

        # 4) 报告已出
        return FINISHED_HINT

    def _handle_mock_stream(self, user_text: str):
        if self.turn == "greeting":
            self.turn = "answering"
            # 自我介绍进 LLM 上下文：否则"项目深挖"阶段无项目信息可挖（bug #28）
            self.messages.append({"role": "user", "content": f"（自我介绍）{user_text}"})
            return (yield from self._ask_next_question_stream())

        if self.turn == "answering":
            if self.current_q is None:
                return (yield from self._ask_next_question_stream())
            prev_answers = len(self.answers)
            prev_messages = copy.deepcopy(self.messages)
            prev_turn, prev_followup = self.turn, self.followup_count
            self.answers.append(
                {
                    "stage": self._stage_name(),
                    "title": self.current_q["title"],
                    "answer": user_text,
                }
            )
            self.messages.append(
                {"role": "user", "content": f"（第{self.stage_idx + 1}题我的回答）{user_text}"}
            )
            # 点评时同步注入本题参考答案（缺答案的 mianshiya 题先同步补一次）
            reference = _ensure_reference_answer(self.current_q)
            content = (
                "用户刚回答了当前问题。请：1) 点评（好的方面+不足，简洁）；2) 追问 1 个深挖细节。"
            )
            if reference:
                content += f"\n\n【本题参考答案（仅供点评参考，勿照念）】\n{reference[:800]}"
            self.messages.append({"role": "user", "content": content})
            # 先提交状态再流式：即使被语音打断（barge-in），下一句也会正确按"追问回答"路由
            # 若流式调用或迭代中途抛异常，需回滚状态与消息，避免会话卡死
            self.followup_count = 1
            self.turn = "followup"
            self.messages.append({"role": "assistant", "content": ""})
            msg = self.messages[-1]
            try:
                stream = self._chat_stream()
                for delta in stream:
                    msg["content"] += delta
                    yield delta
            except Exception:
                self.turn, self.followup_count = prev_turn, prev_followup
                del self.answers[prev_answers:]
                self.messages = prev_messages
                raise
            msg["content"] = msg["content"].strip() or NO_REPLY_FALLBACK
            return msg["content"]

        if self.turn == "followup":
            prev_followup = self.followup_count
            prev_messages = copy.deepcopy(self.messages)
            self.messages.append({"role": "user", "content": f"（追问的回答）{user_text}"})
            if self.followup_count < MAX_FOLLOWUPS and _is_shallow_answer(user_text):
                self.followup_count += 1
                self.messages.append(
                    {
                        "role": "user",
                        "content": "用户对追问的回答仍然比较浅。请简短点评这次回答，"
                        "再追问 1 个更具体的问题（引用用户原话）。",
                    }
                )
                self.messages.append({"role": "assistant", "content": ""})
                msg = self.messages[-1]
                try:
                    stream = self._chat_stream()
                    for delta in stream:
                        msg["content"] += delta
                        yield delta
                except Exception:
                    self.followup_count = prev_followup
                    self.messages = prev_messages
                    raise
                msg["content"] = msg["content"].strip() or NO_REPLY_FALLBACK
                return msg["content"]
            self.followup_count = 0
            self.stage_idx += 1
            if self.stage_idx >= self._total_questions():
                return (yield from self._finish_report_stream())
            # stage_idx 在此自增后出题；若出题流失败，需连同 stage_idx 一起回滚，
            # 否则下次语音发言会跳过本道题（bug #5）
            prev_stage = self.stage_idx
            try:
                return (yield from self._ask_next_question_stream())
            except BaseException:
                self.stage_idx = prev_stage
                raise

        # 4) 报告已出：追加 hint 进上下文与展示历史，否则 session.py 会把
        # messages[-1]（仍是报告全文）重复追加一遍（bug #27）
        self.messages.append({"role": "user", "content": user_text})
        self.messages.append({"role": "assistant", "content": FINISHED_HINT})
        yield FINISHED_HINT
        return FINISHED_HINT

    # ------------------------------------------------------------ 内部动作

    def _ask_next_question(self) -> str:
        if self.custom_questions and self.stage_idx < len(self.custom_questions):
            stage_name = f"定制题 {self.stage_idx + 1}"
            diff = "未知"
            q = {
                "id": -(self.stage_idx + 1),
                "title": self.custom_questions[self.stage_idx],
                "tags": "定制",
                "difficulty": diff,
                "source": "定制",
            }
        else:
            stage_name, stage_tags, source, difficulty = prompts.STAGES[self.stage_idx]
            q = _pick_question(stage_tags, source, difficulty, self.asked_ids)
            if q is None:
                # 与流式入口一致：空题库提示写入 messages，避免历史误记用户原文（bug #6）
                self.messages.append({"role": "assistant", "content": EMPTY_BANK_HINT})
                return EMPTY_BANK_HINT
            diff = q["difficulty"] or "未知"
        self.asked_ids.add(q["id"])
        self.current_q = q
        self.messages.append(
            {
                "role": "user",
                "content": (
                    f"【出题】第{self.stage_idx + 1}题，阶段「{stage_name}」，"
                    f"难度「{diff}」。题目：{q['title']}\n"
                    f"请以面试官口吻把这道题自然地抛给用户（可稍作引导，不要直接给答案）。"
                ),
            }
        )
        reply = self._chat()
        self.messages.append({"role": "assistant", "content": reply})
        self.turn = "answering"
        return reply

    def _ask_next_question_stream(self):
        if self.custom_questions and self.stage_idx < len(self.custom_questions):
            stage_name = f"定制题 {self.stage_idx + 1}"
            diff = "未知"
            q = {
                "id": -(self.stage_idx + 1),
                "title": self.custom_questions[self.stage_idx],
                "tags": "定制",
                "difficulty": diff,
                "source": "定制",
            }
        else:
            stage_name, stage_tags, source, difficulty = prompts.STAGES[self.stage_idx]
            q = _pick_question(stage_tags, source, difficulty, self.asked_ids)
            if q is None:
                # 空题库提示同样写入 messages，否则 session.py 会把
                # messages[-1]（用户原文）误当助手回复追加进历史（bug #6）
                self.messages.append({"role": "assistant", "content": EMPTY_BANK_HINT})
                self.turn = "answering" if self.custom_questions else self.turn
                yield EMPTY_BANK_HINT
                return EMPTY_BANK_HINT
            diff = q["difficulty"] or "未知"
        # 先提交"正在出题"状态：题目播报中途被用户打断（barge-in，CancelledError
        # 不走下面的回滚分支）时，下一句会按本题回答处理；
        # 若 LLM 调用真正失败（网络/限流），则回滚全部状态，避免"题目没听到却被
        # 按本题消费"的会话卡死（bug #4）。快照必须先于 asked_ids.add 等一切变更。
        prev_turn = self.turn
        prev_messages = copy.deepcopy(self.messages)
        prev_asked = set(self.asked_ids)
        prev_q = self.current_q
        self.asked_ids.add(q["id"])
        self.current_q = q
        self.messages.append(
            {
                "role": "user",
                "content": (
                    f"【出题】第{self.stage_idx + 1}题，阶段「{stage_name}」，"
                    f"难度「{diff}」。题目：{q['title']}\n"
                    f"请以面试官口吻把这道题自然地抛给用户（可稍作引导，不要直接给答案）。"
                ),
            }
        )
        self.turn = "answering"
        self.messages.append({"role": "assistant", "content": ""})
        msg = self.messages[-1]
        try:
            stream = self._chat_stream()
            for delta in stream:
                msg["content"] += delta
                yield delta
        except Exception:
            self.turn = prev_turn
            self.asked_ids = prev_asked
            self.current_q = prev_q
            self.messages = prev_messages
            raise
        msg["content"] = msg["content"].strip() or NO_REPLY_FALLBACK
        return msg["content"]

    def _finish_report(self) -> str:
        self.turn = "report"
        self.finished = True
        answers_txt = "\n".join(
            f"- [{a['stage']}] {a['title']}\n  回答：{a['answer'][:300]}" for a in self.answers
        )
        self.messages.append(
            {
                "role": "user",
                "content": (
                    "全部题目已结束。请基于以下用户回答，输出【总结报告】：\n"
                    "1) 第一行输出【总分】NN/100（NN 为 0-100 整数评分，按 "
                    f"{prompts.SCORE_WEIGHTS} 加权），随后给出分项分；\n"
                    "2) 知识薄弱点（具体到知识点，每行以 - 开头）；\n"
                    "3) 改进建议清单（可执行、分优先级）。\n\n"
                    f"用户全部回答：\n{answers_txt}"
                ),
            }
        )
        reply = self._chat(max_tokens=3000, model=config.REPORT_MODEL or None)
        self.messages.append({"role": "assistant", "content": reply})
        self._persist_report(reply)
        return reply

    def _finish_report_stream(self):
        # 状态提交与回滚：报告流失败（LLM 重试耗尽是常态事件）若不回滚，
        # "finished=True + 空 assistant 消息" 的损坏状态被语音侧持久化，
        # 该会话此后永远只回 FINISHED_HINT、报告永久丢失（bug #4）。
        # barge-in（CancelledError）不回滚：保持"正在出报告"语义由用户重新触发。
        prev_turn, prev_finished = self.turn, self.finished
        prev_messages = copy.deepcopy(self.messages)
        self.turn = "report"
        self.finished = True
        answers_txt = "\n".join(
            f"- [{a['stage']}] {a['title']}\n  回答：{a['answer'][:300]}" for a in self.answers
        )
        self.messages.append(
            {
                "role": "user",
                "content": (
                    "全部题目已结束。请基于以下用户回答，输出【总结报告】：\n"
                    "1) 第一行输出【总分】NN/100（NN 为 0-100 整数评分，按 "
                    f"{prompts.SCORE_WEIGHTS} 加权），随后给出分项分；\n"
                    "2) 知识薄弱点（具体到知识点，每行以 - 开头）；\n"
                    "3) 改进建议清单（可执行、分优先级）。\n\n"
                    f"用户全部回答：\n{answers_txt}"
                ),
            }
        )
        try:
            stream = self._chat_stream(max_tokens=3000, model=config.REPORT_MODEL or None)
            self.messages.append({"role": "assistant", "content": ""})
            msg = self.messages[-1]
            for delta in stream:
                msg["content"] += delta
                yield delta
        except Exception:
            self.turn, self.finished = prev_turn, prev_finished
            self.messages = prev_messages
            raise
        msg["content"] = msg["content"].strip() or NO_REPLY_FALLBACK
        self._persist_report(msg["content"])
        return msg["content"]

    def _persist_report(self, report: str) -> None:
        """把本轮问答与报告落库，供侧边栏「面试复盘」展示；失败仅记日志不影响对话。

        复用当前活跃会话行（session_id）：此前 create_session 新建第二条 done 记录，
        与 save_session 更新的 active 行互不相知，导致历史列表每场面试重复两条、
        复盘数据分裂（bug #3）。仅内存会话（无 session_id，如旧流程）才新建行。
        """
        try:
            sid = self.session_id
            if sid is None:
                sid = db.create_session(
                    self.mode,
                    job_title=self.job_title,
                    jd=self.jd,
                    source="定制" if self.custom_questions else "题库",
                    persona=self.persona,
                    started_at=self.started_at,
                    user_id=self.user_id,
                )
            db.add_session_answers(sid, self.answers)
            db.finish_session(sid, _report_score(report), report, _extract_weak_points(report))
        except Exception:
            logger.exception("面试记录落库失败（不影响本轮对话）")

    def reset(self, mode: str) -> None:
        """重建会话状态（模式切换/中途切题时调用）。"""
        self.__init__(mode, persona=self.persona, user_id=self.user_id)

    def ask_question_by_id(self, qid: int) -> str:
        """题库浏览→「出这道题」：直接以指定题目出题，开启一段新的模拟面试。"""
        row = db.get_question_by_id(qid)
        if row is None:
            return "题目不存在，可能已被清理。"
        self.reset("mock")  # 无条件重置，避免旧 stage/answers/messages 残留
        self.current_q = row
        self.asked_ids.add(row["id"])
        diff = row["difficulty"] or "未知"
        self.messages.append(
            {
                "role": "user",
                "content": (
                    f"【出题】用户从题库主动选择了这道题，来源「{row['source']}」，"
                    f"难度「{diff}」。题目：{row['title']}\n"
                    f"请以面试官口吻把这道题自然地抛给用户（可稍作引导，不要直接给答案）。"
                ),
            }
        )
        reply = self._chat()
        self.messages.append({"role": "assistant", "content": reply})
        self.turn = "answering"
        return reply
