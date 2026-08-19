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
    Boolean,
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
    # 视频时长（毫秒），照片为 NULL。见 plans/0008。
    duration_ms: Mapped[int | None] = mapped_column(Integer)
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

    # 视频 tracklet 的出现区间（毫秒），照片行恒为 NULL。见 plans/0008。
    # tracklet 是「单个视频内一段时间连续出现的脸」，不是「一个人」——
    # 不做跨视频/跨照片的身份关联（约束 #8）。
    t_start_ms: Mapped[int | None] = mapped_column(Integer)
    t_end_ms: Mapped[int | None] = mapped_column(Integer)

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
    # 剪辑域队列任务的入参（如 project_id）。老 kind 不用它。见 005_media_edit.sql。
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


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
    # 'search'（默认，查照片）| 'edit'（剪辑聊天窗）。edit 码必须绑定相册，
    # 其行 id 即剪辑工作区 workspace_id。见 docs/schema/005_media_edit.sql。
    role: Mapped[str] = mapped_column(Text, default="search")
    label: Mapped[str | None] = mapped_column(Text)
    disabled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 剪辑域（docs/schema/005_media_edit.sql，设计见 docs/plans/0005）。
# 与照片检索域是两组表：media_asset 的原片落盘在 /photo-gallery/media/<album>/，
# scene.embedding 是 Chinese-CLIP 的图像向量 —— 与 face 的人脸向量不同空间，绝不混用。
# ---------------------------------------------------------------------------


class MediaAsset(Base):
    """剪辑素材一行。source_url 是幂等键（与 photo.photo_url 同思想）。"""

    __tablename__ = "media_asset"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    album: Mapped[str] = mapped_column(ALBUM_TYPE)
    source_url: Mapped[str] = mapped_column(Text, unique=True)
    path: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default="image")

    duration_ms: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(REAL)
    codec: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(Text)
    resolution_tier: Mapped[float] = mapped_column(REAL, default=0.0)

    processing_status: Mapped[str] = mapped_column(Text, default="pending")
    processing_error: Mapped[str | None] = mapped_column(Text)
    scene_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    scenes: Mapped[list[Scene]] = relationship(back_populates="asset")


class Scene(Base):
    """剪辑检索的基本单元。视频一个镜头一行；照片整张即一个 scene。"""

    __tablename__ = "scene"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_asset.id", ondelete="CASCADE"))
    # 冗余自 media_asset：检索外层过滤直接用，免 join
    album: Mapped[str] = mapped_column(ALBUM_TYPE)

    start_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)

    keyframe: Mapped[bytes | None] = mapped_column(LargeBinary)
    keyframe_width: Mapped[int | None] = mapped_column(Integer)
    keyframe_height: Mapped[int | None] = mapped_column(Integer)

    # Chinese-CLIP 图像塔输出，出口 L2 归一化（约束 4）
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    model_name: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(Text)
    dim: Mapped[int] = mapped_column(Integer, default=EMBEDDING_DIM)

    # 画质列，0~1。照片没有稳定性概念，恒为 1。
    stability: Mapped[float] = mapped_column(REAL, default=1.0)
    sharpness: Mapped[float] = mapped_column(REAL, default=0.0)
    exposure: Mapped[float] = mapped_column(REAL, default=0.0)
    quality_score: Mapped[float] = mapped_column(REAL, default=0.0)
    face_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    asset: Mapped[MediaAsset] = relationship(back_populates="scenes")


