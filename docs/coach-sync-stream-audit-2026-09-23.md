# 教练层「同步 / 流式」双实现审计（2026-09-23）

> 目标：为「消重三对实现」立项前，先把**重复范围、调用面、已存在的语义分叉、以及各方案的代价**查清。
> 本文只做复现与影响面盘点，**不含任何代码改动**。

## 一、结论摘要

1. 重复不止「三对」，实际是 **5 对**（`handle`/`handle_stream` 也算一对），同步侧合计约 **139 行**与流式侧平行维护。
2. **同步侧在生产代码里零调用** —— 生产只有两个消费方，都走 `handle_stream`：
   - `app/routers/session.py:205`（SSE `/api/chat`）
   - `app/voice_ws.py:372`（语音 WebSocket）
   同步侧（`handle` + 4 个 `_xxx` 同步方法）的唯一消费者是 `tests/`。
3. 两条路径**已经发生语义漂移**，实测 **13 处分叉**，全部集中在「报告已出」与「空回复」两条分支上（详见第四节）。
4. 因为生产不碰同步侧，**任何只动同步侧的改动对线上零风险**；真正的改动代价在**测试改造量**（约 10 个测试方法把 mock 从 `llm.chat` 换成 `llm.chat_stream`）。

## 二、重复清单（`app/agent/coach.py`）

| # | 同步实现 | 行 | 流式实现 | 行 | 重复内容 |
|---|---|---|---|---|---|
| 1 | `handle` | 318–332（15） | `handle_stream` | 352–357（6） | 入口分派 + 快照/回滚 |
| 2 | `_handle_coach` | 426–432（7） | `_handle_coach_stream` | 434–447（14） | FTS 检索 + RAG 拼装 + 消息追加 |
| 3 | `_handle_mock` | 451–509（59） | `_handle_mock_stream` | 511–605（95） | 四分支状态机（greeting/answering/followup/report） |
| 4 | `_ask_next_question` | 609–643（35） | `_ask_next_question_stream` | 645–702（58） | 选题 + 定制题构造 + 【出题】prompt |
| 5 | `_finish_report` | 704–726（23） | `_finish_report_stream` | 728–766（39） | 报告 prompt + 落库 |

**同步侧独有、随同步侧一同存亡的还有**：`_state_snapshot`（334–346）、`_restore_snapshot`（348–350）—— 二者只被 `handle` 调用。

**逐字重复的字符串字面量**（最容易在两处改一处漏）：

- 报告 prompt（713–719 ↔ 744–750，7 行）
- 定制题 dict 构造（613–619 ↔ 649–655，7 行）
- 【出题】prompt（630–639 ↔ 677–686，10 行）
- 点评 prompt（475–480 ↔ 536–541，6 行）

## 三、调用面（决定风险等级的关键）

```
生产（2 处，全走流式）
  app/routers/session.py:205   gen = session.handle_stream(msg)
  app/voice_ws.py:372          gen = session.handle_stream(text)

测试（唯一使用者）
  tests/test_coach.py            21 处 .handle(...) / .handle_stream(...)
  tests/test_question_bank.py     2 处 .handle(...)
  tests/test_bugfix_batch.py      6 处 _finish_report_stream / _ask_next_question_stream
```

**`handle`（同步）生产零调用**，`ask_question_by_id` 同样只有测试在用（另见第六节）。
也就是说 `_handle_coach` / `_handle_mock` / `_ask_next_question` / `_finish_report` 这四个同步方法**只有 `handle` 一个可达入口**，而 `handle` 只被测试调用。

## 四、已复现的语义分叉（13 处）

复现脚本：`.workbuddy/tmp/probe_sync_vs_stream.py`
（同一串输入分别走 `handle` 与排空 `handle_stream`，逐步对比
`turn / stage_idx / followup_count / finished / len(messages) / messages[-1]`）

### 场景 A —— 跑到出报告后再发言：**12 处分叉**

| 观察项 | `handle`（同步） | `handle_stream`（流式） |
|---|---|---|
| `messages` 条数 | 25（不再增长） | 27 → 29 → … 每轮 +2，**无上限增长** |
| `messages[-1].content` | 报告全文（`小P回复`） | `本轮模拟面试已结束。可以开始新一轮…` |

根因：报告分支（`coach.py:600–605`）里流式侧**显式追加**了 user 消息与 `FINISHED_HINT`
到 `messages`（注释写明是为了让 `session.py` 取 `messages[-1]` 时拿到 hint 而非报告全文），
而同步侧（`coach.py:508–509`）**直接 `return FINISHED_HINT`，不碰 `messages`**。

> 注意：这条差异**不是无关紧要的**——它正是 `session.py` 的 `done.report` 取值契约
> （`session.py:232–238` 用 `messages[-1]`）所依赖的行为。也就是说，**同步路径并不是流式路径的忠实替身**；
> 任何以为「两条路等价、可以随意换」的假设都是错的。

### 场景 B —— 空题库（`_pick_question` 返回 `None`）：**0 处分叉**

两处源码写法不同（同步 625–626 不设 `turn`；流式 663 有 `self.turn = "answering" if self.custom_questions else self.turn`），
但在实际可达路径下（空题库时 `current_q` 恒为 `None`，永远走不到 followup 分支）产出状态一致，**无需处理**。

### 场景 C —— coach 模式 LLM 返回空串：**1 处分叉**

| `handle`（同步） | `handle_stream`（流式） |
|---|---|
| assistant 消息 content = `''`（空） | `（小P暂时无法回答，请稍后重试。）` |

