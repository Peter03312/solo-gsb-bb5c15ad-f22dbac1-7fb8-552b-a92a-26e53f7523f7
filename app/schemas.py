"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _ensure_aware(value: datetime) -> datetime:
    """Reject naive datetimes; normalise any aware value to UTC."""
    if value.tzinfo is None:
        raise ValueError(
            "时间必须带时区信息，例如 2026-01-01T00:00:00Z 或 2026-01-01T08:00:00+08:00"
        )
    return value.astimezone(timezone.utc)


def _normalise_uses(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in values:
        use = raw.strip()
        if not use:
            raise ValueError("allowed_uses 不能包含空字符串")
        if use not in cleaned:
            cleaned.append(use)
    return cleaned


# -- requests --------------------------------------------------------------


class VersionCreateRequest(BaseModel):
    version_id: str | None = Field(
        default=None, min_length=1, max_length=128, description="版本标识；省略时自动生成 UUID"
    )
    parent_ids: list[str] = Field(
        default_factory=list, description="固定引用的已存在父版本（注册后不可修改）"
    )
    note: str = Field(default="", max_length=512)

    @field_validator("parent_ids")
    @classmethod
    def _unique_parents(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in values:
            pid = raw.strip()
            if not pid:
                raise ValueError("parent_ids 不能包含空字符串")
            if pid not in cleaned:
                cleaned.append(pid)
        return cleaned


class LicenseChangeRequest(BaseModel):
    change_id: str | None = Field(
        default=None, min_length=1, max_length=128, description="变更标识；省略时自动生成 UUID"
    )
    version_id: str = Field(min_length=1, description="变更所属的已登记版本")
    effective_at: datetime = Field(description="UTC 生效时刻（必须带时区）")
    allowed_uses: list[str] = Field(description="该次变更后允许的用途全集（按全集覆盖）")

    _check_time = field_validator("effective_at")(_ensure_aware)

    @field_validator("allowed_uses")
    @classmethod
    def _uses(cls, values: list[str]) -> list[str]:
        return _normalise_uses(values)


# -- responses -------------------------------------------------------------


class VersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version_id: str
    created_at: datetime
    note: str
    parent_ids: list[str]
    changes: list["LicenseChangeResponse"]


class LicenseChangeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    change_id: str
    version_id: str
    effective_at: datetime
    allowed_uses: list[str]
    content_hash: str
    created_at: datetime


class BasisEntry(BaseModel):
    version_id: str
    change_id: str
    effective_at: datetime
    allowed_uses: list[str]


class BlockerEntry(BaseModel):
    version_id: str
    reason: Literal["no_valid_license", "use_not_allowed"]
    change_id: str | None = None
    effective_at: datetime | None = None
    allowed_uses: list[str] = Field(default_factory=list)


class PermissionResponse(BaseModel):
    version_id: str
    use: str
    at: datetime
    allowed: bool
    closure: list[str] = Field(description="自身及全部祖先（去重、广度优先）")
    basis: list[BasisEntry] = Field(default_factory=list, description="各版本在该时刻所依据的最近变更")
    blockers: list[BlockerEntry] = Field(default_factory=list)
    allowed_uses_intersection: list[str] = Field(default_factory=list)


class CreateResponse(BaseModel):
    """Response wrapper that reports whether a submission was replayed."""

    replayed: bool
    change: LicenseChangeResponse | None = None
    version: VersionResponse | None = None


class ErrorDetail(BaseModel):
    error: str
    message: str
    details: dict[str, str | None] = Field(default_factory=dict)
