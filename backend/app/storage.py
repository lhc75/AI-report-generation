"""MinIO 对象存储封装。"""
import io
import logging
import time
from pathlib import Path

from minio import Minio

from .config import settings

logger = logging.getLogger(__name__)

_client: Minio | None = None


def _with_retry(operation, attempts: int = 3):
    """对象存储瞬时连接故障（容器重启、网络抖动）自动重试，避免整批生成失败。"""
    last = None
    for index in range(attempts):
        try:
            return operation()
        except Exception as exc:
            last = exc
            if index + 1 < attempts:
                logger.warning("对象存储调用失败（%s），%ss 后重试", exc, 0.5 * (index + 1))
                time.sleep(0.5 * (index + 1))
    raise last


def _local_path(path: str) -> Path:
    if not settings.STORAGE_LOCAL_DIR:
        raise ValueError("本地测试存储必须设置 STORAGE_LOCAL_DIR")
    root = Path(settings.STORAGE_LOCAL_DIR).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("非法存储路径")
    return target


def client() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            settings.minio_endpoint,
            access_key=settings.OSS_ACCESS_KEY,
            secret_key=settings.OSS_SECRET_KEY,
            secure=settings.minio_secure,
        )
    return _client


def ensure_bucket() -> None:
    if settings.STORAGE_BACKEND == "local":
        Path(settings.STORAGE_LOCAL_DIR).mkdir(parents=True, exist_ok=True)
        return
    c = client()
    if not c.bucket_exists(settings.OSS_BUCKET):
        c.make_bucket(settings.OSS_BUCKET)


def upload_bytes(path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    if settings.STORAGE_BACKEND == "local":
        target = _local_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return
    _with_retry(lambda: client().put_object(
        settings.OSS_BUCKET, path, io.BytesIO(data), length=len(data), content_type=content_type
    ))


def download_bytes(path: str) -> bytes:
    if settings.STORAGE_BACKEND == "local":
        return _local_path(path).read_bytes()
    def _download() -> bytes:
        resp = client().get_object(settings.OSS_BUCKET, path)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    return _with_retry(_download)


def remove(path: str) -> None:
    if settings.STORAGE_BACKEND == "local":
        _local_path(path).unlink(missing_ok=True)
        return
    client().remove_object(settings.OSS_BUCKET, path)


def presigned_url(path: str) -> str:
    from datetime import timedelta

    if settings.STORAGE_BACKEND == "local":
        # 桌面客户端没有对象存储，返回本机后端可直接访问的下载地址。
        from urllib.parse import quote

        return "/api/storage/" + quote(path, safe="/")
    return client().presigned_get_object(
        settings.OSS_BUCKET, path, expires=timedelta(seconds=settings.OSS_PRESIGN_EXPIRE_SECONDS)
    )


def local_file_path(path: str) -> Path:
    """本地存储模式下把对象路径解析为磁盘文件路径（含越界校验）。"""
    return _local_path(path)
