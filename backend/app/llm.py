"""OpenAI 兼容协议的模型调用封装。"""
import asyncio
import json
import logging
import time

import httpx

from .config import settings
from .models import ModelConfig
from .security import decrypt_key
from .tls import is_certificate_verification_error, llm_ssl_context

logger = logging.getLogger(__name__)

SAMPLING_PARAMS = [
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "repetition_penalty",
]

# 限流或服务端临时故障才值得换连接重试；4xx 配置类错误重试无意义
_RETRIABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# 客户端/配置类请求错误（地址缺协议、URL 非法等），重试无意义
_NON_RETRIABLE_REQUEST_ERRORS = (
    httpx.UnsupportedProtocol,
    httpx.InvalidURL,
    httpx.LocalProtocolError,
)

# 响应体内报错的可重试特征（限流/过载/临时不可用）
_RETRIABLE_ERROR_HINTS = (
    "rate_limit", "ratelimit", "throttl", "overload", "busy",
    "temporarily", "timeout", "unavailable", "retry",
    "upstream http/2 stream failed", "upstream stream failed",
    "限流", "繁忙", "过载", "超时", "饱和", "负载", "稍候", "稍后",
)

# 输入超出模型上下文窗口的特征（不可重试，由上层压缩历史后重试）
_CONTEXT_OVERFLOW_HINTS = (
    "context length", "context_length", "maximum context", "max context",
    "context window", "maximum number of tokens", "too many tokens",
    "reduce the length", "sequence length", "input is too long",
    "上下文长度", "最大上下文", "输入过长", "超出上下文",
)


def _looks_like_context_overflow(text: str) -> bool:
    haystack = (text or "").lower()
    return any(hint in haystack for hint in _CONTEXT_OVERFLOW_HINTS)


class ChatDeadlineExceeded(httpx.TimeoutException):
    """整体限时内模型一直在输出但没生成完，原样重试只会再超时一次。"""


class ChatProviderError(ValueError):
    """模型服务在响应体内返回的业务错误（限流、过载、上下文超长等）。"""

    def __init__(self, message: str, retriable: bool = False):
        super().__init__(message)
        self.retriable = retriable


class ChatContextOverflow(ChatProviderError):
    """输入超出模型上下文窗口（不可重试；上层压缩历史后重试该轮请求）。"""


def enabled_params(cfg: ModelConfig) -> dict:
    """只把勾选了的采样参数加入请求。"""
    try:
        raw = json.loads(cfg.params_json or "{}")
    except json.JSONDecodeError:
        raw = {}
    params = {}
    for name in SAMPLING_PARAMS:
        item = raw.get(name)
        if isinstance(item, dict) and item.get("enabled") and item.get("value") is not None:
            value = item["value"]
            if name == "top_k":
                # 配置接口统一以 float 存储参数，但服务端的 top_k 字段是整数。
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not float(value).is_integer()
                ):
                    raise ValueError("top_k 必须为整数")
                value = int(value)
            params[name] = value
    return params


def _message_text(message: dict) -> str:
    """提取 OpenAI 兼容响应里可用的助手文本。

    某些推理型服务会把最终文本放在 `reasoning_content`，而不是 `content`。
    这里优先保留真正可执行的内容，再向下兜底其他可读字段。
    """
    for key in ("content", "reasoning_content", "refusal"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _provider_error(chunk) -> "ChatProviderError | None":
    """识别响应体里夹带的错误（HTTP 200 但 body/chunk 里是 error 字段）。"""
    if not isinstance(chunk, dict):
        return None
    err = chunk.get("error")
    if err is None or err is False or err == {} or err == "":
        return None
    status = None
    code = ""
    if isinstance(err, dict):
        message = str(err.get("message") or err.get("msg") or "").strip()
        if not message:
            message = json.dumps(err, ensure_ascii=False)
        status = err.get("status")
        code = str(err.get("code") or err.get("type") or "")
    else:
        message = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)
        message = str(message).strip()
    if not message:
        return None
    if len(message) > 300:
        message = message[:300] + "……"
    retriable = False
    if isinstance(status, (int, float)):
        retriable = status == 429 or status >= 500
    elif isinstance(status, str) and status:
        retriable = status == "429" or status.startswith("5")
    if _looks_like_context_overflow(f"{code} {message}"):
        return ChatContextOverflow(f"模型服务返回错误：{message}", retriable=False)
    if not retriable:
        haystack = f"{code} {message}".lower()
        retriable = any(hint in haystack for hint in _RETRIABLE_ERROR_HINTS)
    return ChatProviderError(f"模型服务返回错误：{message}", retriable=retriable)


