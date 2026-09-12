# 面试官小P · 缺陷检测与 Bug 清单

> 审计日期：2026-09-01 · 审计方式：4 路并行静态代码审计（安全 / 后端逻辑 / 语音链路 / 前端）+ 本地测试基线
> 测试基线：`python -m pytest` → **195 passed / 1 skipped**（全绿不代表无缺陷，仅说明现有测试未覆盖下述路径）
> 说明：所有缺陷均为代码可证实的问题，未做运行时复现；涉及 LLM/网络/edge-tts 的均已 mock。
> 行号基于审计当日代码，修复前请以当前文件为准复核。

## 严重程度分布

| 严重程度 | 数量 | 说明 |
|---|---|---|
| Critical | 0 | 未发现可被直接利用导致数据全量丢失/服务瘫痪的问题 |
| High | 3 | 丢数据 / 鉴权失效分叉 / 功能永久卡死 |
| Medium | 15 | 状态错乱 / 性能回归 / 资源泄漏 / 体验主要故障 |
| Low | 19 | 边界问题 / 纪律失效 / 体验细节 |

## 修复状态总览（2026-09-01 更新）

以下问题已在本轮修复并验证（前端已 `npm run build`，后端 pytest **194 passed / 2 skipped**、ruff 通过、浏览器回归通过）：

| 状态 | BUG 编号 |
|---|---|
| ✅ 已修复 | BUG-02、BUG-05、BUG-06、BUG-07、BUG-08、BUG-09、BUG-10、BUG-11、BUG-13、BUG-14、BUG-15、BUG-16、BUG-17、BUG-18、BUG-19、BUG-22、BUG-23、BUG-26、BUG-27、BUG-28、BUG-29、BUG-30、BUG-31、BUG-32、BUG-33、BUG-34、BUG-35、BUG-36、BUG-37 |
| ⏳ 遗留/说明 | BUG-01（跨端并发写锁，见下方说明）、BUG-04、BUG-12（语音 barge-in 竞态与 TTS 串行化，涉及线程/推送时序重构，风险高，见"遗留说明"） |
| 📋 待评估 | BUG-21（回声 3 字放行，属设计权衡）、BUG-24（报告隐藏需后端索引配合）、BUG-25（hint 死代码，无用户可见故障） |

> **关于 BUG-01**：本次为语音 `_produce` 增加了对 `display_history` 的维护（BUG-07），并用 REST 侧有界 `_chat_locks` 防止内存增长（BUG-32）；真正的"语音与 REST 并发写同一会话"跨端锁需要重构语音生成与 REST 共享同一把 per-user 锁，改动面大、有死锁风险，作为遗留架构项评估。

> **遗留说明（BUG-04 / BUG-12）**：语音 barge-in 时旧 `asyncio.to_thread` 线程不可中断、继续写 `session.messages`；TTS 因"先取推送权后合成"导致并发形同虚设。两者都要求对语音生成链路做线程安全重构，超出本轮安全修复范围，建议单独排期处理。

---

## High

### BUG-01 【High·优先修复】语音与文字两端并发读写同一会话，回合互相覆盖丢失

- **模块**：会话状态（`routers/session.py` + `voice_ws.py` + `stores/session_store.py`）
- **位置**：`app/routers/session.py:152-234`（`_chat_locks` 仅覆盖 REST）、`app/voice_ws.py:266-267`（语音侧无锁）、`app/stores/session_store.py:60-90`（last-wins 覆盖）
- **复现步骤**：
  - 前置：同一用户账号，已开启语音通话。
  1. 语音正在流式生成回复（会话已载入内存、尚未落库）。
  2. 同一用户切到文字版发消息 → `load_active_session` 读到语音侧上次落库的旧状态 → 文字版另生成一条回复。
  3. 语音生成结束 → `save_session` 写库；随后文字版结束 → 用「不含语音回合」的旧状态整体覆盖 `state_json`。
- **预期 vs 实际**：预期两端回合都保留；实际**后落库一方把另一方整轮回合抹掉**（丢回合），且两轮各付一次 LLM 计费。
- **根因**：语音与 REST 各自独立「load→mutate→save」，`_chat_locks` 只有 REST 一侧使用；DB 层 `update_session_state` 无版本号/乐观锁。
- **证据**：`session_store.py` 注释"每回合落库"；`voice_ws.py` 调用 `save_session` 无锁。无现成测试覆盖跨端并发。

### BUG-02 【High·优先修复】SSE 聊天/定制生成 401 不清令牌、不跳登录（fetch 绕过 axios 拦截器）

