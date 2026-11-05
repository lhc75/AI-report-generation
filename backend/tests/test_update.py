# -*- coding: utf-8 -*-
"""版本比较、安装包匹配与 GitHub 更新检查（离线假 HTTP 客户端）。"""
import asyncio

import pytest

from app import update


ASSETS = [
    {"name": "AIReport-1.1.0-arm64.dmg", "browser_download_url": "https://github.com/x/dmg", "size": 100},
    {"name": "AIReport-1.1.0-arm64.dmg.blockmap", "browser_download_url": "https://github.com/x/bm", "size": 1},
    {"name": "AIReport-1.1.0-arm64.zip", "browser_download_url": "https://github.com/x/zip", "size": 90},
    {"name": "AIReport-Portable-1.1.0.exe", "browser_download_url": "https://github.com/x/portable", "size": 80},
    {"name": "AIReport-Setup-1.1.0.exe", "browser_download_url": "https://github.com/x/setup", "size": 70},
]


def test_version_parsing_and_compare():
    assert update.version_tuple("v1.2.3") == (1, 2, 3)
    assert update.version_tuple("1.2.3-rc1") == (1, 2, 3)
    assert update.compare_versions("1.10.0", "1.9.9") == 1
    assert update.compare_versions("1.0", "1.0.0") == 0
    assert update.is_newer("v1.0.3", "1.0.2")
    assert not update.is_newer("1.0.2", "1.0.2")


def test_match_assets_filters_by_platform_and_arch():
    mac = update.match_assets(ASSETS, "mac", "arm64")
    assert [item["name"] for item in mac] == [
        "AIReport-1.1.0-arm64.dmg", "AIReport-1.1.0-arm64.zip"]
    assert all(item["kind"] in {"dmg", "zip"} for item in mac)

    win = update.match_assets(ASSETS, "win", "x64")
    assert [item["kind"] for item in win] == ["portable", "setup"]
    assert update.recommended_asset(win, "win")["kind"] == "setup"
    assert update.recommended_asset(mac, "mac")["kind"] == "zip"

    # 架构不符的资产被排除
    assert update.match_assets(ASSETS, "mac", "x64") == []


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeClient:
    calls = 0

    def __init__(self, payload=None, error=None, **_kwargs):
        self.payload = payload
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, _url, **_kwargs):
        type(self).calls += 1
        if self.error:
            raise self.error
        return _FakeResponse(self.payload)


RELEASE = {
    "tag_name": "v1.1.0",
    "html_url": "https://github.com/x/releases/tag/v1.1.0",
    "name": "v1.1.0",
    "published_at": "2026-11-05T10:00:00Z",
    "body": "修复若干问题",
    "assets": ASSETS,
}


def _patch_client(monkeypatch, payload=None, error=None):
    _FakeClient.calls = 0
    monkeypatch.setattr(update.httpx, "AsyncClient",
                        lambda **kwargs: _FakeClient(payload, error, **kwargs))
    update.reset_cache()


def test_check_for_update_reports_new_version(monkeypatch):
    monkeypatch.setattr(update.settings, "APP_VERSION", "1.0.2")
    monkeypatch.setattr(update, "platform_key", lambda: "mac")
    monkeypatch.setattr(update, "arch_key", lambda: "arm64")
    _patch_client(monkeypatch, payload=RELEASE)

    result = asyncio.run(update.check_for_update(force=True))
    assert result["ok"] is True
    assert result["has_update"] is True
    assert result["latest_version"] == "1.1.0"
    assert result["recommended"]["kind"] == "zip"
    assert "修复若干问题" in result["release_notes"]
    assert _FakeClient.calls == 1

    # 短时缓存：再次检查不再请求
    asyncio.run(update.check_for_update(force=False))
    assert _FakeClient.calls == 1


def test_check_for_update_handles_network_error(monkeypatch):
    monkeypatch.setattr(update.settings, "APP_VERSION", "1.0.2")
    _patch_client(monkeypatch, error=RuntimeError("network down"))
    result = asyncio.run(update.check_for_update(force=True))
    assert result["ok"] is False
    assert "network down" in result["error"]
    assert result["current_version"] == "1.0.2"


def test_check_for_update_same_version(monkeypatch):
    monkeypatch.setattr(update.settings, "APP_VERSION", "1.1.0")
    _patch_client(monkeypatch, payload=RELEASE)
    result = asyncio.run(update.check_for_update(force=True))
    assert result["ok"] is True
    assert result["has_update"] is False
