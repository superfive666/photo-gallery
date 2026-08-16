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
ALBUM_TYPE = String(ALBUM_MAX_LEN)

# 列默认值走数据库函数，而不是 Python 侧 default：
# 批量 COPY / executemany 时也能拿到 uuidv7，行为与手写 SQL 一致。
_UUID7_DEFAULT = text("uuid_generate_v7()")


class Base(DeclarativeBase):
    pass


def _uuid7_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=_UUID7_DEFAULT)


class Photo(Base):
    """源站的一张照片或一个视频。

    photo_url 就是幂等写入的唯一键 —— 源站是公开静态相册，URL 稳定且天然唯一。

    album 只属于 photo：一张照片属于哪个相册是它自己的属性，与它上面有几张脸无关。
    face 通过 photo_id 外键间接得到 album。
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


class Face(Base):
    """向量主表。一张脸一行；十人合影 = 1 条 Photo + 10 条 Face。

    普通表，不分区 —— 十万级向量单个 HNSW 索引的 KNN 就是毫秒级。
    """

    __tablename__ = "face"

    id: Mapped[uuid.UUID] = _uuid7_pk()
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

    # 浏览模式的人脸小图（160px WebP，入库时从原图按 bbox 裁出）。
    # 可空：003 之前的存量脸用 `jobs face-thumbs` 回填。见 plans/0007。
    thumb: Mapped[bytes | None] = mapped_column(LargeBinary)

    model_name: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(Text)
    dim: Mapped[int] = mapped_column(Integer, default=EMBEDDING_DIM)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    photo: Mapped[Photo] = relationship(back_populates="faces")


class BlockList(Base):
    """opt-out。检索必须在 SQL 层过滤，不要在应用层结果集里过滤。

    没有 person 表，所以「屏蔽某个人」= 屏蔽他的那一批 face。
    用 `jobs block --selfie <路径>` 一条命令完成。
    """

    __tablename__ = "block_list"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    scope: Mapped[str] = mapped_column(Text)  # 'face' | 'photo'
    face_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("face.id", ondelete="CASCADE"))
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
    kind: Mapped[str] = mapped_column(Text)  # ingest | recompute
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


class InviteCode(Base):
    """邀请码 ↔ 相册绑定。见 docs/schema/002_invite_code.sql 与 plans/0006。

    码的形态是 `<prefix>.<secret>`：prefix 公开、唯一索引定位行，secret 只存
    argon2 hash —— 登录因此只做一次 argon2 验证。album 为 NULL 是全相册管理码。
    吊销置 disabled_at，不删行。
    """

    __tablename__ = "invite_code"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    code_hash: Mapped[str] = mapped_column(Text)
    album: Mapped[str | None] = mapped_column(ALBUM_TYPE)
    label: Mapped[str | None] = mapped_column(Text)
    disabled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
