"""Pydantic 请求模型与 UTC 整点时间工具。"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

# 形如 2026-11-15T08:00:00Z 或 2026-11-15T08:00Z（必须整点）
_HOUR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:00:00Z$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DomainError(Exception):
    """业务规则错误，映射为 HTTP 400（同版异文/修订号/兼任等拒收）。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def validate_hour(value: str) -> str:
    """校验并归一化为 UTC 整点字符串。"""
    if not isinstance(value, str):
        raise DomainError("时间必须是 UTC 整点 ISO8601 字符串，如 2026-11-15T08:00:00Z")
    v = value.strip()
    # 兼容省略秒的写法
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:00Z$", v):
        v = v[:-1] + ":00Z"
    if not _HOUR_RE.match(v):
        raise DomainError(f"时间必须是 UTC 整点（分钟秒为 00），收到: {value!r}")
    try:
        datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise DomainError(f"非法日历时间: {value!r}") from exc
    return v


def validate_date(value: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        raise DomainError("日期格式必须为 YYYY-MM-DD")
    v = value.strip()
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError as exc:
        raise DomainError(f"非法日期: {value!r}") from exc
    return v


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def hours_of_day(date_str: str) -> list[str]:
    """返回某日 00:00Z..23:00Z 共 24 个整点。"""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return [d.replace(hour=h).strftime("%Y-%m-%dT%H:%M:%SZ") for h in range(24)]


def shift_hour(hour: str, delta_hours: int) -> str:
    """UTC 整点加减若干小时，返回规范整点字符串。"""
    d = datetime.strptime(validate_hour(hour), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    from datetime import timedelta

    return (d + timedelta(hours=delta_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class PointItem(BaseModel):
    point_id: str = Field(min_length=1, max_length=64)
    role: str

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in ("incoming", "outgoing"):
            raise DomainError("role 只能是 incoming（进线）或 outgoing（出线）")
        return v

    @field_validator("point_id")
    @classmethod
    def _point(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise DomainError("point_id 不能为空")
        return v


class ConfigSubmit(BaseModel):
    effective_hour: str
    points: list[PointItem] = Field(min_length=1)


class ReadingItem(BaseModel):
    point_id: str = Field(min_length=1, max_length=64)
    hour: str
    revision: int = Field(ge=1)
    energy: float = Field(ge=0)

    @field_validator("energy")
    @classmethod
    def _finite_energy(cls, v: float) -> float:
        if not math.isfinite(v):
            raise DomainError("电量必须是有限非负数，不接受 NaN/Infinity")
        return v

    @field_validator("point_id")
    @classmethod
    def _point(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise DomainError("point_id 不能为空")
        return v


class ReadingSubmit(BaseModel):
    readings: list[ReadingItem] = Field(min_length=1)


class ReportRequest(BaseModel):
    report_date: str
    tolerance: float = Field(ge=0)

    @field_validator("tolerance")
    @classmethod
    def _finite_tolerance(cls, v: float) -> float:
        if not math.isfinite(v):
            raise DomainError("容差必须是有限非负数，不接受 NaN/Infinity")
        return v