class FilterPreset(Base):
    """滤镜库。一切滤镜都是 3D LUT（内置预设在导入时由代码生成）。"""

    __tablename__ = "filter_preset"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    lut: Mapped[bytes] = mapped_column(LargeBinary)
    preview: Mapped[bytes | None] = mapped_column(LargeBinary)
    checksum: Mapped[str] = mapped_column(Text)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EditProject(Base):
    """一次剪辑任务（聊天窗里的一个会话）。用户提交剧本时隐式创建。"""

    __tablename__ = "edit_project"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invite_code.id"))
    # 冗余自 invite_code，创建时固化 —— 之后改绑码也不影响存量项目
    album: Mapped[str] = mapped_column(ALBUM_TYPE)
    title: Mapped[str] = mapped_column(Text, default="")
    script: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(Text, default="ingesting")
    error: Mapped[str | None] = mapped_column(Text)
    current_round: Mapped[int] = mapped_column(Integer, default=1)
    # 乐观并发控制：双设备同时操作时，过期写返回 409
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    default_filter_slug: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    shots: Mapped[list[Shot]] = relationship(back_populates="project")


class EditRound(Base):
    """反馈闭环留痕。第 N+1 轮的 LLM 上下文按轮次全量组装自这张表。"""

    __tablename__ = "edit_round"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("edit_project.id", ondelete="CASCADE"))
    round_no: Mapped[int] = mapped_column(Integer)
    user_note: Mapped[str | None] = mapped_column(Text)
    # {shot_idx: 反馈原文}
    shot_feedback: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    shot_list: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    llm_model: Mapped[str | None] = mapped_column(Text)
    prompt_fingerprint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Shot(Base):
    """镜头一行。locked 后后续轮次绝不重写（评审反馈闭环的硬约定）。"""

    __tablename__ = "shot"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("edit_project.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer)
    source_text: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    queries: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    media_kind: Mapped[str] = mapped_column(Text, default="any")
    min_ms: Mapped[int | None] = mapped_column(Integer)
    max_ms: Mapped[int | None] = mapped_column(Integer)
    filter_slug: Mapped[str | None] = mapped_column(String(100))

    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_candidate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    feedback: Mapped[str | None] = mapped_column(Text)
    round_no: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped[EditProject] = relationship(back_populates="shots")
    candidates: Mapped[list[ShotCandidate]] = relationship(back_populates="shot")


class ShotCandidate(Base):
    """镜头×scene 候选。rejected 的 scene 在该镜头后续轮次的检索里排除（负反馈）。"""

    __tablename__ = "shot_candidate"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    shot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shot.id", ondelete="CASCADE"))
    scene_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scene.id", ondelete="CASCADE"))
    round_no: Mapped[int] = mapped_column(Integer, default=1)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    similarity: Mapped[float] = mapped_column(REAL, default=0.0)
    quality: Mapped[float] = mapped_column(REAL, default=0.0)
    final_score: Mapped[float] = mapped_column(REAL, default=0.0)
    status: Mapped[str] = mapped_column(Text, default="pending")
    in_ms: Mapped[int | None] = mapped_column(Integer)
    out_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    shot: Mapped[Shot] = relationship(back_populates="candidates")


class RenderOutput(Base):
    """渲染产物。滤镜记 slug+checksum，可追溯可复现。"""

    __tablename__ = "render_output"

    id: Mapped[uuid.UUID] = _uuid7_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("edit_project.id", ondelete="CASCADE"))
    shot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shot.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    precise_in_ms: Mapped[int | None] = mapped_column(Integer)
    precise_out_ms: Mapped[int | None] = mapped_column(Integer)
    padded_in_ms: Mapped[int | None] = mapped_column(Integer)
    padded_out_ms: Mapped[int | None] = mapped_column(Integer)
    filter_slug: Mapped[str | None] = mapped_column(String(100))
    filter_checksum: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(Text, default="crf16")
    ffmpeg_args: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProjectEvent(Base):
    """追加式事件时间线，聊天窗的数据源。

    事件流是展示层，状态表才是事实层：只追加不改写，payload 只存小快照 + id 引用。
    绝不允许出现 embedding 或任何图片字节（约束 2 延伸适用）。
    """

    __tablename__ = "project_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("edit_project.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(Text)  # 'user' | 'assistant' | 'system'
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