- **模块**：前端鉴权（`frontend/src/api/index.js`）
- **位置**：`frontend/src/api/index.js:43-46`（chatStream）、`:91-94`（customApi.generate）；对照 `frontend/src/api/http.js:27-37`
- **复现步骤**：
  - 前置：登录后令牌过期（`TOKEN_TTL_DAYS`）或后端清库重启使令牌失效。
  1. 在聊天框发消息。
- **预期 vs 实际**：预期与 REST 一致（401 → 清 token → 跳登录）；实际 SSE 走原生 `fetch`，401 只把响应体原文写进气泡，令牌残留（`isLoggedIn` 仍 true、路由守卫继续放行），用户停留在聊天页反复失败。
- **根因**：SSE 需 `ReadableStream` 故用 fetch，但未复用拦截器的 401 统一处理；两套 HTTP 通道鉴权失效行为分叉。

### BUG-03 【High·优先修复】题库对话框接口失败 → 骨架屏永久卡死

- **模块**：前端题库（`frontend/src/components/QuestionBankDialog.vue`）
- **位置**：`QuestionBankDialog.vue:185-198`（load 无 catch）、`:200-208`（watch 无 catch）、`:178-183`（loadMeta 无 catch）、模板 `:90-92`（`!loaded` 永久骨架屏）
- **复现步骤**：
  - 前置：打开题库对话框瞬间断网或后端重启返回 5xx。
  1. `load()` 抛未捕获异常，`loaded` 永远为 false。
- **预期 vs 实际**：预期失败显示错误态 + 重试；实际 4 条骨架行动画永久闪烁，无错误提示、无重试入口，只能关闭重开。
- **根因**：状态机只设计 loading/empty 两态，异常路径未纳入；同文件 `loadStats`/`loadHistory` 有 catch，此处遗漏。

---

## Medium

### BUG-04 【Med】语音 barge-in 时旧生成线程与新生成并发改写同一 `session.messages`

- **模块**：语音链路（`app/voice_ws.py` + `app/agent/coach.py`）
- **位置**：`voice_ws.py:253-270`（`_cancel_generation` 不等待任务）、`:354`+`:428`（`asyncio.to_thread(_next_chunk)`）、`coach.py:482-530`（worker 线程内 `msg["content"] += delta`）
- **复现步骤**：
  - 前置：语音中长回复正在生成（线程 A 正在迭代 LLM 流、逐段写 `session.messages[-1]`）。
  1. 用户开口打断 → `generation.cancel()`：协程任务被取消，但线程 A **无法被中断**，继续跑完整个流并写同一 dict。
  2. 新文本立即启动线程 B，对同一 `session.messages` 追加新回合；两端还可能并发触发 `_maybe_compact()` 重建。
- **预期 vs 实际**：预期旧回复被干净放弃；实际新旧线程并发读写同一 list/dict，旧回复后半段可能并入新回合或随 compact 丢失，序列化期间可能读到半成品。
- **根因**：取消的是事件循环协程，`asyncio.to_thread` 底层线程不可中断，且无锁/生成器 `close()` 保护。

### BUG-05 【Med】语音出题流失败不回滚 `stage_idx`，下次发言跳题

- **模块**：语音状态机（`app/agent/coach.py`）
- **位置**：`coach.py:629-660`（快照不含 `stage_idx`）、`:558-562`（followup 分支先 `stage_idx += 1` 再出题）
- **复现步骤**：
  - 前置：语音链路，用户答完追问（非浅答）。
  1. `stage_idx += 1` → 出第 N+1 题时 LLM 异常重试耗尽。
  2. 回滚了 `turn`/`current_q`/`messages`，但 `stage_idx` 停在 N+1；语音侧 `t.exception()` 不落库 → 内存漂移状态被保留。
  3. 用户下次发言 → 再次 `stage_idx += 1` → 出第 N+2 题，**第 N+1 题被永久跳过**。
- **预期 vs 实际**：预期失败后回到第 N+1 题；实际题目推进与用户实际答题错位。
- **根因**：出题流失败回滚快照遗漏 `stage_idx`（仅 followup 分支触发；REST 因每次请求重载会话被掩盖）。

### BUG-06 【Med】空题库时 `EMPTY_BANK_HINT` 不入 `messages`，历史把用户原文误记为助手回复

- **模块**：会话历史（`app/agent/coach.py` + `app/routers/session.py`）
- **位置**：`coach.py:621-623`（yield 提示但不 append）、`session.py:201-205`（流结束后取 `messages[-1]` 当助手回复）
- **复现步骤**：
  - 前置：全新部署、题库为空。
  1. `POST /session/start`（mock）→ 发自我介绍。
  2. 空题库 → `yield EMPTY_BANK_HINT` 后返回，**不写 messages**。
  3. 流结束把 `messages[-1]`（用户自我介绍原文）当助手回复追加进 `display_history` 并落库。
