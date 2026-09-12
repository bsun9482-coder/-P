# 面试官小P - 开发约定

基于 DeepSeek 的 Python/后端面试模拟与辅导 Agent：Vue3 前端（多用户账号）+
FastAPI 统一后端（REST + SSE + 语音 WebSocket）+ 定时爬取题库（SQLite）。

## 常用命令

```bash
# 安装（含开发依赖：pytest / httpx / ruff）
pip install -e ".[dev]"

# 测试
python -m pytest

# 静态检查与格式化
ruff check .
ruff format .

# 启动统一服务（Vue3 前端 + REST + 语音，单端口 8765）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# 前端（Vue3 + Vite）——启动前需构建，产物供后端托管
cd frontend
npm install
npm run dev        # 开发模式（5173，代理 /api /ws 到 8765）
npm run build      # 生产构建 → frontend/dist
cd ..

# 手动抓取题库
python -m app.crawler.run
```

## 目录约定

- `app/`：应用包。
  - `main.py`：**统一 FastAPI 服务**——挂载 REST 路由、语音 WebSocket（按
    用户认证）、托管 `frontend/dist`（SPA history 回退）。
  - `voice_ws.py`：语音 WebSocket 协议处理（生命周期、互踢、ASR、回复生成）。
  - `routers/`：REST 路由层（`auth` 认证 / `session` 会话与 SSE 聊天 / `questions`
    题库与收藏 / `custom` 定制面试）。
  - `agent/`：领域层——会话状态机（`coach.py` 支持 to_dict/from_dict 序列化）与 LLM 封装。
  - `crawler/`：采集层，数据源适配器。
  - `core/`：基础设施——`config` / `db` / `ratelimit` / `scheduler`。
  - `services/`：业务服务——`tts` / `asr_client` / `prompts` / `importer`。
  - `stores/`：数据访问层——`session_store`（会话持久化）、`voice_store`（按用户
    定制面试）、`auth`（pbkdf2 + 令牌）为多用户数据层。
  - `ui/`：仅保留静态资源 `assets/`（头像）。
- `frontend/`：Vue3 前端工程（Vite + Element Plus）。`src/composables/voice/`
  是语音通话引擎（从旧 `voice_page.html` 移植）。构建产物 `frontend/dist` 由后端托管，
  **修改前端后需 `npm run build` 并重启/刷新**。
- `scripts/`：启动脚本（`start.bat` Windows / `start.sh` macOS·Linux），自动构建前端。
- `deploy/`：容器部署（多阶段 `Dockerfile` + `docker-compose.yml`）。
- `tests/`：根级测试目录（pytest 默认 `testpaths`），通过
  `[tool.pytest.ini_options] pythonpath = ["."]` 导入 `app.*` 与根级模块。
- `data/`：运行时数据（SQLite、日志、锁），已 gitignore，不要提交。

## 约定与注意事项

- 代码注释、文档使用中文；源码 UTF-8 无 BOM、LF 换行（`.editorconfig` 已声明）。
- `scripts/start.bat` 为 **UTF-8 + CRLF** 编码（`chcp 65001`），修改时保持该编码与换行。
- 依赖只在 `pyproject.toml` 中声明（`requirements.txt` 已移除），新增依赖同步
  更新 `[project.dependencies]` 与 `[project.optional-dependencies].dev`。
- 前端依赖声明在 `frontend/package.json`；国内网络慢时 `frontend/.npmrc` 已指向
  npmmirror 镜像。
- 测试必须可离线运行：LLM、网络、edge-tts 全部 mock；不要依赖真实 API Key。
- 运行时通过环境变量注入密钥（`.env` 不入库），参见 `.env.example`。
- 数据库迁移通过 `PRAGMA user_version`（当前版本 8，令牌哈希化 + WS 一次性票据表）；
  改 schema 需在 `app/core/db.py` 的 `_migrate` 追加迁移步骤并升级版本号。
- 登录令牌经 `Authorization: Bearer <token>`（REST）传递；WebSocket 无法带请求头，
  前端先 `POST /api/auth/ws-ticket`（Bearer）换取一次性短时票据，WS URL 只携带
  `?ticket=<票据>`——长效令牌禁止出现在 URL，落库值一律为 SHA-256 哈希；
  新增受保护接口用 `auth.CurrentUser` 依赖解析当前用户。

## 工程纪律

### 设计令牌单一事实来源

- 品牌色只定义在 `frontend/src/styles/main.css` 的 `:root` 中，必须是具体值，
  禁止自引用（`--x: var(--x)`）或引用其他令牌拼出间接循环。
- 组件/页面一律引用 `var(--brand)`、`var(--brand-2)`、`var(--brand-light)`、
  `var(--brand-rgb)`，不得写死品牌色值；新增品牌色先加令牌，再引用。
- `tests/test_css_discipline.py` 自动守卫上述规则，改样式后必须保持通过。

### 解析与身份判断

- 文本/报告解析只放后端 `app/agent/coach.py`，必须限定章节作用域
  （如维度分只在【总分】之后、薄弱点/改进之前抽取），并配离线单测；
  前端不得复制一份解析正则。
- 前端历史/列表元素禁止用内容相等判断身份；隐藏、去重、替换必须针对
  唯一条目（索引或显式标记）。

### 提交纪律

- 一个需求一个提交，commit message 用 `type(scope): 中文描述`。
- 禁止把同一改动重复提交为多个 SHA；同一 PR 保持线性历史。
- 推送前先 `git fetch` 对比，历史分叉用 rebase 而不是 merge。
- **分支名使用平铺连字符名**（如 `refactor-layered-structure`），不使用含 `/` 的层级名。
  部分开发环境下层级分支名的 ref 会**静默写入失败**：`git commit` 报告成功，但
  `.git/refs/heads/<a>/<b>` 未创建、HEAD 悬空（症状：`git status` 显示 `No commits yet`
  且所有文件变 `A`、`git log` 为空）。此时**提交对象与文件内容均未丢失**，恢复方式：
  `git update-ref refs/heads/<平铺名> <sha>` + `git symbolic-ref HEAD refs/heads/<平铺名>`。
  远端分支名可与本地不同，推送时用 `git push origin <本地平铺名>:<远端名>` 映射。

### 测试纪律

- `python -m pytest` 必须全量离线通过才能提交（LLM、网络、edge-tts 全 mock）。
- 改动解析器、状态机、数据库迁移时必须同步新增/更新单测。
