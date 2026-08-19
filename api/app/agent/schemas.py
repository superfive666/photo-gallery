"""agent 各节点的输出契约（pydantic）。

坏输出在这里被挡住，不会脏到状态表 —— 私有模型能力强弱只影响单节点输出质量，
永远不会把状态机带偏。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ShotDraft(BaseModel):
    idx: int = Field(ge=1, le=200)
    source_text: str = ""
    description: str = Field(min_length=1, max_length=500)
    queries: list[str] = Field(min_length=1, max_length=3)
    media_kind: Literal["any", "image", "video"] = "any"
    min_seconds: float | None = Field(default=None, ge=0)
    max_seconds: float | None = Field(default=None, ge=0)

    @field_validator("queries")
    @classmethod
    def _non_empty_queries(cls, v: list[str]) -> list[str]:
        cleaned = [q.strip() for q in v if q.strip()]
        if not cleaned:
            raise ValueError("queries 不能全为空")
        return cleaned[:3]


class ShotListOut(BaseModel):
    title: str = ""
    default_filter_slug: str | None = None
    shots: list[ShotDraft] = Field(min_length=1, max_length=200)


class RefinedShot(BaseModel):
    idx: int = Field(ge=1, le=200)
    description: str = Field(min_length=1, max_length=500)
    queries: list[str] = Field(min_length=1, max_length=3)

    @field_validator("queries")
    @classmethod
    def _non_empty_queries(cls, v: list[str]) -> list[str]:
        cleaned = [q.strip() for q in v if q.strip()]
        if not cleaned:
            raise ValueError("queries 不能全为空")
        return cleaned[:3]


class RefineOut(BaseModel):
    shots: list[RefinedShot] = Field(max_length=200)