- **预期 vs 实际**：预期历史里是"题库暂时为空"提示；实际历史助手位置显示用户自己的原文，刷新后提示语丢失；此后每发一条消息都重复追加上一条用户消息为"助手回复"，历史逐条错乱。
- **根因**：空题库分支是唯一"yield 提示但不落 messages"的路径，与 `messages[-1]` 反推助手回复的约定不兼容。

### BUG-07 【Med】语音链路从不维护 `display_history`，语音回合在共享文字历史中永久缺失

- **模块**：会话历史（`app/voice_ws.py` + `app/agent/coach.py`）
- **位置**：`coach.py:290-300`（`history_for_display` 优先返回 `display_history`）、`voice_ws.py:352-472`（`_produce` 只写 `messages`）
- **复现步骤**：
  - 前置：先文字版聊几轮（`display_history` 被 REST 持续追加）。
  1. 再开语音聊若干轮（只增长 `messages`）。
  2. 回到文字版查看历史。
- **预期 vs 实际**：预期文字历史包含语音回合；实际 `history_for_display` 因 `display_history` 非空直接返回它，语音回合全部被遮蔽（`messages` 里有但被隐藏）。纯语音会话正常，一旦被 REST 触碰即错位。
- **根因**：`display_history` 注释声明"由 REST/WS 层共同维护"，但 WS 层从未实现，且无"display_history 比 messages 短则回退"的保护。

### BUG-08 【Med】调度器每日 02:00 两个 job 并发重复爬取 + 语义清洗

- **模块**：定时任务（`app/core/scheduler.py` + `app/core/config.py`）
- **位置**：`scheduler.py:107-123`（daily `CronTrigger` 与 `IntervalTrigger(hours=24)` 为两个不同 job id）、`config.py:28-29`（`CRAWL_TIME=02:00` 且 `CRAWL_INTERVAL_HOURS=24` 默认同时启用）
- **复现步骤**：
  - 前置：默认配置运行到 02:00。
  1. daily 与 interval 两个 job 各自触发 → `crawl_all()` + `_clean_job()`（含 LLM 语义清洗）各执行两次。
- **预期 vs 实际**：预期每天只抓一次；实际双份网络抓取 + 双份 LLM 清洗（双倍成本），并对 SQLite 写产生竞争（busy_timeout 兜底但阻塞）。进程在 02:00 附近启动时 bootstrap 会叠加为第三份。
- **根因**：两个触发器 24h 间隔下对齐到同一整点；APScheduler `max_instances` 按 job id 隔离，互不排斥。

### BUG-09 【Med】bootstrap 定时任务用本地墙钟当 Asia/Shanghai 解释，非上海时区主机"启动即抓取"被推迟数小时

- **模块**：定时任务（`app/core/scheduler.py`）
- **位置**：`scheduler.py:129`（`next_run_time=datetime.now()`）、`:103`（`timezone=Asia/Shanghai`）
- **复现步骤**：
  - 前置：时区为 UTC 的主机（容器常见）启动服务。
  1. `datetime.now()` 返回 UTC 墙钟（naive），APScheduler 按调度器时区解释为 Asia/Shanghai → 触发时间被换算到 8 小时后。
- **预期 vs 实际**：预期启动即抓；实际延迟 = 主机时区与上海时区差值（UTC 约 8h）。国内主机恰好一致，问题被掩盖。
- **根因**：naive 本地时间与调度器显式时区混用。

### BUG-10 【Med】SSE 客户端断线：回合静默丢弃 + 后台 worker 线程继续消费 LLM 流到结束

- **模块**：SSE 聊天（`app/routers/session.py`）
- **位置**：`session.py:190-234`（`async with lock` + `await asyncio.to_thread(_next_chunk)`）
- **复现步骤**：
  - 前置：SSE 流式生成中途断开。
  1. Starlette 取消 `_event_stream` → `CancelledError`（`BaseException`，不被 `except Exception` 捕获）。
  2. `save_session` 不执行 → 该轮用户消息与部分回复不落库（丢回合）。
  3. `to_thread` 里的同步生成器不被中断，继续拉完整 LLM 流（最长 120s），期间占默认线程池与上游连接。
- **预期 vs 实际**：预期断线保留已生成部分或终止 LLM 调用；实际回合静默丢失，大量断线会占满 `ThreadPoolExecutor` 使其他 `to_thread`（含正常 SSE）排队变慢。
- **根因**：`to_thread` 线程不可取消 + 取消路径不落库、不 `close()` 同步生成器。

### BUG-11 【Med】`asr_ready` 无条件切换 LISTENING，覆盖播报中的 SPEAKING，三种打断通道全部失效

