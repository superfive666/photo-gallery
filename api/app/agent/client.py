"""OpenAI-compatible 的 LLM 客户端。

供应商三元组全部来自环境变量（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），
私有模型、Claude（经兼容网关）、OpenAI 互换只是换配置，代码零改动。

**不假设模型支持 function calling / JSON mode**：结构化输出的约束写在 prompt 里，
抽取与校验在 runner 侧做（失败把校验错误喂回去重试一次）—— 换任何兼容模型都不挑能力。
"""

from __future__ import annotations

import httpx

from gallery_core.config import Settings
from gallery_core.logging import get_logger

log = get_logger(__name__)


class LlmError(RuntimeError):
    """LLM 不可用或返回了非预期结果。"""


def llm_configured(settings: Settings) -> bool:
    return bool(settings.llm_base_url and settings.llm_model)


async def chat(settings: Settings, system: str, user: str) -> str:
    """一次 /chat/completions 调用，返回助手文本。只在固定的状态机节点被调用。"""
    if not llm_configured(settings):
        raise LlmError("LLM 未配置（LLM_BASE_URL / LLM_MODEL）")

    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    headers = {}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    last_error: Exception | None = None
    for attempt in range(settings.llm_max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                resp = await client.post(
                    settings.llm_base_url.rstrip("/") + "/chat/completions",
                    json=payload,
                    headers=headers,
                )
            if resp.status_code != 200:
                raise LlmError(f"LLM 返回 {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise LlmError("LLM 返回了空内容")
            # 只记耗费与轮次，绝不记剧本内容
            usage = data.get("usage") or {}
            log.info(
                "llm_chat_done",
                model=settings.llm_model,
                attempt=attempt,
                total_tokens=usage.get("total_tokens"),
            )
            return content
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            last_error = exc
            log.warning("llm_chat_retry", attempt=attempt, error_type=type(exc).__name__)
    raise LlmError(f"LLM 调用失败: {type(last_error).__name__}: {last_error}")
