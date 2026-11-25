"""Validated model calls for survey generation."""

import asyncio
import json
import logging
import re
from contextvars import ContextVar

from .llm import allow_model_failover, chat_completion

logger = logging.getLogger(__name__)
model_slots = ContextVar("survey_model_slots", default=None)


async def validated_json(config, instructions, payload, validate, *, timeout=60, attempts=3):
    if config is None:
        raise ValueError("未配置对应的 AI 模型")
    slots = model_slots.get() or asyncio.Semaphore(1)
    base = [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    feedback = ""
    with allow_model_failover():
        for attempt in range(attempts):
            messages = base if not feedback else base + [{"role": "user", "content": feedback}]
            try:
                async with slots:
                    raw = await chat_completion(config, messages, timeout=timeout, retries=0)
            except Exception as exc:
                feedback = "上次调用失败，请按原要求返回 JSON。"
                logger.info("Survey model call %d failed: %s", attempt + 1, type(exc).__name__)
                continue
            try:
                normalized = re.sub(r"\A```(?:json)?\s*|\s*```\Z", "", raw.strip(), flags=re.IGNORECASE)
                return validate(json.loads(normalized))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                feedback = f"校验发现问题：{str(exc)[:1200]}。请修正并返回完整 JSON。"
    raise ValueError(feedback or "AI 输出未通过校验")