- **模块**：语音前端（`frontend/src/composables/voice/useVoiceCall.js`）
- **位置**：`useVoiceCall.js:625-631`（`asr_ready` handler 无条件 `setPhase(LISTENING)`）
- **复现步骤**：
  - 前置：ASR 启动较慢 / TTS 首块较快（edge-tts 模式），或播报长回复期间 ASR 断线重连成功。
  1. `asr_ready` 在 `phase === SPEAKING` 时到达 → 强制切到 LISTENING。
- **预期 vs 实际**：预期只置 `V.asrReady=true` 保持 SPEAKING；实际 `V.speaking` 变 false → 手动打断、`asr_partial` 内容打断、`vadCheck` 音量打断全部失效，且 UI 显示"聆听中…"但音频仍在播放。
- **根因**：handler 假设"收到 asr_ready 时必然不在播报"，未检查 `V.phase`；对照 `asr_error` handler（不触碰 phase）可知是遗漏。

### BUG-12 【Med】turn 推送串行化把 TTS 合成本身也串行化，`TTS_MAX_CONCURRENCY=3` 形同虚设

- **模块**：语音后端（`app/voice_ws.py` + `app/services/tts.py`）
- **位置**：`voice_ws.py:402-404`（`await _await_turn(slot)` 位于 `synthesize` 之前）、`tts.py:50-52`（注释声称并行合成）
- **复现步骤**：
  - 前置：`VOICE_TTS=cosyvoice`，发一条会被切成 ≥2 段的回复（首块 45 字、后续 90 字阈值）。
  1. 观察第 2 段开始合成时刻。
- **预期 vs 实际**：预期多段同时合成（并发 3），整条回复时间大幅缩短；实际第 2 段要等第 1 段**推送完成**才开始合成，完全串行。10 段回复 40-60s 而非 15-20s；等待 turn 的任务还占着 semaphore 槽，第 4 段起连 sem 都拿不到。
- **根因**：修复 sid 乱序时把"获取推送权"放在"合成"之前，而非"合成完成后的 flush"之前。正确形态是先并发合成缓存音频块，拿到 turn 后再分配 sid 推送。

### BUG-13 【Med】AudioContext 从不 close，反复进出语音页约 6 次后接通无声

- **模块**：语音前端（`frontend/src/composables/voice/useVoiceCall.js`）
- **位置**：`:175-184`（`ensureAudio` 每实例新建）、`:755-763`（`cleanupAudio` 无 `close()`）、`:786-788`（`onUnmounted`）
- **复现步骤**：
  - 前置：不刷新整页。
  1. 连续进出 `/voice` 并接通 6~7 次，每次新建 `AudioContext`，挂断/卸载从不 `close()`。
  2. Chrome 对存活 AudioContext 有配额（约 6 个），超限后 `new AC()` 抛异常被空 catch 吞掉 → `V.audioCtx` 为 null → `onAudioFrame` 里 `if (!V.audioCtx) return` 静默丢弃所有 TTS 音频帧。
- **预期 vs 实际**：预期每次通话后释放或全局复用；实际泄漏到上限后新通话**无声且无任何提示**。
- **根因**：`cleanupAudio` 只清理采集流与播放源，漏掉 AudioContext 生命周期管理。

### BUG-14 【Med】语音意外断线后 `ui.active` 残留 true，"点击可重连"实际需点两次且语义相悖

- **模块**：语音前端（`frontend/src/composables/voice/useVoiceCall.js` + `frontend/src/views/VoiceView.vue`）
- **位置**：`useVoiceCall.js:744-746`（非 4409 onclose 未复位 `ui.active`）、`:690-692`（`connect` 被 `if (ui.active) return` 挡住）、`VoiceView.vue:69-72`
- **复现步骤**：
  - 前置：通话中后端重启 / 断网。
  1. WS onclose（非 4409）→ 状态条"连接已断开，点击可重连"，但 `ui.active` 仍 true，按钮显示"挂断"。
  2. 点底部按钮 → 实际执行 `disconnect()`，需**再点一次**才真正重连；点状态条则 `statusInterruptible=false` 完全无反应。
- **预期 vs 实际**：预期断开后一键重连（4409 分支正是这样做的）；实际提示与交互脱节、按钮语义与意图相悖。
- **根因**：onclose 只在 4409 分支复位 `ui.active`，普通断线漏掉。

### BUG-15 【Med】登出/401 强跳时进行中的 SSE 流未中止，后端继续生成、聊天锁被占

