"""模型 TLS 离线验收：临时本地 HTTPS，完全隔离真实配置、数据库和存储。"""
import asyncio
import importlib
import json
import os
import ssl
import sys
import tempfile
import threading
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


# 使用独立包名加载实际应用代码，避免导入 app.db 时连接 MySQL，亦不读取 .env。
APP_DIR = Path(__file__).resolve().parent / "app"
PACKAGE_NAME = "_tls_offline_app"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(APP_DIR)]
sys.modules[PACKAGE_NAME] = package
config = types.ModuleType(f"{PACKAGE_NAME}.config")
config.ROOT_DIR = APP_DIR.parents[1]
config.settings = SimpleNamespace(
    LLM_CA_BUNDLE="",
    LLM_TRUST_ENV=False,
    LLM_FIRST_CHUNK_TIMEOUT=5.0,
    LLM_STREAM_IDLE_TIMEOUT=5.0,
    LLM_MAX_RETRIES=2,
)
sys.modules[config.__name__] = config


@dataclass
class ModelConfig:
    base_url: str
    model: str = "offline-model"
    api_key_enc: str = "offline-token"
    params_json: str = "{}"


models = types.ModuleType(f"{PACKAGE_NAME}.models")
models.ModelConfig = ModelConfig
sys.modules[models.__name__] = models
security = types.ModuleType(f"{PACKAGE_NAME}.security")
security.decrypt_key = lambda _value: "offline-token"
sys.modules[security.__name__] = security
tls = importlib.import_module(f"{PACKAGE_NAME}.tls")
llm = importlib.import_module(f"{PACKAGE_NAME}.llm")