def _extract_message(body) -> str:
    """从整包 JSON 响应里提取助手文本；先检查体内 error，再容忍异形结构。"""
    error = _provider_error(body)
    if error is not None:
        raise error
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"模型服务返回了无法解析的响应：{exc}") from exc
    return _message_text(message if isinstance(message, dict) else {})


async def _sse_lines(
    resp: httpx.Response,
    first_wait: float,
    idle_wait: float,
    deadline: float,
    timeout: float,
):
    """按 SSE 规范切行（只认 LF/CRLF），并对等待数据的两类停滞限时。

    不能用 httpx 的 aiter_lines：它按 str.splitlines 切分，会把 JSON 字符串
    里合法的 U+2028/U+2029/\\x85 等字符误当行尾，悄悄丢掉对应数据块。
    first_wait 限制首个字节的到达时间（覆盖本地慢模型的长 prefill），
    idle_wait 限制之后相邻数据块的间隔；两者都受整体限时（deadline）约束，
    且每个数据块到达时都会检查整体限时，防止无换行的字节滴灌饿死超时。
    """
    buffer = b""
    lines: list[str] = []
    seen_data = False
    iterator = resp.aiter_bytes().__aiter__()
    while True:
        if lines:
            yield lines.pop(0)
            continue
        wait = idle_wait if seen_data else first_wait
        wait = min(wait, max(0.05, deadline - time.monotonic()))
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=wait)
        except StopAsyncIteration:
            if buffer.strip():
                yield buffer.rstrip(b"\r").decode("utf-8", "replace")
            return
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise ChatDeadlineExceeded(
                    f"模型持续生成但超过单次限时 {timeout:g} 秒仍未完成"
                ) from None
            if seen_data:
                raise httpx.ReadTimeout(
                    f"模型服务响应停滞超过 {wait:g} 秒，连接判定已断开"
                ) from None
            raise httpx.ReadTimeout(
                f"模型服务超过 {wait:g} 秒未返回任何数据"
            ) from None
        if time.monotonic() > deadline:
            raise ChatDeadlineExceeded(
                f"模型持续生成但超过单次限时 {timeout:g} 秒仍未完成"
            )
        seen_data = True
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            lines.append(raw.rstrip(b"\r").decode("utf-8", "replace"))