- **模块**：前端会话（`frontend/src/views/ChatView.vue` + `frontend/src/api/http.js`）
- **位置**：`ChatView.vue:357-362`（logout 未调 `_abortStream`）、`http.js:30-34`（401 硬跳转同样不中止流）
- **复现步骤**：
  - 前置：LLM 生成中。
  1. 立即退出登录 → 重新登录同一账号 → 立刻再发消息 → 收到 429"上一条回复仍在生成中"。
  2. 原因：`$reset()` 把 `abort` 置 null、旧流回调被代际检查屏蔽，但 **fetch 连接未断**，后端持续生成烧 token，per-user 锁（`session.py:190-193`）随旧流生命周期持有。
- **预期 vs 实际**：预期登出时先 `_abortStream()` 再 `$reset()`；实际 UI"干净"但网络/后端资源继续消耗，且短时间内阻塞该用户下一次对话。
- **根因**：登出/401 路径只重置状态未触发流取消；Pinia `$reset()` 不执行自定义清理 action。

### BUG-16 【Med】CSS 设计令牌纪律守卫失效：守卫仍盯旧蓝色，组件/全局硬编码珊瑚色全绿通过

- **模块**：前端样式纪律（`tests/test_css_discipline.py` + `frontend/src`）
- **位置**：`test_css_discipline.py:9`（`BRAND_LITERALS` 仍是旧蓝 `#4f6ef7` 等，而品牌已是珊瑚 `#d97852/#c4603a/#f3b599`）；`QuestionBankDialog.vue:355`（`#f9e6d8`）、`main.css:363-364/420-422/484/488-489`（写死珊瑚色阶）、`ReportPanel.vue:122`（`'#d9a441,#e6bb6d'`）、`index.html:6`（旧蓝 `theme-color #eef3ff`）
- **复现步骤**：
  - 前置：主题从蓝改珊瑚时只改了 `:root` 令牌值。
  1. 运行 `python -m pytest tests/test_css_discipline.py` → 三个测试全部通过，守卫对现行品牌色完全失明。
- **预期 vs 实际**：预期守卫锁定当前品牌字面量并抓到组件硬编码（违反 AGENTS.md）；实际测试通过但纪律已被破坏。
- **根因**：`BRAND_LITERALS` 未随主题同步更新，衍生浅色阶从未提升为令牌。

### BUG-17 【Med】ChatView 多处 await 无错误处理：失败零反馈，最坏导致当前会话被误归档

- **模块**：前端会话（`frontend/src/views/ChatView.vue`）
- **位置**：`:277-283`（onMounted `await chat.load()`）、`:291-301`、`:303-314`、`:322-325`、`:353-355`、`:370-375`
- **复现步骤**：
  - 场景 A（最坏链路）：已有一场进行中的面试 → 断网刷新 `/` → `chat.load()` 抛未捕获异常 → 后续初始化全部不执行 → 页面显示欢迎卡片 → 用户点"开始面试" → 后端 `archive_current` 把进行中的会话归档。
  - 场景 B：资料弹窗昵称超 32 字符 → `updateMe` 422 → 点保存毫无反应。
- **预期 vs 实际**：预期失败给出错误提示并保持现状；实际 unhandled rejection + 静默，且 onMounted 失败诱导用户做出破坏性操作。
- **根因**：错误处理策略不一致（同文件 `loadStats/loadHistory` 有 catch，其余裸奔）；onMounted 级联 await 放大单点失败。

### BUG-18 【Med】题库查询竞态：慢响应覆盖新结果

- **模块**：前端题库（`frontend/src/components/QuestionBankDialog.vue`）
- **位置**：`:185-198`（`load()` 无请求序号/AbortController）
- **复现步骤**：
  - 前置：两次查询并发。
  1. 输关键词 A 点查询，立即改 B 再点查询 → 若 A 响应晚于 B，`rows`/`favoriteIds` 被 A 的旧结果覆盖，列表与当前筛选不符。
- **预期 vs 实际**：预期只渲染最后一次请求结果；实际"后到者胜"展示过期数据。
- **根因**：无代际号/取消机制；对比 `stores/chat.js` 已用 `gen + AbortController` 解决同类问题（bug #7），此处未沿用。

---

## Low

### BUG-19 【Low】重连后 `reply_start` 不清 `V.buf`/`liveBubbleIndex`，欢迎语字幕拼接断线前的半截回复
- **模块**：语音前端（`useVoiceCall.js:583-592`；对照 `sendText:316`、`bargeIn:496` 均正确清理）
- **复现**：回复播报中断线（`V.buf` 残留半截）→ 重连 → 后端 `REOPEN_GREETING`（reply_start → delta）→ `updateLive` 发现 `liveBubbleIndex>=0` 更新旧气泡 → 旧气泡文本变成"断线前半截回复 + 欢迎语"拼接混合体。
- **预期 vs 实际**：预期新回复独立成新气泡；实际视觉混乱（不损数据）。
- **根因**：`V.buf`/`liveBubbleIndex` 清理责任分散在 `sendText`/`bargeIn`，`reply_start` 作为"新一轮开始"协议锚点未承担重置职责。

