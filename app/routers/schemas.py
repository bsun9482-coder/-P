"""REST 接口的响应模型（Pydantic）。

**为什么需要这个文件**：此前所有路由的返回注解都是裸 `dict`，OpenAPI 里只能
生成"空对象"，前端对接只能翻后端源码猜字段；字段改名/删除也不会有人发现。

**注意（重要）**：FastAPI 的响应模型不只是"文档声明"——它会**按模型过滤和校验
响应体**：模型里没有的字段会被丢掉，类型不符会报校验错误。因此：

- 模型字段必须与 `app/routers/*.py`、`app/stores/*.py` 实际返回的键**完全一致**；
- 删/改字段前先搜本文件与前端引用，字段顺序也尽量与返回 dict 保持一致
  （保证 JSON 键序不变）；
- 形状随分支变化的接口（如 `GET /api/session`）用可选字段 +
  `response_model_exclude_unset=True`，未返回的键不会凭空补成 `null`。

契约由 `tests/test_api_response_contract.py` 守卫。
"""

from pydantic import BaseModel

# ------------------------------------------------------------------ 通用


class OkOut(BaseModel):
    """通用成功响应（只表示"操作已执行"，无附加数据）。"""

    ok: bool


class HealthOut(BaseModel):
    """就绪探针结果。"""

    status: str


class VoiceConfigOut(BaseModel):
    """前端语音页所需运行时配置（替代旧 HTML 模板注入）。"""

    vad_threshold: float
    vad_hits: int
    vad_quiet_frames: int
    vad_noise_margin: float
    tts: str


# ------------------------------------------------------------------ 账号


class PublicUserOut(BaseModel):
    """可下发给前端的用户公开信息（不含密码散列）。"""

    id: int
    username: str
    nickname: str
    persona: str


class TokenOut(BaseModel):
    """注册/登录成功：令牌 + 用户信息。"""

    token: str
    user: PublicUserOut


class WsTicketOut(BaseModel):
    """语音 WS 一次性连接票据（短时、单次消费）。"""

    ticket: str


# ------------------------------------------------------------------ 题库


class QuestionOut(BaseModel):
    """题目条目（`content`/`answer`/`url` 允许为空）。"""

    id: int
    source: str
    source_label: str
    title: str
    content: str | None = None
    answer: str | None = None
    tags: list[str]
    difficulty: str
    company: str
    url: str | None = None


class QuestionListOut(BaseModel):
    """题库浏览结果：题目列表 + 当前用户收藏 id 集合。"""

    items: list[QuestionOut]
    favorite_ids: list[int]


class SourceCountOut(BaseModel):
    """题目来源及其数量。"""

    key: str
    label: str
    count: int


class TagCountOut(BaseModel):
    """标签及其数量。"""

    name: str
    count: int


class QuestionMetaOut(BaseModel):
    """题库筛选元数据。"""

    sources: list[SourceCountOut]
    companies: list[str]
    tags: list[TagCountOut]


class ImportStatsOut(BaseModel):
    """CSV 导入统计。"""

    new: int
    skipped: int
    rows: int


class FavoriteIdsOut(BaseModel):
    """当前用户收藏的题目 id 列表。"""

    ids: list[int]


# ------------------------------------------------------------------ 会话


class DimensionOut(BaseModel):
    """报告中的单个评分维度。"""

    label: str
    score: int


class ReportDataOut(BaseModel):
    """结构化报告数据（来自 `app.agent.coach.parse_report`）。"""

    score: int | None = None
    dimensions: list[DimensionOut]
    weak_points: list[str]
    improvements: list[str]


class SessionStartOut(BaseModel):
    """启动新会话的返回。"""

    ok: bool
    session_id: int
    mode: str
    history: list[list[str]]
    finished: bool


class SessionStateOut(BaseModel):
    """当前会话状态（前端刷新/恢复用）。

    无活跃会话时只返回 `active`/`mode`/`history`/`finished`/`report` 五个键，
    其余字段不出现 —— 路由需配 `response_model_exclude_unset=True`。
    """

    active: bool
    session_id: int | None = None
    mode: str
    history: list[list[str]]
    finished: bool
    report: str | None = None
    report_data: ReportDataOut | None = None
    persona: str | None = None
    custom_questions: list[str] | None = None
    job_title: str | None = None


class HistoryItemOut(BaseModel):
    """历史列表单条（含进行中与已完成）。"""

    id: int
    mode: str
    job_title: str
    source: str
    persona: str
    started_at: str
    score: int | None = None
    report: str | None = None
    weak_points: str | None = None
    status: str
    active: bool


class HistoryListOut(BaseModel):
    """某用户的面试历史列表。"""

    items: list[HistoryItemOut]


# ------------------------------------------------------------------ 定制面试


class CustomStatusOut(BaseModel):
    """该用户是否有待执行的语音定制面试。"""

    ready: bool
    job_title: str
