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
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, REAL, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 512
ALBUM_MAX_LEN = 200

# album 是 face 表的分区键。显式 COLLATE "C" 让分区路由与 locale 无关、可预测，
# 并保证 photo.album 与 face.album 用同一排序规则（否则 JOIN 时会报无法确定 collation）。
ALBUM_TYPE = String(ALBUM_MAX_LEN, collation="C")

# 列默认值走数据库函数，而不是 Python 侧 default：
# 批量 COPY / executemany 时也能拿到 uuidv7，行为与手写 SQL 一致。
_UUID7_DEFAULT = text("uuid_generate_v7()")


class Base(DeclarativeBase):
    pass


def _uuid7_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=_UUID7_DEFAULT)


class Photo(Base):
    """源站的一张照片或一个视频。

    photo_url 就是幂等写入的唯一键 —— 源站是公开静态相册，URL 稳定且天然唯一，
    不需要再额外造一个 source_asset_id。
    """

    __tablename__ = "photo"

    id: Mapped[uuid.UUID] = _uuid7_pk()

    # URL friendly 的相册标识，形如 '2026-08-10'
    album: Mapped[str] = mapped_column(ALBUM_TYPE)
    # 原图完整地址。源站公开无鉴权，可直接给前端。
    photo_url: Mapped[str] = mapped_column(Text, unique=True)

    # 源站已提供缩略图则直接落其字节，否则由 jobs/thumbnails.py 生成。
    # BYTEA 而非 base64：省 33% 体积，>2KB 走 TOAST 行外存储，主堆保持紧凑。
    thumbnail: Mapped[bytes | None] = mapped_column(LargeBinary)
    thumbnail_width: Mapped[int | None] = mapped_column(Integer)
    thumbnail_height: Mapped[int | None] = mapped_column(Integer)

    kind: Mapped[str] = mapped_column(Text, default="image")
    processing_status: Mapped[str] = mapped_column(Text, default="pending")
    processing_error: Mapped[str | None] = mapped_column(Text)
    face_count: Mapped[int] = mapped_column(Integer, default=0)
    # 「合影后排的人搜不到」的量化依据，排查召回率时第一个要看的数
    faces_discarded: Mapped[int] = mapped_column(Integer, default=0)
    embedded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    faces: Mapped[list[Face]] = relationship(back_populates="photo")


class Person(Base):
    """人脸聚类得到的簇。不分区（跨相册），不承载真实身份。"""

    __tablename__ = "person"

    id: Mapped[uuid.UUID] = _uuid7_pk()
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
    """向量主表。一张脸一行；十人合影 = 1 条 Photo + 10 条 Face。

    按 album 做 LIST 分区。分区表的主键必须包含分区键，所以 PK 是 (album, id)。
    """

    __tablename__ = "face"

    # 复合主键：album 在前，让 PK 索引同时服务按相册的前缀查找
    album: Mapped[str] = mapped_column(ALBUM_TYPE, primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_UUID7_DEFAULT
    )

    photo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photo.id", ondelete="CASCADE"))

    # 已 L2 归一化。归一化只在 embedding 服务出口做一次。
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))

    bbox_x: Mapped[int] = mapped_column(Integer)
    bbox_y: Mapped[int] = mapped_column(Integer)
    bbox_w: Mapped[int] = mapped_column(Integer)
    bbox_h: Mapped[int] = mapped_column(Integer)
    # least(bbox_w, bbox_h)。质量门控与漏检归因都依赖它。
    face_px: Mapped[int] = mapped_column(Integer)
    det_score: Mapped[float] = mapped_column(REAL)

    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL")
    )

    model_name: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(Text)
    dim: Mapped[int] = mapped_column(Integer, default=EMBEDDING_DIM)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    photo: Mapped[Photo] = relationship(back_populates="faces")


class BlockList(Base):
    """opt-out。检索必须在 SQL 层过滤，不要在应用层结果集里过滤。"""

    __tablename__ = "block_list"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    scope: Mapped[str] = mapped_column(Text)  # 'person' | 'photo'
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"))
    photo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("photo.id", ondelete="CASCADE"))
    reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AlbumSyncState(Base):
    __tablename__ = "album_sync_state"

    album: Mapped[str] = mapped_column(ALBUM_TYPE, primary_key=True)
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    photo_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class JobRun(Base):
    """离线任务执行记录。排查「召回率变差」时先看这张表。"""

    __tablename__ = "job_run"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    kind: Mapped[str] = mapped_column(Text)  # ingest | cluster | recompute
    album: Mapped[str | None] = mapped_column(ALBUM_TYPE)
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
    session_hash: Mapped[str] = mapped_column(Text)
    ip_hash: Mapped[str] = mapped_column(Text)
    album_filter: Mapped[str | None] = mapped_column(ALBUM_TYPE)
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
