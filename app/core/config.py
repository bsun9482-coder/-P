"""全局配置：从 .env 加载密钥与参数，统一提供路径常量。"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（config.py 位于 app/core/，向上两级为 app/，再上一级为项目根）
BASE_DIR = Path(__file__).resolve().parent.parent.parent
# override=True：确保项目 .env 优先于系统环境变量（避免旧的环境变量遮蔽新密钥）
load_dotenv(BASE_DIR / ".env", override=True)

# ---- DeepSeek ----
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
# 总结报告使用的模型（可选）：留空则用 DEEPSEEK_MODEL；如 deepseek-reasoner 更深入但更慢更贵
REPORT_MODEL = os.getenv("REPORT_MODEL", "").strip()
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))

# ---- 数据 ----
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "questions.db"
DB_TIMEOUT_SECONDS = int(os.getenv("DB_TIMEOUT_SECONDS", "10"))

# ---- 定时爬取 ----
CRAWL_TIME = os.getenv("CRAWL_TIME", "02:00").strip()  # 每天几点抓取
CRAWL_INTERVAL_HOURS = int(os.getenv("CRAWL_INTERVAL_HOURS", "24"))  # 间隔小时，0=仅启动时抓一次
SCHEDULER_TZ = os.getenv("SCHEDULER_TZ", "Asia/Shanghai").strip()

# ---- 爬虫 ----
CRAWL_PAGES_PER_CATEGORY = int(os.getenv("CRAWL_PAGES_PER_CATEGORY", "15"))
CRAWL_REQUEST_DELAY = float(os.getenv("CRAWL_REQUEST_DELAY", "0.3"))
CRAWL_WORKERS = int(os.getenv("CRAWL_WORKERS", "3"))
LEETCODE_CACHE_HOURS = int(os.getenv("LEETCODE_CACHE_HOURS", "72"))
# 力扣（算法题）默认不抓取：算法题仍属模拟面试第 2 阶段，只是抓取端默认关闭，
# 需要时置 CRAWL_LEETCODE=1 开启（开启后才有 算法-* 标签的题入库）
CRAWL_LEETCODE = os.getenv("CRAWL_LEETCODE", "0").strip().lower() in ("1", "true", "yes")

# ---- 懒加载补抓（定制面试零命中时按需抓取）----
LAZY_CRAWL_PAGES = int(os.getenv("LAZY_CRAWL_PAGES", "3"))  # 每分类抓列表页数
LAZY_CRAWL_LIMIT = int(os.getenv("LAZY_CRAWL_LIMIT", "40"))  # 每分类最多入库条数
LAZY_CRAWL_TIMEOUT = float(os.getenv("LAZY_CRAWL_TIMEOUT", "30"))  # 补抓总超时（秒）

# ---- 数据清洗（clean_status 状态机：raw→rule_cleaned→semantic_cleaned→ready）----
CLEAN_RULE_VERSION = os.getenv(
    "CLEAN_RULE_VERSION", "2026.08.17"
).strip()  # 清洗规则版本号，升级后旧版本数据重洗
CLEAN_SEMANTIC_BATCH_LIMIT = int(
    os.getenv("CLEAN_SEMANTIC_BATCH_LIMIT", "100")
)  # 单次语义清洗最多打标条数（控制 LLM 成本）

# ---- 语音通话服务 ----
VOICE_HOST = os.getenv("VOICE_HOST", "127.0.0.1").strip()
VOICE_PORT = int(os.getenv("VOICE_PORT", "8765"))
VOICE_NAME = os.getenv("VOICE_NAME", "zh-CN-XiaoxiaoNeural").strip()
VOICE_RATE = os.getenv("VOICE_RATE", "+0%").strip()
VOICE_PITCH = os.getenv("VOICE_PITCH", "+2Hz").strip()
VOICE_VAD_THRESHOLD = float(
    os.getenv("VOICE_VAD_THRESHOLD", "0.08")
)  # 打断音量阈值（越高越不易误触发）
VOICE_VAD_HITS = int(
    os.getenv("VOICE_VAD_HITS", "5")
)  # 连续超阈值帧数（每帧约60ms），需持续说话才打断
VOICE_VAD_QUIET_FRAMES = int(
    os.getenv("VOICE_VAD_QUIET_FRAMES", "3")
)  # 武装前需安静的帧数（防自己话尾误打断）
VOICE_VAD_NOISE_MARGIN = float(
    os.getenv("VOICE_VAD_NOISE_MARGIN", "1.6")
)  # 自适应阈值 = 环境底噪 × 系数
VOICE_TTS = (
    os.getenv("VOICE_TTS", "edge").strip().lower()
)  # edge=微软edge-tts在线神经语音 / cosyvoice=阿里云百炼CosyVoice / local=浏览器本地语音

# ---- 阿里云百炼 CosyVoice（VOICE_TTS=cosyvoice 时使用）----
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "").strip()
# ---- 阿里云百炼 实时语音识别（Paraformer，替代 Chrome 内置 SpeechRecognition）----
ASR_MODEL = os.getenv("ASR_MODEL", "paraformer-realtime-v2").strip()
ASR_SAMPLE_RATE = int(os.getenv("ASR_SAMPLE_RATE", "16000"))
COSYVOICE_MODEL = os.getenv("COSYVOICE_MODEL", "cosyvoice-v2").strip()
# 龙小淳 v2：温暖甜美的女声；其他可用音色见百炼文档音色列表
COSYVOICE_VOICE = os.getenv("COSYVOICE_VOICE", "longxiaochun_v2").strip()
COSYVOICE_FORMAT = os.getenv("COSYVOICE_FORMAT", "mp3").strip().lower()
COSYVOICE_SAMPLE_RATE = int(os.getenv("COSYVOICE_SAMPLE_RATE", "24000"))
COSYVOICE_RATE = float(os.getenv("COSYVOICE_RATE", "1.0"))
COSYVOICE_PITCH = float(os.getenv("COSYVOICE_PITCH", "1.0"))

# ---- 统一 Web 服务（Vue3 前端 + REST + 语音，单端口）----
# 多用户改造后 Streamlit 退役，FastAPI 同时托管静态前端与所有 API。
# 兼容旧配置：新键 APP_PORT 优先，缺省回退 VOICE_PORT。
APP_HOST = os.getenv("APP_HOST", os.getenv("VOICE_HOST", "127.0.0.1")).strip()
APP_PORT = int(os.getenv("APP_PORT", os.getenv("VOICE_PORT", "8765")))

# ---- 登录令牌有效期（天）----
TOKEN_TTL_DAYS = int(os.getenv("TOKEN_TTL_DAYS", "30"))

# ---- 语音 WS 一次性票据有效期（秒）：URL 只出现短时票据，长效令牌不出 Bearer 头 ----
WS_TICKET_TTL_SECONDS = int(os.getenv("WS_TICKET_TTL_SECONDS", "60"))

# ---- 语音 WS 文本消息限流（按用户滑动窗口，防单连接刷消息烧 LLM 余额）----
VOICE_TEXT_RATE_LIMIT = int(os.getenv("VOICE_TEXT_RATE_LIMIT", "30"))
VOICE_TEXT_RATE_WINDOW = float(os.getenv("VOICE_TEXT_RATE_WINDOW", "60"))


def ensure_data_dir() -> None:
    """确保数据目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
