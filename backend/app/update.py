"""应用版本与更新检查：查询 GitHub Releases 最新版本并匹配当前平台的安装包。

设计要点：
- 只读公开 Release API，失败不抛出到接口层，返回可展示的错误信息；
- 查询结果短时缓存，避免界面轮询触发 GitHub 限流；
- 资产按平台/架构匹配，桌面端据此选择直接下载安装的包。
"""
from __future__ import annotations

import logging
import platform
import re
import sys
import time
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")
_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
_CACHE_ERROR_SECONDS = 60


def version_tuple(value: str) -> tuple[int, ...]:
    """把 v1.2.3 / 1.2 / 1.2.3-rc1 解析为可比较的数字元组。"""
    match = _VERSION_RE.search(str(value or ""))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def compare_versions(left: str, right: str) -> int:
    left_parts, right_parts = version_tuple(left), version_tuple(right)
    length = max(len(left_parts), len(right_parts))
    left_padded = left_parts + (0,) * (length - len(left_parts))
    right_padded = right_parts + (0,) * (length - len(right_parts))
    return (left_padded > right_padded) - (left_padded < right_padded)


def is_newer(candidate: str, current: str) -> bool:
    return bool(version_tuple(candidate)) and compare_versions(candidate, current) > 0


def current_version() -> str:
    return (settings.APP_VERSION or "").strip() or "0.0.0"


def platform_key() -> str:
    if sys.platform.startswith("darwin"):
        return "mac"
    if sys.platform.startswith("win"):
        return "win"
    return "linux"


def arch_key() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x64"


def release_page_url() -> str:
    return f"https://github.com/{settings.UPDATE_REPO}/releases"


def _asset_platform(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(".blockmap") or lowered.endswith(".sha256"):
        return ""
    if lowered.endswith(".dmg") or lowered.endswith(".pkg"):
        return "mac"
    if lowered.endswith(".exe"):
        return "win"
    if lowered.endswith(".appimage") or lowered.endswith(".deb"):
        return "linux"
    if lowered.endswith(".zip"):
        return ""  # zip 平台未知，由调用方按当前平台接受（mac 的更新包）
    return ""


def _asset_arch(name: str) -> str:
    lowered = name.lower()
    if "arm64" in lowered or "aarch64" in lowered:
        return "arm64"
    if "x64" in lowered or "amd64" in lowered or "x86_64" in lowered:
        return "x64"
    if "universal" in lowered:
        return "universal"
    return ""


def _asset_kind(platform_name: str, name: str) -> str:
    lowered = name.lower()
    if platform_name == "mac":
        return "zip" if lowered.endswith(".zip") else "dmg" if lowered.endswith(".dmg") else "pkg"
    if platform_name == "win":
        if "setup" in lowered:
            return "setup"
        if "portable" in lowered:
            return "portable"
        return "exe"
    return "linux"


def match_assets(assets: list[dict], target_platform: str | None = None,
                 target_arch: str | None = None) -> list[dict[str, Any]]:
    """按当前平台/架构过滤 Release 资产，保持 API 返回顺序。"""
    target_platform = target_platform or platform_key()
    target_arch = target_arch or arch_key()
    matched: list[dict[str, Any]] = []
    for asset in assets:
        name = str(asset.get("name") or "")
        kind = _asset_kind(target_platform, name)
        asset_platform = _asset_platform(name)
        if target_platform == "mac":
            if not (name.lower().endswith(".dmg") or name.lower().endswith(".zip")
                    or name.lower().endswith(".pkg")):
                continue
        elif asset_platform != target_platform:
            continue
        asset_arch = _asset_arch(name)
        if asset_arch and asset_arch not in {target_arch, "universal"}:
            continue
        matched.append({
            "name": name,
            "url": asset.get("browser_download_url") or "",
            "size": int(asset.get("size") or 0),
            "kind": kind,
        })
    return matched


def recommended_asset(assets: list[dict[str, Any]], target_platform: str | None = None) -> dict[str, Any] | None:
    """桌面端自动安装优先使用 zip（mac）或 Setup（win）。"""
    target_platform = target_platform or platform_key()
    if target_platform == "mac":
        for kind in ("zip", "dmg", "pkg"):
            hit = next((item for item in assets if item["kind"] == kind), None)
            if hit:
                return hit
        return assets[0] if assets else None
    for kind in ("setup", "exe", "portable"):
        hit = next((item for item in assets if item["kind"] == kind), None)
        if hit:
            return hit
    return assets[0] if assets else None


def _cached_payload(force: bool) -> dict[str, Any] | None:
    payload = _CACHE.get("payload")
    if not payload or force:
        return None
    age = time.monotonic() - float(_CACHE.get("at") or 0)
    limit = settings.UPDATE_CACHE_SECONDS if payload.get("ok") else _CACHE_ERROR_SECONDS
    return dict(payload) if age <= limit else None


def _remember(payload: dict[str, Any]) -> dict[str, Any]:
    _CACHE["at"] = time.monotonic()
    _CACHE["payload"] = payload
    return payload


def _base_payload() -> dict[str, Any]:
    return {
        "current_version": current_version(),
        "platform": platform_key(),
        "arch": arch_key(),
        "release_url": release_page_url(),
    }


async def check_for_update(force: bool = False) -> dict[str, Any]:
    """查询最新 Release 并与当前版本比较；任何网络问题都转成结构化结果。"""
    cached = _cached_payload(force)
    if cached is not None:
        return cached
    base = _base_payload()
    url = f"https://api.github.com/repos/{settings.UPDATE_REPO}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-report-update-check",
    }
    try:
        async with httpx.AsyncClient(
            timeout=settings.UPDATE_CHECK_TIMEOUT,
            follow_redirects=True,
            trust_env=settings.LLM_TRUST_ENV,
        ) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 404:
                return _remember({**base, "ok": False,
                                  "error": "仓库尚未发布任何版本"})
            response.raise_for_status()
            release = response.json()
    except Exception as exc:  # 网络、证书、限流、响应格式都可能失败
        logger.warning("检查 GitHub 新版本失败：%s", exc)
        return _remember({**base, "ok": False,
                          "error": f"无法连接 GitHub 检查更新：{type(exc).__name__}: {exc}"})

    tag = str(release.get("tag_name") or "").strip()
    latest_version = tag.lstrip("vV")
    assets = match_assets(list(release.get("assets") or []))
    recommended = recommended_asset(assets)
    notes = str(release.get("body") or "").strip()
    payload = {
        **base,
        "ok": True,
        "latest_version": latest_version or tag,
        "has_update": is_newer(latest_version, base["current_version"]),
        "release_url": release.get("html_url") or release_page_url(),
        "release_name": release.get("name") or tag,
        "published_at": release.get("published_at") or "",
        "release_notes": notes[:4000],
        "assets": assets,
        "recommended": recommended,
        "checked_at": int(time.time()),
    }
    return _remember(payload)


def reset_cache() -> None:
    """测试与"立即重新检查"使用。"""
    _CACHE["at"] = 0.0
    _CACHE["payload"] = None
