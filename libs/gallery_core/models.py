"""SQLAlchemy 2.0 模型。与 docs/schema/*.sql 保持一致。

注意：DDL 的唯一真相是 docs/schema/ 下的 SQL 文件，这里的模型是它的映射。
两边都要改，且 SQL 文件只能新增不能原地改。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, REAL, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 512


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Album(Base):
    __tablename__ = "album"

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_album_id: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    # 只有 'public' 进检索库。源站转私密后必须同步到这里。
    visibility: Mapped[str] = mapped_column(Text, default="public")
    event_date: Mapped[dt.date | None] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    photos: Mapped[list[Photo]] = relationship(back_populates="album")


class Photo(Base):
    __tablename__ = "photo"

    id: Mapped[uuid.UUID] = _uuid_pk()
    album_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("album.id", ondelete="CASCADE"))

    source_asset_id: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(Text, default="image")
    filename: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    source_checksum: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    taken_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    # 二进制而非 base64：省 33% 体积，>2KB 自动走 TOAST 行外存储，主堆保持紧凑。
    thumb_webp: Mapped[bytes | None] = mapped_column(LargeBinary)
    thumb_width: Mapped[int | None] = mapped_column(Integer)
    thumb_height: Mapped[int | None] = mapped_column(Integer)

    processing_status: Mapped[str] = mapped_column(Text, default="pending")
    processing_error: Mapped[str | None] = mapped_column(Text)
    face_count: Mapped[int] = mapped_column(Integer, default=0)
    faces_discarded: Mapped[int] = mapped_column(Integer, default=0)
    embedded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    album: Mapped[Album] = relationship(back_populates="photos")
    faces: Mapped[list[Face]] = relationship(back_populates="photo")


class Person(Base):
    """人脸聚类得到的簇。不承载真实身份 —— label 默认永远是 NULL。"""

    __tablename__ = "person"

    id: Mapped[uuid.UUID] = _uuid_pk()
    label: Mapped[str | None] = mapped_column(Text)
    # 簇内成员向量均值再 L2 归一化。两段式检索的第一段就是对它做 KNN。
    centroid: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    face_count: Mapped[int] = mapped_column(Integer, default=0)
    model_name: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Face(Base):
    """一张脸一行。十人合影 = 1 条 Photo + 10 条 Face。"""

    __tablename__ = "face"

    id: Mapped[uuid.UUID] = _uuid_pk()
    photo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photo.id", ondelete="CASCADE"))
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL")
    )

    bbox_x: Mapped[int] = mapped_column(Integer)
    bbox_y: Mapped[int] = mapped_column(Integer)
    bbox_w: Mapped[int] = mapped_column(Integer)
    bbox_h: Mapped[int] = mapped_column(Integer)
    # least(bbox_w, bbox_h)。质量门控与漏检归因都依赖它。
    face_px: Mapped[int] = mapped_column(Integer)
    det_score: Mapped[float] = mapped_column(REAL)
    landmarks: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # 已 L2 归一化。归一化只在 embedding 服务出口做一次。
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))

    model_name: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(Text)
    dim: Mapped[int] = mapped_column(Integer, default=EMBEDDING_DIM)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    photo: Mapped[Photo] = relationship(back_populates="faces")


class BlockList(Base):
    """opt-out。检索必须在 SQL 层 JOIN 掉它，不要在应用层结果集里过滤。"""

    __tablename__ = "block_list"

    id: Mapped[uuid.UUID] = _uuid_pk()
    scope: Mapped[str] = mapped_column(Text)  # 'person' | 'photo'
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"))
    photo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("photo.id", ondelete="CASCADE"))
    reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SourceSyncState(Base):
    __tablename__ = "source_sync_state"

    album_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("album.id", ondelete="CASCADE"), primary_key=True
    )
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_cursor: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class JobRun(Base):
    """离线任务执行记录。排查「召回率变差」时先看这张表。"""

    __tablename__ = "job_run"

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(Text)  # ingest | cluster | recompute
    album_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("album.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Text, default="running")
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class SearchAudit(Base):
    """检索留痕。只有计数与耗时 —— 绝不存图片、向量或任何可还原查询人脸的数据。"""

    __tablename__ = "search_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # DDL 里是 TEXT（存 sha256 十六进制，64 字符），这里保持一致
    session_hash: Mapped[str] = mapped_column(Text)
    ip_hash: Mapped[str] = mapped_column(Text)
    faces_detected: Mapped[int] = mapped_column(Integer, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(Text, primary_key=True)
    applied_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