根因：流式侧有 `msg["content"] = msg["content"].strip() or NO_REPLY_FALLBACK`（`coach.py:446`），
同步侧（`coach.py:430–431`）**没有这一步**，会把一条空 assistant 消息写进历史并落库。

## 五、方案与代价

### 方案 A（推荐）—— 同步入口退化为流式入口的薄壳

```python
def handle(self, user_text: str) -> str:
    gen = self._advance(user_text)  # 即现 handle_stream 的实现
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value
```

- 删除 4 个同步私有方法（约 124 行）。`_state_snapshot`/`_restore_snapshot` **保留**：
  薄壳仍需外层快照，兜住流式内部不回滚的分支（如 `_handle_coach_stream` 异常时留下的用户消息）。
- **生产零风险**：生产不调用 `handle`，流式路径一行不动。
- 13 处分叉**自动归零**（同步路径继承流式语义）。
- 代价：约 **10 个测试方法**需要把 mock 从 `llm.chat` 换成 `llm.chat_stream`，否则会打真实 LLM。
  已定位受影响断言：`tests/test_coach.py` 的 `test_finish_report_persists_session`（304）、
  `test_report_uses_report_model`（327，断言 `report_kwargs["model"] == "deepseek-reasoner"`，
  换路径后需改 patch 目标）、`test_comment_injects_reference_answer`（379）等。
- 风险：**中低**。改动面集中在测试；行为变化仅限「同步侧从此与流式侧一致」。

### 方案 B —— 只修平同步侧分叉，不动结构

- 给 `_handle_coach` 补 `NO_REPLY_FALLBACK`；给 `_handle_mock` 报告分支补 `messages` 追加。
- 再新增一个**同输入双路径 parity 测试**，钉死「两条路径状态完全一致」，防止将来再漂移。
- 代价：约 5 行改动 + 1 个测试；**不破坏任何现有测试**。
- 缺点：139 行重复仍在，只是被守卫钉住。
- 风险：**低**。

### 方案 C —— 抽公共步骤生成器，两条路径各自消费

- 把状态机抽成单一 `_advance()`，`handle_stream` 与 `handle` 都消费它。
- 收益：结构最优，将来加第三出口（批量 API）不用再抄。
- 代价：**要动 `handle_stream` 本身（= 生产路径）**，回归面最广。
- 风险：**高**。与用户对该方向的「影响面最广、排最后」判断一致。

## 六、顺带发现（本方向之外，仅登记不处理）

- `ask_question_by_id`（`coach.py:796`）在 **app/ 与 frontend/ 中均无调用方**，同样只有测试在用。
  若确认「题库浏览 → 出这道题」功能未接线，应单独登记为缺陷而不是并进本次消重。
- `_state_snapshot` / `_restore_snapshot` 仅服务于 `handle`，随方案 A 一并失去意义。

## 七、建议落地顺序

1. **先做方案 A**（薄壳消重）：一次消灭 5 对重复与 13 处分叉，且不触碰生产路径；
   改动前先把受影响测试的 mock 目标列全，用一个提交完成（`refactor(coach): ...`）。
2. 方案 B 可作为方案 A 的降级备选：若担心测试改造量过大，先只修平分叉 + 加 parity 守卫。
3. **方案 C 暂不做**，等同方向⑥ 的「结构最优版」，留待其他方向收口后再评估。


## 八、实施记录（2026-09-23 已完成）

按方案 A 落地，一并提交 `app/agent/coach.py` + `tests/test_coach.py` + 本文档。

### 改了什么

- `app/agent/coach.py`：**+12 / −135 行**（5 个 hunk）。
  `handle()` 改为排空 `handle_stream()` 并取 `StopIteration.value`；
  删除 `_handle_coach` / `_handle_mock` / `_ask_next_question` / `_finish_report`。
  `_state_snapshot` / `_restore_snapshot` **保留**（见方案 A 首条说明）。
- `tests/test_coach.py`：10 个受影响用例的 mock 从 `llm.chat` 迁到 `llm.chat_stream`
  （新增辅助 `_stream_once()` 处理"迭代器只能用一次"）；新增 `SyncStreamParityTests` 3 条守卫。
  `llm.chat` 在会跑到上下文压缩的用例里**保留**（压缩本身仍走 `llm.chat`）。

### 验收证据

| 项 | 结果 |
|---|---|
| 分叉探针（`.workbuddy/tmp/probe_sync_vs_stream.py`） | **13 → 0** 处；三场景均"状态完全一致" |
| `tests/test_coach.py` | 22 → **25** 通过 |
| 全量 `pytest` | **214 用例 / 1 失败 / 0 error / 1 skip**（唯一红点仍是 test_launch 代理陷阱） |
| `ruff check .` / `ruff format --check .` | 均干净 |

### 守卫探针（证明新测试真的会红）

把 `coach.py` 临时还原成 HEAD 的旧双实现后，`SyncStreamParityTests` **3 条全部失败**，
失败信息正是两处真实缺陷（报告已出分支 `messages[-1]` 被当成报告、coach 空回复留空消息）；
恢复新实现后重新全绿。守卫有效。

### 顺带发现

`ruff format` 0.16 **会格式化 Markdown 里的 Python 代码块** —— 本文档最初就因此
让 `ruff format --check .` 报红（代码块内注释对齐不符）。**新增含 Python 代码围栏的
文档要一并过格式关卡。**
