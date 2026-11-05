"""应用信息与更新检查接口。"""
from fastapi import APIRouter, Query

from .. import update as update_engine
from ..config import settings

router = APIRouter(prefix="/api/app", tags=["app"])


@router.get("/about")
def about():
    """当前客户端信息；版本由桌面端通过 APP_VERSION 注入。"""
    return {
        "name": "AI 报告生成",
        "version": update_engine.current_version(),
        "platform": update_engine.platform_key(),
        "arch": update_engine.arch_key(),
        "desktop": settings.AI_REPORT_DESKTOP,
        "update_repo": settings.UPDATE_REPO,
        "release_url": update_engine.release_page_url(),
    }


@router.get("/update")
async def check_update(force: bool = Query(False, description="忽略缓存立即查询 GitHub")):
    """检查 GitHub Releases 最新版本；网络失败返回 ok=false 而不是 500。"""
    return await update_engine.check_for_update(force=force)
