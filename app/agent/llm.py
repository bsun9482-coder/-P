"""DeepSeek LLM 调用封装（OpenAI 兼容接口）。

- 密钥从 .env 读取（DEEPSEEK_API_KEY），客户端懒加载；
- 失败自动重试（指数退避 + 抖动），支持流式输出（chat_stream）；
- 可选 response_format（如 json_object）用于结构化输出；
- 请求级日志：记录耗时与 token 用量，便于成本观测。
"""

import logging
import random
import time

from app.core import config
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)

logger = logging.getLogger("interview_coach.llm")

_client: OpenAI | None = None


def _is_retryable(e: Exception) -> bool:
    """仅对瞬时错误重试；4xx 客户端错误（key 失效/参数错/超限）重试也无济于事，
    只会白等退避时间（400/401 被误重试 3 次，累计 ~15s）。

    可重试：限流 429、超时、连接错误、服务端 5xx。
    """
    if isinstance(
        e, (RateLimitError, APITimeoutError, APIConnectionError, ConnectionError, TimeoutError)
    ):
        return True
    if isinstance(e, APIStatusError):
        return 500 <= getattr(e, "status_code", 0) < 600
    return False


def is_api_key_configured() -> bool:
    """检查 API Key 是否像真实密钥（排除占位符/乱码被误当成已配置）。"""
    key = (config.DEEPSEEK_API_KEY or "").strip()
    return bool(key) and len(key) >= 20 and "你的key" not in key and not key.startswith("sk-你的")


def get_client() -> OpenAI:
    global _client
    if _client is None:
        if not is_api_key_configured():
            raise RuntimeError(
                "未配置有效的 DEEPSEEK_API_KEY：请编辑项目根目录的 .env 文件，"
                "填入真实密钥（在 platform.deepseek.com 获取）后重启服务。"
            )
        _client = OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
            timeout=config.LLM_TIMEOUT_SECONDS,
        )
    return _client


def _retry_wait(attempt: int) -> float:
    """指数退避 + 随机抖动，最多等 30s。"""
    return min(2**attempt, 30) + random.uniform(0, 0.5)


def _build_kwargs(
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    response_format: dict | None,
    model: str | None = None,
) -> dict:
    kwargs = {
        "model": model or config.DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    return kwargs


def chat(
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 2048,
    max_retries: int | None = None,
    response_format: dict | None = None,
    model: str | None = None,
) -> str:
    """发送对话消息，失败自动重试，返回完整回复文本。"""
    max_retries = max_retries if max_retries is not None else config.LLM_MAX_RETRIES
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            start = time.perf_counter()
            resp = get_client().chat.completions.create(
                **_build_kwargs(messages, temperature, max_tokens, response_format, model)
            )
            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            logger.info(
                "llm 调用成功 attempt=%s/%s 延迟=%.2fs usage=%s",
                attempt + 1,
                max_retries,
                time.perf_counter() - start,
                usage,
            )
            return text
        except Exception as e:
            if not _is_retryable(e):
                raise
            last_err = e
            wait = _retry_wait(attempt)
            logger.warning(
                "llm 调用失败（%s），%.1fs 后重试 (%s/%s)",
                type(e).__name__,
                wait,
                attempt + 1,
                max_retries,
            )
            time.sleep(wait)
    if last_err is None:
        last_err = RuntimeError("LLM 调用失败（未发生重试）")
    raise last_err


def chat_stream(
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 2048,
    max_retries: int | None = None,
    response_format: dict | None = None,
    model: str | None = None,
):
    """流式对话：逐段 yield 回复文本。

    注意：一旦开始产出内容便不再重试，避免输出重复片段。
    """
    max_retries = max_retries if max_retries is not None else config.LLM_MAX_RETRIES
    last_err: Exception | None = None
    started = False
    for attempt in range(max_retries):
        try:
            kwargs = _build_kwargs(messages, temperature, max_tokens, response_format, model)
            resp = get_client().chat.completions.create(stream=True, **kwargs)
            for chunk in resp:
                delta = chunk.choices[0].delta.content
                if delta:
                    started = True
                    yield delta
            return
        except Exception as e:
            if not _is_retryable(e):
                raise
            last_err = e
            if started:
                raise
            wait = _retry_wait(attempt)
            logger.warning(
                "llm 流式调用失败（%s），%.1fs 后重试 (%s/%s)",
                type(e).__name__,
                wait,
                attempt + 1,
                max_retries,
            )
            time.sleep(wait)
    if last_err is None:
        last_err = RuntimeError("LLM 流式调用失败（未发生重试）")
    raise last_err