async def _chat_completion_stream(
    cfg: ModelConfig, messages: list[dict], timeout: float
) -> str:
    """发起一次流式补全请求，拼回完整文本。

    流式让网关/代理在长时间生成期间持续有数据可转发，避免长请求被中间层
    静默掐断；等待响应、首个数据、相邻数据块分别限时，停滞判定为连接断开
    并交给上层重试；整体限时（timeout）在数据仍然到达时也生效：模型本身
    太慢就不值得重试。
    """
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload = {"model": cfg.model, "messages": messages, "stream": True}
    payload.update(enabled_params(cfg))
    headers = {"Authorization": f"Bearer {decrypt_key(cfg.api_key_enc)}"}
    # read 用整体限时兜底（永不先于上面的停滞限时触发）；停滞判定由 _sse_lines 负责
    limits = httpx.Timeout(
        connect=min(30.0, timeout),
        read=timeout,
        write=min(120.0, timeout),
        pool=min(30.0, timeout),
    )
    deadline = time.monotonic() + timeout
    first_wait = min(settings.LLM_FIRST_CHUNK_TIMEOUT, timeout)
    idle_wait = min(settings.LLM_STREAM_IDLE_TIMEOUT, timeout)
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    refusal_parts: list[str] = []
    raw_lines: list[str] = []
    saw_sse = False
    done_seen = False
    async with httpx.AsyncClient(timeout=limits, verify=llm_ssl_context(),
                                 trust_env=settings.LLM_TRUST_ENV) as client:
        stream_cm = client.stream("POST", url, json=payload, headers=headers)
        header_wait = min(first_wait, max(0.05, deadline - time.monotonic()))
        try:
            resp = await asyncio.wait_for(stream_cm.__aenter__(), timeout=header_wait)
        except TimeoutError:
            # __aenter__ 未完成，不能再调 __aexit__；连接清理由外层 AsyncClient 负责
            raise httpx.ReadTimeout(
                f"模型服务超过 {header_wait:g} 秒未返回响应"
            ) from None
        try:
            if resp.status_code >= 300:
                error_wait = min(idle_wait, max(0.05, deadline - time.monotonic()))
                try:
                    await asyncio.wait_for(resp.aread(), timeout=error_wait)
                except TimeoutError:
                    raise httpx.ReadTimeout("模型服务返回错误响应时连接停滞") from None
                resp.raise_for_status()
            async for line in _sse_lines(resp, first_wait, idle_wait, deadline, timeout):
                line = line.strip()
                if not line:
                    continue
                if not line.startswith("data:"):
                    raw_lines.append(line)
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    done_seen = True
                    break
                saw_sse = True
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                error = _provider_error(chunk)
                if error is not None:
                    raise error
                choices = chunk.get("choices")
                if not isinstance(choices, list):
                    continue
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or choice.get("message") or {}
                    if not isinstance(delta, dict):
                        continue
                    for key, parts in (
                        ("content", content_parts),
                        ("reasoning_content", reasoning_parts),
                        ("refusal", refusal_parts),
                    ):
                        value = delta.get(key)
                        if isinstance(value, str) and value:
                            parts.append(value)
        finally:
            await stream_cm.__aexit__(None, None, None)
    if not saw_sse and raw_lines:
        # 服务方忽略 stream 参数、直接返回了完整 JSON
        try:
            body = json.loads("\n".join(raw_lines))
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型服务返回了无法解析的响应：{exc}") from exc
        return _extract_message(body)
    if saw_sse and not done_seen:
        # 连接在生成途中被断开：截断的正文一旦被采信就是静默的数据损坏
        raise ChatProviderError(
            "模型服务在生成中途结束了响应（未收到 [DONE] 结束标记），内容可能被截断",
            retriable=True,
        )
    for parts in (content_parts, reasoning_parts, refusal_parts):
        text = "".join(parts).strip()
        if text:
            return text
    return ""


async def _chat_completion_plain(
    cfg: ModelConfig, messages: list[dict], timeout: float
) -> str:
    """整包请求兜底：个别 OpenAI 兼容服务不接受 stream:true。"""
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload = {"model": cfg.model, "messages": messages}
    payload.update(enabled_params(cfg))
    headers = {"Authorization": f"Bearer {decrypt_key(cfg.api_key_enc)}"}
    limits = httpx.Timeout(timeout, connect=min(30.0, timeout), pool=min(30.0, timeout))
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=limits, verify=llm_ssl_context(),
                                         trust_env=settings.LLM_TRUST_ENV) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                try:
                    body = resp.json()
                except json.JSONDecodeError as exc:
                    raise ValueError(f"模型服务返回了无法解析的响应：{exc}") from exc
    except TimeoutError:
        raise ChatDeadlineExceeded(
            f"模型服务在单次限时 {timeout:g} 秒内未返回完整响应"
        ) from None
    return _extract_message(body)


