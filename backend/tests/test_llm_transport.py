"""模型代理配置统一覆盖所有请求路径，连接与证书错误可准确区分。"""
import asyncio
import json
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import llm
from app.config import Settings


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(llm, "decrypt_key", lambda _key: "isolated-test-key")
    return SimpleNamespace(base_url="https://model.invalid/v1", model="isolated-model",
                           params_json="{}", api_key_enc="offline")


def mocked_clients(monkeypatch, *, trust_env, reject_stream=False, overflow=False):
    monkeypatch.setattr(llm.settings, "LLM_TRUST_ENV", trust_env)
    verified_context = ssl.create_default_context()
    monkeypatch.setattr(llm, "llm_ssl_context", lambda: verified_context)
    original_client = httpx.AsyncClient
    options, requests = [], []

    def respond(request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "isolated-model"}]})
        if body.get("stream"):
            if overflow:
                return httpx.Response(400, json={"error": {"message": (
                    "This model's maximum context length is 131072 tokens. "
                    "However, you requested 200000 tokens")}})
            if reject_stream:
                return httpx.Response(400, json={"error": {"message": "Streaming not supported"}})
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"pong"}}]}\n\ndata: [DONE]\n\n',
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})

    def client(*args, **kwargs):
        options.append(dict(kwargs))
        return original_client(*args, **kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(llm.httpx, "AsyncClient", client)
    return options, requests, verified_context


@pytest.mark.parametrize("trust_env", [True, False])
def test_stream_plain_and_model_listing_use_explicit_proxy_setting_and_verified_tls(cfg, monkeypatch, trust_env):
    options, requests, context = mocked_clients(monkeypatch, trust_env=trust_env)

    async def run():
        assert await llm._chat_completion_stream(cfg, [], timeout=1) == "pong"
        assert await llm._chat_completion_plain(cfg, [], timeout=1) == "pong"
        assert await llm.test_connection(cfg) == ("pong", "")

    asyncio.run(run())
    assert len(options) == 4
    assert all(option["trust_env"] is trust_env and option["verify"] is context for option in options)
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert [(method, path) for method, path, _ in requests] == [
        ("POST", "/v1/chat/completions"), ("POST", "/v1/chat/completions"),
        ("POST", "/v1/chat/completions"), ("GET", "/v1/models"),
    ]


@pytest.mark.parametrize("trust_env", [True, False])
def test_http_400_stream_fallback_keeps_the_same_proxy_and_tls_settings(cfg, monkeypatch, trust_env):
    options, requests, context = mocked_clients(monkeypatch, trust_env=trust_env, reject_stream=True)
    assert asyncio.run(llm.chat_completion(cfg, [], timeout=1, retries=0)) == "pong"
    assert len(options) == 2
    assert all(option["trust_env"] is trust_env and option["verify"] is context for option in options)
    assert requests[0][2]["stream"] is True
    assert "stream" not in requests[1][2]


def test_proxy_setting_defaults_true_and_parses_false_explicitly(monkeypatch):
    monkeypatch.delenv("LLM_TRUST_ENV", raising=False)
    assert Settings(_env_file=None).LLM_TRUST_ENV is True
    monkeypatch.setenv("LLM_TRUST_ENV", "false")
    assert Settings(_env_file=None).LLM_TRUST_ENV is False


@pytest.mark.parametrize("with_request", [True, False])
def test_empty_connect_error_retries_then_explains_connection_interruption(cfg, monkeypatch, with_request):
    request = httpx.Request("POST", cfg.base_url + "/chat/completions") if with_request else None
    errors = [httpx.ConnectError("", request=request) for _ in range(3)]
    original_cause = OSError("isolated proxy tunnel closed")
    errors[-1].__cause__ = original_cause
    stream = AsyncMock(side_effect=errors)
    pause = AsyncMock()
    monkeypatch.setattr(llm, "_chat_completion_stream", stream)
    monkeypatch.setattr(llm.asyncio, "sleep", pause)
    with pytest.raises(httpx.ConnectError, match="模型服务连接中断") as raised:
        asyncio.run(llm.chat_completion(cfg, [], retries=2))
    assert stream.await_count == 3 and pause.await_count == 2
    assert "网络和代理" in str(raised.value) and "LLM_TRUST_ENV" in str(raised.value)
    assert "证书" not in str(raised.value)
    assert raised.value.__cause__ is errors[-1] and raised.value.__cause__.__cause__ is original_cause
    if request is not None:
        assert raised.value.request is request


def test_empty_connection_failure_can_recover_before_retries_are_exhausted(cfg, monkeypatch):
    stream = AsyncMock(side_effect=[httpx.ConnectError(""), "recovered"])
    monkeypatch.setattr(llm, "_chat_completion_stream", stream)
    monkeypatch.setattr(llm.asyncio, "sleep", AsyncMock())
    assert asyncio.run(llm.chat_completion(cfg, [], retries=1)) == "recovered"
    assert stream.await_count == 2


@pytest.mark.parametrize("with_request", [True, False])
def test_certificate_cause_takes_priority_over_empty_connection_message(cfg, monkeypatch, with_request):
    request = httpx.Request("POST", cfg.base_url + "/chat/completions") if with_request else None
    error = httpx.ConnectError("", request=request)
    error.__cause__ = ssl.SSLCertVerificationError(1, "isolated certificate validation failure")
    stream, pause = AsyncMock(side_effect=error), AsyncMock()
    monkeypatch.setattr(llm, "_chat_completion_stream", stream)
    monkeypatch.setattr(llm.asyncio, "sleep", pause)
    with pytest.raises(httpx.ConnectError, match="HTTPS 证书校验失败") as raised:
        asyncio.run(llm.chat_completion(cfg, [], retries=2))
    assert stream.await_count == 1 and pause.await_count == 0
    assert "LLM_CA_BUNDLE" in str(raised.value)
    assert "模型服务连接中断" not in str(raised.value)
    assert raised.value.__cause__ is error
    if request is not None:
        assert raised.value.request is request


def test_nonempty_connect_error_retains_original_details(cfg, monkeypatch):
    error = httpx.ConnectError("isolated connection refused")
    monkeypatch.setattr(llm, "_chat_completion_stream", AsyncMock(side_effect=error))
    with pytest.raises(httpx.ConnectError) as raised:
        asyncio.run(llm.chat_completion(cfg, [], retries=0))
    assert raised.value is error


@pytest.mark.parametrize("trust_env", [True, False])
def test_http_400_context_overflow_raises_without_plain_fallback(cfg, monkeypatch, trust_env):
    options, requests, context = mocked_clients(monkeypatch, trust_env=trust_env, overflow=True)
    with pytest.raises(llm.ChatContextOverflow) as raised:
        asyncio.run(llm.chat_completion(cfg, [{"role": "user", "content": "hi"}],
                                        timeout=10, retries=0))
    assert "上下文窗口" in str(raised.value)
    assert raised.value.retriable is False
    assert [method for method, _, _ in requests] == ["POST"]  # 未浪费 stream→plain 回退


def test_in_body_context_overflow_maps_to_overflow_error():
    error = llm._provider_error({"error": {"message": (
        "This model's maximum context length is 65536 tokens")}})
    assert isinstance(error, llm.ChatContextOverflow)
    assert isinstance(error, llm.ChatProviderError)
    assert error.retriable is False


def test_in_body_other_errors_stay_provider_errors():
    error = llm._provider_error({"error": {"message": "rate limit exceeded, please retry"}})
    assert isinstance(error, llm.ChatProviderError)
    assert not isinstance(error, llm.ChatContextOverflow)
    assert error.retriable is True