def create_certificates(directory: Path) -> Path:
    """创建仅信任于本次测试的 CA 和 localhost 服务证书。"""
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Offline TLS Test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=True,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (directory / "server.pem").write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    (directory / "server.key").write_bytes(server_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return ca_path


class OfflineHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *args):
        pass

    def respond(self, body: bytes, content_type: str = "application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append(("GET", self.path))
        self.respond(json.dumps({"data": [{"id": "offline-model"}]}).encode())

    def do_POST(self):
        self.server.requests.append(("POST", self.path))
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if payload.get("stream"):
            self.respond(
                b'data: {"choices":[{"delta":{"content":"pong"}}]}\n\n'
                b'data: [DONE]\n\n',
                "text/event-stream",
            )
        else:
            self.respond(b'{"choices":[{"message":{"content":"pong"}}]}')


class TLSOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="ai-report-tls-")
        cls.directory = Path(cls.temporary.name)
        cls.ca_path = create_certificates(cls.directory)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), OfflineHandler)
        cls.server.requests = []
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cls.directory / "server.pem", cls.directory / "server.key")
        cls.server.socket = server_context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"https://localhost:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary.cleanup()

    def setUp(self):
        self.environment = patch.dict(os.environ, {"NO_PROXY": "localhost,127.0.0.1"})
        self.environment.start()
        os.environ.pop("SSL_CERT_FILE", None)
        os.environ.pop("SSL_CERT_DIR", None)
        config.settings.LLM_CA_BUNDLE = ""
        tls.llm_ssl_context.cache_clear()
        self.server.requests.clear()

    def tearDown(self):
        tls.llm_ssl_context.cache_clear()
        config.settings.LLM_CA_BUNDLE = ""
        self.environment.stop()

    async def get(self, url: str, context: ssl.SSLContext):
        async with httpx.AsyncClient(verify=context, timeout=5.0, trust_env=False) as client:
            return await client.get(url)

    def trusted_context(self):
        config.settings.LLM_CA_BUNDLE = str(self.ca_path)
        tls.llm_ssl_context.cache_clear()
        return tls.llm_ssl_context()

    def test_default_context_keeps_verification_and_rejects_untrusted_ca(self):
        context = tls.llm_ssl_context()
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertIs(context, tls.llm_ssl_context())
        with self.assertRaises(httpx.ConnectError) as raised:
            asyncio.run(self.get(self.base_url + "/models", context))
        self.assertTrue(tls.is_certificate_verification_error(raised.exception))
        self.assertEqual(self.server.requests, [])

    def test_explicit_ca_allows_real_https(self):
        context = self.trusted_context()
        response = asyncio.run(self.get(self.base_url + "/models", context))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], "offline-model")
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_trusted_ca_still_rejects_wrong_hostname(self):
        context = self.trusted_context()
        url = f"https://127.0.0.1:{self.server.server_port}/models"
        with self.assertRaises(httpx.ConnectError) as raised:
            asyncio.run(self.get(url, context))
        self.assertTrue(tls.is_certificate_verification_error(raised.exception))
        self.assertEqual(self.server.requests, [])

    def test_relative_ca_path_resolves_from_project_root(self):
        config.settings.LLM_CA_BUNDLE = "ca.pem"
        with patch.object(tls, "ROOT_DIR", self.directory):
            context = tls.llm_ssl_context()
        self.assertEqual(asyncio.run(self.get(self.base_url + "/models", context)).status_code, 200)

    def test_process_ssl_cert_file_is_honored(self):
        os.environ["SSL_CERT_FILE"] = str(self.ca_path)
        context = tls.llm_ssl_context()
        self.assertEqual(asyncio.run(self.get(self.base_url + "/models", context)).status_code, 200)

    def test_missing_or_invalid_ca_configuration_fails_clearly(self):
        invalid = self.directory / "invalid.pem"
        invalid.write_text("this is not a certificate", encoding="utf-8")
        for value in (self.directory / "missing.pem", invalid, self.directory):
            with self.subTest(path=value.name):
                config.settings.LLM_CA_BUNDLE = str(value)
                tls.llm_ssl_context.cache_clear()
                with self.assertRaises(ValueError) as raised:
                    tls.llm_ssl_context()
                self.assertIn("LLM_CA_BUNDLE", str(raised.exception))

    def test_all_model_client_paths_use_the_verified_context(self):
        context = self.trusted_context()
        original_client = httpx.AsyncClient
        contexts = []

        def recording_client(*args, **kwargs):
            contexts.append(kwargs.get("verify"))
            kwargs["trust_env"] = False
            return original_client(*args, **kwargs)

        async def run_calls():
            cfg = ModelConfig(base_url=self.base_url)
            messages = [{"role": "user", "content": "ping"}]
            self.assertEqual(await llm._chat_completion_stream(cfg, messages, timeout=5), "pong")
            self.assertEqual(await llm._chat_completion_plain(cfg, messages, timeout=5), "pong")
            self.assertEqual(await llm.test_connection(cfg), ("pong", ""))

        with patch.object(llm.httpx, "AsyncClient", recording_client):
            asyncio.run(run_calls())
        self.assertEqual(len(contexts), 4)
        self.assertTrue(all(item is context for item in contexts))
        self.assertEqual(self.server.requests, [
            ("POST", "/chat/completions"),
            ("POST", "/chat/completions"),
            ("POST", "/chat/completions"),
            ("GET", "/models"),
        ])

    def test_certificate_failure_does_not_retry(self):
        request = httpx.Request("POST", self.base_url + "/chat/completions")
        error = httpx.ConnectError("TLS handshake failed", request=request)
        error.__cause__ = ssl.SSLCertVerificationError(1, "unable to get local issuer certificate")
        streaming = AsyncMock(side_effect=error)
        plain = AsyncMock()
        sleep = AsyncMock()
        cfg = ModelConfig(base_url=self.base_url)
        with patch.object(llm, "_chat_completion_stream", streaming), \
                patch.object(llm, "_chat_completion_plain", plain), \
                patch.object(llm.asyncio, "sleep", sleep):
            with self.assertRaises(httpx.ConnectError) as raised:
                asyncio.run(llm.chat_completion(cfg, [], retries=3))
        streaming.assert_awaited_once()
        plain.assert_not_awaited()
        sleep.assert_not_awaited()
        self.assertIn("证书", str(raised.exception))
        self.assertIn("LLM_CA_BUNDLE", str(raised.exception))
        self.assertIs(raised.exception.request, request)
        self.assertIs(raised.exception.__cause__, error)

    def test_real_untrusted_https_failure_does_not_retry(self):
        streaming = AsyncMock(wraps=llm._chat_completion_stream)
        sleep = AsyncMock()
        cfg = ModelConfig(base_url=self.base_url)
        with patch.object(llm, "_chat_completion_stream", streaming), \
                patch.object(llm.asyncio, "sleep", sleep):
            with self.assertRaises(httpx.ConnectError) as raised:
                asyncio.run(llm.chat_completion(cfg, [], timeout=5, retries=3))
        streaming.assert_awaited_once()
        sleep.assert_not_awaited()
        self.assertIn("证书", str(raised.exception))
        self.assertEqual(self.server.requests, [])

    def test_certificate_error_detection_checks_chains_and_avoids_cycles(self):
        certificate_error = ssl.SSLCertVerificationError(1, "issuer not found")
        cause_error = httpx.ConnectError("connection failed")
        cause_error.__cause__ = certificate_error
        context_error = RuntimeError("wrapped request")
        context_error.__context__ = certificate_error
        self.assertTrue(tls.is_certificate_verification_error(cause_error))
        self.assertTrue(tls.is_certificate_verification_error(context_error))
        self.assertTrue(tls.is_certificate_verification_error(
            httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        ))
        first, second = RuntimeError("network error"), RuntimeError("DNS error")
        first.__cause__, second.__context__ = second, first
        self.assertFalse(tls.is_certificate_verification_error(first))
        self.assertFalse(tls.is_certificate_verification_error(httpx.ReadTimeout("read timeout")))

    def test_enabled_integral_top_k_is_serialized_as_integer(self):
        for value in (20, 20.0):
            with self.subTest(value=value):
                cfg = ModelConfig(base_url=self.base_url, params_json=json.dumps({
                    "top_k": {"enabled": True, "value": value},
                    "temperature": {"enabled": True, "value": 0.7},
                }))
                params = llm.enabled_params(cfg)
                self.assertEqual(params, {"top_k": 20, "temperature": 0.7})
                self.assertIs(type(params["top_k"]), int)
                self.assertIs(type(json.loads(json.dumps(params))["top_k"]), int)

    def test_enabled_invalid_top_k_is_rejected(self):
        for value in (20.5, True, "20"):
            with self.subTest(value=value):
                cfg = ModelConfig(base_url=self.base_url, params_json=json.dumps({
                    "top_k": {"enabled": True, "value": value},
                }))
                with self.assertRaises(ValueError) as raised:
                    llm.enabled_params(cfg)
                self.assertIn("top_k", str(raised.exception))
                self.assertIn("整数", str(raised.exception))

    def test_disabled_top_k_is_not_sent(self):
        cfg = ModelConfig(base_url=self.base_url, params_json=json.dumps({
            "top_k": {"enabled": False, "value": 20.5},
            "temperature": {"enabled": True, "value": 0.7},
        }))
        self.assertEqual(llm.enabled_params(cfg), {"temperature": 0.7})


if __name__ == "__main__":
    unittest.main(verbosity=2)