async def chat_completion(
    cfg: ModelConfig,
    messages: list[dict],
    timeout: float = 600.0,
    retries: int | None = None,
) -> str:
    """调用模型，返回文本内容。

    连接停滞（LLM_FIRST_CHUNK_TIMEOUT / LLM_STREAM_IDLE_TIMEOUT）、限流或服务端
    临时故障时按 retries 自动重试，默认取 settings.LLM_MAX_RETRIES；服务不接受
    stream:true（HTTP 400/422）时改走整包请求（不消耗重试次数）；模型一直在
    输出但超过整体限时（ChatDeadlineExceeded）时不重试——原样重试只会再超时一次。
    """
    max_retries = settings.LLM_MAX_RETRIES if retries is None else max(0, retries)
    attempt = 0
    use_plain = False
    while True:
        try:
            if use_plain:
                return await _chat_completion_plain(cfg, messages, timeout)
            return await _chat_completion_stream(cfg, messages, timeout)
        except ChatDeadlineExceeded:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            # 上下文超长要抢在 stream→plain 回退之前判定：换整包请求只会再超一次。
            try:
                body_text = exc.response.text
            except Exception:
                body_text = ""
            if _looks_like_context_overflow(body_text):
                raise ChatContextOverflow(
                    f"模型服务返回 HTTP {status}：输入超出模型上下文窗口",
                    retriable=False,
                ) from exc
            if not use_plain and status in (400, 422):
                use_plain = True
                logger.warning(
                    "model service rejected stream:true (HTTP %s), "
                    "falling back to non-streaming request",
                    status,
                )
                continue
            if status not in _RETRIABLE_STATUS or attempt >= max_retries:
                raise
            last = f"HTTP {status}"
        except ChatProviderError as exc:
            if not exc.retriable or attempt >= max_retries:
                raise
            last = f"{exc}"[:300]
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            try:
                request = exc.request
            except RuntimeError:
                request = None
            if is_certificate_verification_error(exc):
                raise httpx.ConnectError(
                    "模型服务 HTTPS 证书校验失败：证书链不完整、证书过期、"
                    "域名不匹配或 CA 未被信任。请检查服务端完整证书链；"
                    "使用私有或代理 CA 时，在 .env 配置 LLM_CA_BUNDLE "
                    "为可信 PEM CA 文件路径并重启后端。"
                    f"原始错误：{exc}",
                    request=request,
                ) from exc
            if attempt >= max_retries or isinstance(exc, _NON_RETRIABLE_REQUEST_ERRORS):
                if isinstance(exc, httpx.ConnectError) and not str(exc).strip():
                    raise httpx.ConnectError(
                        "模型服务连接中断，请检查网络和代理；"
                        "确认服务可直连时，可将项目 LLM_TRUST_ENV 设为 false 后重启后端。",
                        request=request,
                    ) from exc
                raise
            last = f"{type(exc).__name__}: {exc}"[:300]
        attempt += 1
        delay = min(2.0 * attempt, 8.0)
        logger.warning(
            "chat completion attempt %s/%s failed (%s), retrying in %.0fs",
            attempt,
            max_retries + 1,
            last,
            delay,
        )
        await asyncio.sleep(delay)


async def test_connection(cfg: ModelConfig) -> tuple[str, str]:
    """验证配置可用性，返回 (模型回复, 警告)。

    先做最短对话 ping；再拉取服务方的模型名单，配置的模型不在名单里时
    给出警告（有些服务对未知模型名会静默兜底，单靠 ping 发现不了）。
    服务不支持列出模型时跳过名单校验。
    """
    reply = await chat_completion(
        cfg, [{"role": "user", "content": "ping，请回复pong"}], timeout=30.0, retries=0
    )
    warning = ""
    try:
        url = cfg.base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {decrypt_key(cfg.api_key_enc)}"}
        async with httpx.AsyncClient(timeout=10.0, verify=llm_ssl_context(),
                                     trust_env=settings.LLM_TRUST_ENV) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            ids = [m.get("id", "") for m in resp.json().get("data", [])]
        if ids and cfg.model not in ids:
            warning = (
                f"服务方模型名单中没有“{cfg.model}”"
                f"（可用：{'、'.join(ids[:5])}{'……' if len(ids) > 5 else ''}），"
                "请确认模型名是否填错"
            )
    except Exception:
        pass
    return reply, warning