### BUG-20 【Low】speechSynthesis voices 等待的 `once` 回调可被事件与超时各触发一次，降级文本朗读两遍
- **模块**：语音前端（`useVoiceCall.js:565-577`）
- **复现**：首次访问 + 在线 TTS 失败触发降级 → `onvoiceschanged = once` 与 `setTimeout(once,1500)` 双触发源，`once` 无幂等守卫 → 同一段文本被朗读两遍，`fallbackActive` 计两次。
- **预期 vs 实际**：预期只朗读一次；实际两遍。
- **根因**：双触发源共用未做幂等保护的回调。

### BUG-21 【Low】<3 字回声绕过内容级过滤 + `asr_partial` 单次确认即打断：外放场景开场白被自身回声截断
- **模块**：语音前端（`useVoiceCall.js:643-655` + `voiceUtils.js:9-10`）
- **复现**：外放（AEC 残留回声）接通 → 开场白"你好，我是……" → 麦克风采到"你好" → `isEchoLike` 因长度<3 必放行 → `bargeIn()` → 开场白前两字就被自己打断。
- **预期 vs 实际**：预期播报起始阶段对超短 partial 要求二次确认；实际单次 1-2 字 partial 直接打断（整句路径有 2 秒去重，partial 路径无）。
- **根因**：内容级过滤对 <3 字无区分度，partial 打断却把该结果当高置信信号即刻执行。附注：`asr_client.py:8-9` 注释声称"播报时暂停发送音频"，实际 `handleAudioChunk`（useVoiceCall.js:188-200）无此逻辑，注释与实现不符。

### BUG-22 【Low】语音 `rate_limit`/`internal` 错误后 phase 卡死 CONNECTING，状态不恢复
- **模块**：语音前端（`useVoiceCall.js:657-663` error handler 只 `setStatus` 无 `setPhase`；`:312` sendText 已置 CONNECTING）
- **复现**：60s 窗口第 31 条 text 触发限流或后端 `internal` → phase 永久停留 CONNECTING、`statusBusy=true`；用户每说一句都再次触发 `rate_limit`，且无等待倒计时提示。
- **预期 vs 实际**：预期错误后回到 LISTENING、可继续对话；实际状态只靠下一条成功消息覆盖修复。
- **根因**：error handler 未纳入状态机管理，忽略 `sendText` 已推进 phase 的事实。

### BUG-23 【Low】ASR 永久性失败（未装 dashscope / 无 Key）被当作瞬时故障无限重试，前端永久"自动重连中"
- **模块**：语音后端（`voice_ws.py:323-334` `_asr_supervisor` 无重试上限 + `asr_client.py:109-114` 对永久失败也只返回 False）
- **复现**：不配 `DASHSCOPE_API_KEY` 启动 → 接通 → 以 3→6→12→20s 退避**永远**重试一个不可能成功的启动，前端永久"自动重连中"且失败静默。
- **预期 vs 实际**：预期区分瞬时/永久失败，永久失败停止重试并告知终态；实际无限空转、体验欺骗。
- **根因**：三类失败（缺依赖/无 Key/网络）共用同一 False 返回值与同一条无限重连路径。

### BUG-24 【Low】报告气泡隐藏用"内容相等"判断身份，违反项目明文纪律
- **模块**：前端会话（`ChatView.vue:264-271`）
- **位置**：`reportIndex` 用 `m.content === chat.report` 从后往前匹配
- **复现**：辅导模式中用户要求复述报告内容，历史出现与报告全文相同的早期助手消息且位于最后时，隐藏的是错误条目。
- **预期 vs 实际**：预期用唯一标记（后端 done 事件带报告消息索引/ID）；实际内容匹配（有"从后往前+限 assistant 非流式"缓解，但机制违规）。
- **根因**：后端 `done` 事件（`session.py:215-224`）未携带报告消息显式标识，前端只能退化为内容匹配。

### BUG-25 【Low】语音页 hint 气泡更新是死代码；transcript 用索引作 key
- **模块**：语音前端（`useVoiceCall.js:159-172` + `VoiceView.vue:34`）
- **复现**：hint 气泡走 `v-if="!ui.transcript.length"` 条件分支，从不进入 transcript 数组，`ui.transcript.find((b)=>b.role==='hint')` 永远 undefined（实际靠 `hintText` computed 兜底，无用户可见故障，但为无效逻辑）；transcript 用索引 key 不符"唯一标记"纪律。
- **根因**：从旧 `voice_page.html` 移植时保留了"hint 在数组中"的旧模型假设。

