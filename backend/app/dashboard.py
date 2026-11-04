"""问卷作答数据统计服务。

由旧版 Streamlit ``dashboard.py`` 迁入并重构；本模块不依赖 Streamlit，
负责把项目数据文件转换成概览、聚合表和分页明细。
"""
from __future__ import annotations

import csv

import io
import re
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from . import storage

# 与旧版 dashboard.py 保持一致的元信息口径。
META_COLS = ["username", "city", "area", "school", "schoolId", "grade", "submit_count", "update_time"]
SUPPORTED_EXTS = {".csv", ".xlsx", ".xls"}
_META_CACHE_MAX = 8
_FULL_CACHE_MAX = 2
BUILTIN_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "dashboard" / "学生作答情况_12.14"
BUILTIN_SOURCE_PREFIX = "builtin:"

_META_CACHE: OrderedDict[tuple[str, str], pd.DataFrame] = OrderedDict()
_FULL_CACHE: OrderedDict[tuple[str, str], pd.DataFrame] = OrderedDict()
_INFLIGHT_FRAMES: dict[tuple[str, str, str], dict[str, Any]] = {}
_CACHE_LOCK = threading.RLock()


class DashboardDataError(ValueError):
    """数据文件无法用于 dashboard 统计。"""


def list_builtin_files() -> list[dict[str, Any]]:
    """列出随当前系统迁移的旧版学生作答 CSV。"""
    root = BUILTIN_DATA_DIR.resolve()
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.glob("*.csv")):
        stat = path.stat()
        files.append({
            "id": f"{BUILTIN_SOURCE_PREFIX}{path.name}",
            "file_name": path.name,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return files


def _decode_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _validate_columns(columns: Iterable[Any]) -> None:
    existing = {str(column) for column in columns}
    missing = [column for column in META_COLS if column not in existing]
    if missing:
        raise DashboardDataError("数据文件缺少必要列：" + "、".join(missing))


def _read_dataframe(file_name: str, raw: bytes, *, meta_only: bool) -> pd.DataFrame:
    ext = Path(file_name.lower()).suffix
    if ext not in SUPPORTED_EXTS:
        raise DashboardDataError("仅支持 CSV、XLSX 或 XLS 数据文件")

    if ext == ".csv":
        text = _decode_csv(raw)
        if meta_only:
            header = next(csv.reader([text.split("\n", 1)[0]]), [])
            _validate_columns(header)
            df = pd.read_csv(
                io.StringIO(text),
                usecols=META_COLS,
                dtype=str,
                keep_default_na=False,
            )
        else:
            df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
            _validate_columns(df.columns)
    else:
        # 与统计设置页面一致：Excel 默认分析第一个工作表。
        df = pd.read_excel(
            io.BytesIO(raw),
            sheet_name=0,
            dtype=str,
            keep_default_na=False,
        )
        _validate_columns(df.columns)
        if meta_only:
            df = df[META_COLS].copy()

    if df.empty:
        raise DashboardDataError("数据文件没有可统计的记录")
    return _normalize_meta(df)


def _normalize_meta(df: pd.DataFrame) -> pd.DataFrame:
    """统一空值、提交次数和更新时间，缓存中的帧保持不变性。"""
    out = df.copy()
    for column in META_COLS:
        out[column] = out[column].astype(str).str.strip()
    out["submit_count"] = pd.to_numeric(out["submit_count"], errors="coerce").fillna(0)
    out["update_time"] = pd.to_datetime(out["update_time"], errors="coerce")
    return out


def _cache_get(cache: OrderedDict[tuple[str, str], pd.DataFrame], key: tuple[str, str]) -> pd.DataFrame | None:
    """返回缓存帧；调用方不得直接修改返回对象。"""
    with _CACHE_LOCK:
        if key not in cache:
            return None
        cache.move_to_end(key)
        return cache[key]


def _cache_put(cache: OrderedDict[tuple[str, str], pd.DataFrame], key: tuple[str, str],
               value: pd.DataFrame, max_size: int) -> None:
    with _CACHE_LOCK:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > max_size:
            cache.popitem(last=False)


def _load_frame_or_error(file_name: str, raw: bytes, *, meta_only: bool) -> pd.DataFrame:
    try:
        return _read_dataframe(file_name, raw, meta_only=meta_only)
    except DashboardDataError:
        raise
    except Exception as exc:
        raise DashboardDataError("数据文件解析失败，请检查 CSV/Excel 内容是否与扩展名一致") from exc


def _read_source_bytes(source_ref: str) -> bytes:
    """读取对象存储文件或随系统迁移的内置 CSV。"""
    if source_ref.startswith(BUILTIN_SOURCE_PREFIX):
        name = source_ref[len(BUILTIN_SOURCE_PREFIX):]
        root = BUILTIN_DATA_DIR.resolve()
        target = (root / name).resolve()
        if not target.is_file() or not target.is_relative_to(root):
            raise DashboardDataError("内置数据文件不存在")
        try:
            return target.read_bytes()
        except OSError as exc:
            raise DashboardDataError("内置数据文件读取失败") from exc
    try:
        return storage.download_bytes(source_ref)
    except Exception as exc:
        raise DashboardDataError("数据文件读取失败，请重新上传后再试") from exc


def _load_cached_frame(kind: str, file_id: int | str, file_name: str, source_ref: str,
                       *, meta_only: bool, cache: OrderedDict[tuple[str, str], pd.DataFrame],
                       max_size: int) -> pd.DataFrame:
    """缓存命中直接返回；并发请求同一文件时只解析一次，其他请求等待结果。"""
    cache_key = (str(file_id), str(source_ref))
    inflight_key = (kind, cache_key[0], cache_key[1])
    with _CACHE_LOCK:
        cached = _cache_get(cache, cache_key)
        if cached is not None:
            return cached
        slot = _INFLIGHT_FRAMES.get(inflight_key)
        if slot is None:
            slot = {"event": threading.Event(), "error": None}
            _INFLIGHT_FRAMES[inflight_key] = slot
            owner = True
        else:
            owner = False

    if not owner:
        slot["event"].wait()
        if slot["error"] is not None:
            raise slot["error"]
        with _CACHE_LOCK:
            cached = _cache_get(cache, cache_key)
            if cached is not None:
                return cached
        raise DashboardDataError("数据文件读取失败，请重新加载")

    frame = None
    try:
        raw = _read_source_bytes(source_ref)
        frame = _load_frame_or_error(file_name, raw, meta_only=meta_only)
        _cache_put(cache, cache_key, frame, max_size)
        return frame
    except Exception as exc:
        slot["error"] = exc
        raise
    finally:
        with _CACHE_LOCK:
            _INFLIGHT_FRAMES.pop(inflight_key, None)
        slot["event"].set()


def load_meta_frame(file_id: int | str, file_name: str, source_ref: str) -> pd.DataFrame:
    """读取带容量上限的元信息帧；调用方必须已经完成数据源归属校验。"""
    return _load_cached_frame(
        "meta", file_id, file_name, source_ref,
        meta_only=True, cache=_META_CACHE, max_size=_META_CACHE_MAX,
    )


def load_full_frame(file_id: int | str, file_name: str, source_ref: str) -> pd.DataFrame:
    """仅在用户请求完整明细时读取，避免打开页面就解析全部题目列。"""
    return _load_cached_frame(
        "full", file_id, file_name, source_ref,
        meta_only=False, cache=_FULL_CACHE, max_size=_FULL_CACHE_MAX,
    )

def _grade_key(value: Any) -> tuple[int, int, str]:
    text = "" if pd.isna(value) else str(value).strip()
    match = re.search(r"\d+", text)
    if match:
        return (0, int(match.group(0)), text)
    return (1, 0, text)


def _group_sum(df: pd.DataFrame, column: str) -> list[dict[str, Any]]:
    series = df[[column, "submit_count"]].copy()
    series[column] = series[column].fillna("未填写").replace("", "未填写")
    grouped = series.groupby(column, dropna=False)["submit_count"].sum()
    grouped = grouped[grouped > 0].sort_values(ascending=False)
    return [{"name": str(name), "value": int(value)} for name, value in grouped.items()]


def _grade_chart(df: pd.DataFrame) -> list[dict[str, Any]]:
    grouped = _group_sum(df, "grade")
    grouped.sort(key=lambda item: _grade_key(item["name"]))
    return grouped


def _series_total(series: pd.Series) -> int:
    value = pd.to_numeric(series, errors="coerce").sum()
    return 0 if pd.isna(value) else int(value)


def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    if value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat(timespec="seconds")
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if np.isnan(value) else float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (int, float, bool)) and not isinstance(value, (str, bytes)):
        if isinstance(value, float) and np.isnan(value):
            return None
        return value
    if value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _records_payload(df: pd.DataFrame, page: int, page_size: int) -> dict[str, Any]:
    start = (page - 1) * page_size
    end = start + page_size
    page_df = df.iloc[start:end]
    return {
        "columns": [str(column) for column in df.columns],
        "rows": [
            {str(column): _json_scalar(value) for column, value in row.items()}
            for row in page_df.to_dict(orient="records")
        ],
        "page": page,
        "page_size": page_size,
        "total": int(len(df)),
    }


def apply_filters(df: pd.DataFrame, filters: list[Any]) -> pd.DataFrame:
    """应用旧版 dashboard 的文本、数值范围和日期筛选。"""
    out = df.copy()
    for item in filters or []:
        column = str(getattr(item, "column", "") or "")
        if not column or column not in out.columns:
            raise DashboardDataError(f"筛选列不存在：{column or '空列名'}")

        kind = getattr(item, "kind", "text")
        if kind == "text":
            value = str(getattr(item, "value", "") or "")
            if value:
                mask = out[column].astype("string").str.contains(
                    value, case=False, na=False, regex=False
                )
                out = out[mask]
        elif kind == "range":
            numeric = pd.to_numeric(out[column], errors="coerce")
            mask = numeric.notna()
            minimum = getattr(item, "minimum", None)
            maximum = getattr(item, "maximum", None)
            if minimum is not None:
                mask &= numeric >= float(minimum)
            if maximum is not None:
                mask &= numeric <= float(maximum)
            out = out[mask]
        elif kind == "date":
            dates = pd.to_datetime(out[column], errors="coerce")
            mask = dates.notna()
            start = getattr(item, "start", None)
            end = getattr(item, "end", None)
            if start is not None:
                mask &= dates >= pd.Timestamp(start)
            if end is not None:
                mask &= dates < pd.Timestamp(end) + pd.Timedelta(days=1)
            out = out[mask]
        else:
            raise DashboardDataError(f"不支持的筛选类型：{kind}")
    return out


def build_overview(df: pd.DataFrame, school_keyword: str = "", school_id: str = "") -> dict[str, Any]:
    """计算顶部指标、城市/年级分布和单校查询。"""
    total_submissions = _series_total(df["submit_count"])
    n_students = int(df["username"].nunique(dropna=True))
    n_schools = int(df["schoolId"].nunique(dropna=True))
    last_update = df["update_time"].max()
    school_query = _build_school_query(df, school_keyword, school_id)
    return {
        "summary": {
            "total_submissions": total_submissions,
            "student_count": n_students,
            "school_count": n_schools,
            "last_update": None if pd.isna(last_update) else last_update.isoformat(timespec="seconds"),
        },
        "charts": {
            "city": _group_sum(df, "city"),
            "grade": _grade_chart(df),
        },
        "school_query": school_query,
    }


def _build_school_query(df: pd.DataFrame, school_keyword: str, school_id: str) -> dict[str, Any] | None:
    keyword = (school_keyword or "").strip()
    sid = (school_id or "").strip()
    if not keyword and not sid:
        return None

    mask = pd.Series(True, index=df.index)
    if keyword:
        mask &= df["school"].astype("string").str.contains(keyword, case=False, na=False, regex=False)
    if sid:
        mask &= df["schoolId"].astype("string") == sid
    matched = df[mask]
    if matched.empty:
        return {"found": False}

    info = matched.iloc[0]
    grades = matched[["grade", "submit_count"]].copy()
    grades["grade"] = grades["grade"].fillna("未填写").replace("", "未填写")
    grouped = grades.groupby("grade", dropna=False)["submit_count"].sum()
    return {
        "found": True,
        "school": _json_scalar(info["school"]),
        "city": _json_scalar(info["city"]),
        "area": _json_scalar(info["area"]),
        "schoolId": _json_scalar(info["schoolId"]),
        "total_submissions": _series_total(matched["submit_count"]),
        "student_count": int(matched["username"].nunique(dropna=True)),
        "grades": [
            {"name": str(name), "value": int(value)}
            for name, value in sorted(grouped.items(), key=lambda item: _grade_key(item[0]))
        ],
    }


def build_school_table(df: pd.DataFrame, filters: list[Any]) -> dict[str, Any]:
    grouped_df = df[META_COLS].copy()
    for column in ("city", "area", "school", "schoolId", "grade"):
        grouped_df[column] = grouped_df[column].fillna("未填写").replace("", "未填写")
    table = (
        grouped_df.groupby(["city", "area", "school", "schoolId", "grade"], dropna=False, sort=False)["submit_count"]
        .sum()
        .reset_index(name="submit_count")
    )
    table = apply_filters(table, filters)
    table = table.sort_values(
        ["city", "area", "school", "grade"],
        key=lambda series: series.map(_grade_key) if series.name == "grade" else series.astype(str),
    ).reset_index(drop=True)
    return {"columns": [str(column) for column in table.columns], "rows": _records_payload(table, 1, len(table))["rows"], "total": int(len(table))}


def build_records(df: pd.DataFrame, filters: list[Any], page: int, page_size: int) -> dict[str, Any]:
    filtered = apply_filters(df, filters)
    return _records_payload(filtered, page, page_size)










