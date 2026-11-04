"""模型 HTTPS 的证书信任：系统证书库、Mozilla CA 和可选的私有 CA。"""
import os
import ssl
from functools import lru_cache
from pathlib import Path

import certifi
import truststore

from .config import ROOT_DIR, settings


@lru_cache(maxsize=1)
def llm_ssl_context() -> ssl.SSLContext:
    """保留证书/主机名校验，由原生验证器处理系统信任与中间证书。

    macOS/Windows 原生验证器可以补全服务端漏发的中间证书；Linux 使用
    系统 OpenSSL 信任库，服务端仍应提供完整链。上下文只初始化一次。
    """
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # 同时保留 httpx 默认的 Mozilla CA，以及标准进程环境中的 CA 配置。
    context.load_verify_locations(
        cafile=os.environ.get("SSL_CERT_FILE") or certifi.where(),
        capath=os.environ.get("SSL_CERT_DIR") or None,
    )
    if settings.LLM_CA_BUNDLE.strip():
        bundle = Path(settings.LLM_CA_BUNDLE.strip()).expanduser()
        if not bundle.is_absolute():
            bundle = ROOT_DIR / bundle
        try:
            context.load_verify_locations(cafile=str(bundle))
        except (OSError, ssl.SSLError) as exc:
            raise ValueError(
                f"无法加载 LLM_CA_BUNDLE 证书文件：{bundle}；"
                "请确认文件存在且包含 PEM 格式的 CA 证书"
            ) from exc
    return context


def is_certificate_verification_error(exc: BaseException) -> bool:
    """识别 httpx/httpcore 包装后的证书错误，避免对配置错误反复重试。"""
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        for nested in (current.__cause__, current.__context__):
            if nested is not None:
                pending.append(nested)
    return False