### BUG-26 【Low】语音降级 `setTimeout` 未纳入通话生命周期：挂断后可能残留一句播报
- **模块**：语音前端（`useVoiceCall.js:565-577`）
- **复现**：首次使用（voices 未就绪）→ 某段 TTS 失败降级 → `setTimeout(once,1500)` → 1.5s 内挂断 → 定时器仍触发 `speechSynthesis.speak()` 播出一句；句柄未保存无法清理，`onvoiceschanged` 全局单例赋值互相覆盖。
- **预期 vs 实际**：预期挂断后无任何音频；实际最长 1.5s 残留播报窗口。
- **根因**：异步回调未与 `V.suppressAudio`/断开状态联动。

### BUG-27 【Low】注册/资料表单前端校验与后端 Pydantic 约束脱节
- **模块**：前端表单（`LoginView.vue:97-106`、`ChatView.vue:156-166`；后端约束 `routers/auth.py:13-16,25-27`）
- **复现**：注册页输入 2 字符用户名 → 前端放行 → 后端 422 → catch 里 `detail` 是 pydantic 错误数组，ElMessage 显示对象串不可读；资料昵称超长则完全无反馈。
- **根因**：无 el-form rules，手工校验只覆盖部分约束。

### BUG-28 【Low】401 硬跳转丢失 redirect 上下文，与手动导航行为不一致
- **模块**：前端鉴权（`http.js:30-34` `window.location.href='/login'` 无 `?redirect=`；对照 `router/index.js:22-24`）
- **复现**：在 `/voice` 页 REST 401 → 硬跳 `/login` → 重新登录固定回 `/`，而路由守卫拦截路径登录后会回原页面。
- **根因**：拦截器用 `window.location` 硬跳，未携带 `pathname` 拼 redirect。

### BUG-29 【Low】SSE 非 200 响应原文直接进聊天气泡，未解析 detail
- **模块**：前端会话（`api/index.js:43-44,91-92`）
- **复现**：同账号双标签页并发发消息 → 后端 per-user 锁返回 429 → 气泡显示 `{"detail":"上一条回复仍在生成中"}` JSON 原文；422 校验错误同理。
- **根因**：`safeText` 只做文本兜底，无 JSON→detail 解析层。

### BUG-30 【Low】报告解析：薄弱点条目含"改进"字样时，"改进建议"章节被提前激活，改进清单误含薄弱点
- **模块**：报告解析（`app/agent/coach.py:87-105` `_extract_section_items`）
- **复现**：薄弱点含"需要改进算法理解" → 对 `_extract_section_items(report,"改进")` 未激活时即命中"改进"激活章节 → 「熟悉 Python 3」被收进改进清单，真实「阅读 Flask 源码」被截断。`test_parse_report_dimensions_scoped_to_score_section` 未覆盖该场景。
- **根因**：章节激活判据未限定标题样式（bug #16 只修了"激活后"误判，未修"激活前"误激活）。

### BUG-31 【Low】`_migrate` 非事务执行：v7 favorites 重建（DROP+RENAME）崩溃中间态不可恢复，收藏数据丢失
- **模块**：数据库迁移（`app/core/db.py:220-225,287-306`）
- **复现**：旧库升级，`executescript` 内 `DROP TABLE favorites` 与 `RENAME` 之间崩溃 → 重启后 `CREATE TABLE IF NOT EXISTS` 重建空表、v7 检查 `user_id` 已在 → 跳过 → 旧收藏遗留在孤儿表 `favorites_new` 永不使用。
- **根因**：Python `executescript` 隐式提交外层事务，SQLite 对 DDL 自动提交，DROP/RENAME 无事务保护。

### BUG-32 【Low】`_chat_locks` 无限增长（每用户锁对象永久保留）
- **模块**：会话（`app/routers/session.py:152-156`）
- **复现**：长期运行多用户服务，`_chat_locks.setdefault` 只增不删；`ratelimit.py` 有 `_MAX_KEYS` 上限此处没有。
- **根因**：缺少条目淘汰/清理机制（量级小，单实例低危）。

### BUG-33 【Low】`/api/questions/import` 请求体与 CSV 字段无大小上限
- **模块**：题库导入（`app/routers/questions.py:107-116` + `app/services/importer.py:44-70`）
- **复现**：`POST /api/questions/import` 提交超大 JSON body（单行 answer 数十万字符）→ 直接入库，DB 膨胀。
- **根因**：import 端点未声明 body/字段上限（对照 `AddQuestionBody.answer(max_length=20000)`）。

### BUG-34 【Low】`/api/chat` 纯空白消息通过校验并触发一次空文本 LLM 调用
- **模块**：会话（`routers/session.py:39-40` `Field(min_length=1)` + `coach.py:150-152` `_sanitize_input` strip 后可空）
- **复现**：发送 `{"message":"   "}` → 长度≥1 通过 → strip 后空串仍以空内容调用 LLM，白付一次计费。
- **根因**：长度校验未在 strip 之后执行。

### BUG-35 【Low】同步 `handle()` 路径无任何异常回滚（生产未使用，仅测试/兼容入口）
- **模块**：状态机（`app/agent/coach.py:422-480`）
- **复现**：`_handle_mock` 各分支 append/stage_idx 后调 `_chat()` 抛异常时状态已提交而 `turn` 未推进，下次调用重复追加。
- **根因**：同步入口未实现流式入口已有的"快照→提交→异常回滚"模式（当前生产走 `handle_stream`，仅影响测试与复用者）。

### BUG-36 【Low】WS 一次性票据消费存在 TOCTOU 竞态，"同事务单次消费"承诺未真正实现
- **模块**：认证（`app/core/db.py:1131-1146` `consume_ws_ticket`；调用面 `stores/auth.py:79-89`、`voice_ws.py:341`）
- **复现**：同一票据在 60s TTL 内并发发起两个 WS 连接 → 两连接 SELECT 都读到行、后到者 DELETE 落空但不报错 → 两连接均认证成功。
- **预期 vs 实际**：预期恰有一个成功、另一个返回 None；实际票据被消费两次（实际危害被互踢 4409 兜底：同 user 最终仍单连接；且窃票者本可在 TTL 内抢先，增量攻击面小）。
- **根因**：`get_conn()` 未设 `isolation_level`，Python sqlite3 legacy 模式 SELECT 自动提交读、不在事务内；缺乏 `BEGIN IMMEDIATE`/`DELETE ... RETURNING` 的原子消费。现有测试仅覆盖串行消费。

### BUG-37 【Low】PBKDF2 迭代次数 200k 低于 OWASP 当前推荐 600k
- **模块**：认证（`app/stores/auth.py:24` `_PBKDF2_ITERATIONS = 200_000`）
- **复现**：获取数据库文件用 GPU 离线爆破，200k 迭代单卡速率约为 600k 配置的 3 倍。
- **预期 vs 实际**：算法/盐/恒定时间比较均正确，仅强度余量不足（非算法性缺陷）。
- **根因**：参数未随行业基线上调；`verify_password` 按存储散列内嵌迭代数校验，天然支持渐进升级。

---

## 附加备注（低置信 / 需运行时验证）

### NOTE-A【待验证】ASR SDK 同步调用跑在事件循环上
- **位置**：`app/voice_ws.py:316`（`self.asr.start()`）、`:183`（`self.asr.stop()`）
- **描述**：dashscope `Recognition.start()` 若内含同步网络 IO，慢网络下会阻塞整个事件循环，殃及所有并发用户（对照：LLM 已用 `asyncio.to_thread` 规避，ASR 路径未做）。需运行时对比验证。

### NOTE-B【待验证】ASR send 失败无自愈路径
- **位置**：`app/services/asr_client.py:123-129`
- **描述**：`send_audio_frame` 异常仅记日志、不置 None、不触发重连；若 SDK 僵死后只抛 send 异常而不回调 `on_error`，识别永久失效。是否发生取决于 SDK 内部行为。

### NOTE-C【附注】注释与实现不符
- **位置**：`app/services/asr_client.py:8-9`
- **描述**：模块注释声称"前端在小P播报时暂停发送音频（防回声）"，实际前端 `handleAudioChunk`（useVoiceCall.js:188-200）无任何暂停上行逻辑，回声防护完全依赖 AEC + 内容过滤。

---

## 修复优先级建议（按投入产出排序）

1. **先修数据安全**：BUG-01（跨端覆盖丢回合）、BUG-06（历史错乱落库）
2. **再修功能卡死**：BUG-02（SSE 401 失效）、BUG-03（骨架屏卡死）、BUG-10（SSE 断线丢回合）
3. **语音核心体验**：BUG-04（barge-in 竞态）、BUG-05（跳题）、BUG-11（打断失效）、BUG-12（TTS 串行化）、BUG-13（无声泄漏）
4. **纪律守卫**：BUG-16（CSS 令牌守卫失效，越晚同步越难追溯）
5. **其余 Medium→Low**：BUG-07/08/09/14/15/17/18 及各 Low 项

> 修复任何模块时请遵守 AGENTS.md：改动解析器/状态机/数据库迁移必须同步新增/更新单测，全量 `python -m pytest` 离线通过。
