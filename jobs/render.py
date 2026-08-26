"""渲染导出：锁定镜头 → ffmpeg 帧精确剪裁 + LUT 烧入 → output/<workspace>/<project>/。

质量纪律（见 docs/plans/0005「关于重编码与质量」）：
  · 不缩放 —— 输出保持源片原分辨率；
  · 默认 H.264 CRF16，视觉无损；重编码只发生在导出的片段文件上，原片永不改写；
  · 评审确认的 in/out 点前后各留 1s 余量；manifest 同时记「精确点」与「含余量点」。

预渲染（plans/0012）：镜头一锁定就由 shot_render 任务把片段剪出来，用户还在评审
下一个镜头时重活已经在跑。片段旁写指纹 sidecar（<文件名>.fp），内容是入参哈希 ——
最终 render_project 逐项核对指纹，已剪好且入参没变的直接复用，只补漏 + 打包。
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_core.config import Settings
from gallery_core.db import session_scope
from gallery_core.logging import get_logger
from gallery_core.models import (
    EditProject,
    FilterPreset,
    MediaAsset,
    RenderOutput,
    Scene,
    Shot,
    ShotCandidate,
)
from jobs.luts import apply_lut, parse_cube
from jobs.sources.base import SourceAdapter, SourceAsset

log = get_logger(__name__)

_FFMPEG_TIMEOUT = 1800  # 单段 30 分钟兜底：卡死的 ffmpeg 不能拖住整个 worker


class RenderError(RuntimeError):
    pass


def slugify(title: str, max_len: int = 40) -> str:
    """目录/文件名安全的 slug。中文保留（现代文件系统没问题），符号替换成连字符。"""
    cleaned = re.sub(r"[^\w一-鿿-]+", "-", title.strip()).strip("-")
    return cleaned[:max_len] or "untitled"


def project_dirname(project_id: uuid.UUID, title: str) -> str:
    """「id 短前缀 + 标题 slug」：既唯一，又能在磁盘上一眼认出是哪次剪辑。"""
    return f"{str(project_id)[:8]}-{slugify(title)}"


def build_ffmpeg_args(
    src: str,
    dst: str,
    in_ms: int,
    out_ms: int,
    *,
    lut_path: str | None,
    crf: int,
    preset: str,
) -> list[str]:
    """纯函数，方便单测。-ss 放在 -i 前做快速 seek，配合重编码即帧精确。"""
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{in_ms / 1000:.3f}",
        "-to",
        f"{out_ms / 1000:.3f}",
        "-i",
        src,
    ]
    if lut_path is not None:
        args += ["-vf", f"lut3d=file='{lut_path}'"]
    args += [
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        dst,
    ]
    return args


def _run_ffmpeg(args: list[str]) -> None:
    try:
        # 入参是我们自己拼的固定命令 + 本地文件路径，无 shell 注入面
        proc = subprocess.run(  # noqa: S603
            args, capture_output=True, timeout=_FFMPEG_TIMEOUT, check=False
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RenderError(f"ffmpeg 失败: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-400:]
        raise RenderError(f"ffmpeg 退出码 {proc.returncode}: {tail}")


def _render_image(payload: bytes, dst: Path, lut_bytes: bytes | None) -> None:
    """照片导出：套 LUT 存 JPEG。吃字节 —— 照片可能不在本地（006 迁移，渲染时现下载）。"""
    import io

    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(payload)) as im:
        oriented = ImageOps.exif_transpose(im) or im
        rgb = np.asarray(oriented.convert("RGB"), dtype=np.uint8)
    if lut_bytes is not None:
        rgb = apply_lut(rgb, parse_cube(lut_bytes))
    Image.fromarray(rgb, mode="RGB").save(dst, format="JPEG", quality=90)


async def _load_image_bytes(item: _PlannedShot, adapter: SourceAdapter | None) -> bytes:
    """取照片原图字节：本地文件优先；没有就按 source_url 从源站现下载。

    照片不落盘是刻意的（省本地空间）—— 渲染是低频、单张、小文件，
    现下载比常年占盘便宜得多。下载走 adapter，沿用其限速与重试纪律。
    """
    if item.asset_path:
        local = Path(item.asset_path)
        if await asyncio.to_thread(local.is_file):
            return await asyncio.to_thread(local.read_bytes)
    if item.asset_source_url.startswith("local://"):
        raise RenderError(
            f"照片原图缺失且无法远程获取（{item.asset_source_url}）——文件被移动或删除了？"
        )
    if adapter is None:
        raise RenderError("照片在远端但渲染器没有源站 adapter，无法下载原图")
    source = SourceAsset(
        album=item.asset_album,
        filename=Path(item.asset_source_url).name,
        photo_url=item.asset_source_url,
        kind="image",
    )
    chunks: list[bytes] = []
    async for chunk in adapter.open_asset(source):
        chunks.append(chunk)
    return b"".join(chunks)


def output_stem(idx: int, description: str, role: str) -> str:
    """导出文件名（不含扩展名）。备选带 _alt 后缀，与主选相邻排序，
    manifest 的 role 列是同一信息的机器可读版。"""
    stem = f"{idx:02d}_{slugify(description, 24)}"
    return stem if role == "primary" else f"{stem}_alt"


@dataclass(slots=True)
class _PlannedShot:
    """渲染所需的全部输入，材料化成纯值 —— 重活（ffmpeg）期间不持有 ORM 对象与连接。"""

    shot_id: uuid.UUID
    idx: int
    # primary = 主选，backup = 备选（随主选一同导出，后期二选一）
    role: str
    description: str
    asset_kind: str
    asset_path: str | None
    asset_source_url: str
    asset_album: str
    asset_checksum: str | None
    asset_duration_ms: int | None
    scene_start_ms: int
    scene_end_ms: int
    in_ms: int | None
    out_ms: int | None
    preset_slug: str | None
    preset_checksum: str | None
    lut_bytes: bytes | None


@dataclass(slots=True)
class _ProjectPlan:
    project_id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    shots: list[_PlannedShot]


# ---------------------------------------------------------------------------
# 预渲染缓存（plans/0012）：指纹 sidecar
# ---------------------------------------------------------------------------


def timecodes(item: _PlannedShot, settings: Settings) -> tuple[int, int, int, int]:
    """(precise_in, precise_out, padded_in, padded_out)。照片恒为 0。"""
    if item.asset_kind != "video":
        return 0, 0, 0, 0
    precise_in = item.in_ms if item.in_ms is not None else item.scene_start_ms
    precise_out = item.out_ms if item.out_ms is not None else item.scene_end_ms
    padded_in = max(0, precise_in - settings.render_handle_ms)
    duration = item.asset_duration_ms or precise_out
    padded_out = min(duration, precise_out + settings.render_handle_ms)
    return precise_in, precise_out, padded_in, padded_out


def clip_fingerprint(item: _PlannedShot, settings: Settings) -> str:
    """一个片段文件的入参哈希。任何会改变输出内容的输入都必须参与 ——
    候选/时码/滤镜/编码参数变了指纹就变，预渲染产物即作废重剪。"""
    precise_in, precise_out, padded_in, padded_out = timecodes(item, settings)
    payload = json.dumps(
        {
            "kind": item.asset_kind,
            "src": item.asset_path or item.asset_source_url,
            "checksum": item.asset_checksum,
            "precise": [precise_in, precise_out],
            "padded": [padded_in, padded_out],
            "filter": [item.preset_slug, item.preset_checksum],
            "encode": [settings.render_crf, settings.render_preset],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _sidecar(dst: Path) -> Path:
    return dst.with_name(f"{dst.name}.fp")


def is_cached(dst: Path, fingerprint: str) -> bool:
    """片段已存在且指纹一致 → 可直接复用，跳过 ffmpeg。"""
    sidecar = _sidecar(dst)
    try:
        return dst.is_file() and sidecar.read_text("utf-8").strip() == fingerprint
    except OSError:
        return False


def write_fingerprint(dst: Path, fingerprint: str) -> None:
    _sidecar(dst).write_text(fingerprint, "utf-8")


# ---------------------------------------------------------------------------
# 计划材料化与单片段渲染
# ---------------------------------------------------------------------------


async def _plan_shot(
    session: AsyncSession, project: EditProject, shot: Shot
) -> list[_PlannedShot]:
    """一个镜头 → 1~2 条渲染项（主选 + 可选备选）。调用方持有会话。"""
    preset: FilterPreset | None = None
    slug = shot.filter_slug or project.default_filter_slug
    if slug:
        preset = (
            await session.execute(select(FilterPreset).where(FilterPreset.slug == slug))
        ).scalar_one_or_none()
    # 「原色」是恒等 LUT，烧不烧结果一样 —— 跳过烧入省一次全帧计算
    lut_bytes = preset.lut if preset is not None and preset.slug != "original" else None

    # 主选 + 可选备选（007）：同一镜头出两个文件，manifest 标 role
    picks = [("primary", shot.locked_candidate_id)]
    if shot.backup_candidate_id is not None:
        picks.append(("backup", shot.backup_candidate_id))

    planned: list[_PlannedShot] = []
    for role, candidate_id in picks:
        label = "主选" if role == "primary" else "备选"
        candidate = await session.get(ShotCandidate, candidate_id)
        if candidate is None:
            raise RenderError(f"镜头 {shot.idx} 锁定的{label}候选不存在")
        scene = await session.get(Scene, candidate.scene_id)
        if scene is None:
            raise RenderError(f"镜头 {shot.idx} 的{label}候选 scene 不存在")
        asset = await session.get(MediaAsset, scene.asset_id)
        if asset is None:
            raise RenderError(f"镜头 {shot.idx} 的{label}素材记录不存在")

        planned.append(
            _PlannedShot(
                shot_id=shot.id,
                idx=shot.idx,
                role=role,
                description=shot.description,
                asset_kind=asset.kind,
                asset_path=asset.path,
                asset_source_url=asset.source_url,
                asset_album=asset.album,
                asset_checksum=asset.checksum,
                asset_duration_ms=asset.duration_ms,
                scene_start_ms=scene.start_ms,
                scene_end_ms=scene.end_ms,
                in_ms=candidate.in_ms,
                out_ms=candidate.out_ms,
                preset_slug=preset.slug if preset else None,
                preset_checksum=preset.checksum if preset else None,
                lut_bytes=lut_bytes,
            )
        )
    return planned


async def _load_plan(
    settings: Settings, project_id: uuid.UUID, only_shot_id: uuid.UUID | None = None
) -> _ProjectPlan:
    """一次短事务读出渲染所需的一切并材料化。之后整个渲染过程不再占用连接。

    only_shot_id：预渲染单个镜头。该镜头未锁定/不存在时返回空计划（不报错 ——
    任务在队列里排队期间用户可能已撤销锁定，这是正常竞态不是故障）。
    整项目模式下任何未锁定镜头都是硬错误（start_render 已挡，这里防御纵深）。
    """
    async with session_scope() as session:
        project = await session.get(EditProject, project_id)
        if project is None:
            raise RenderError(f"项目不存在: {project_id}")

        stmt = select(Shot).where(Shot.project_id == project.id).order_by(Shot.idx)
        if only_shot_id is not None:
            stmt = stmt.where(Shot.id == only_shot_id)
        shots = (await session.execute(stmt)).scalars().all()

        planned: list[_PlannedShot] = []
        for shot in shots:
            if not shot.locked or shot.locked_candidate_id is None:
                if only_shot_id is not None:
                    continue
                raise RenderError(f"镜头 {shot.idx} 尚未锁定，不能渲染")
            planned.extend(await _plan_shot(session, project, shot))
        return _ProjectPlan(
            project_id=project.id,
            workspace_id=project.workspace_id,
            title=project.title,
            shots=planned,
        )


@dataclass(slots=True)
class _RenderedClip:
    """一个片段文件的渲染结果（可能来自缓存复用）。"""

    dst: Path
    kind: str
    precise_in: int
    precise_out: int
    padded_in: int
    padded_out: int
    ffmpeg_args: str | None
    size_bytes: int
    cached: bool


async def _render_item(
    settings: Settings,
    item: _PlannedShot,
    proj_dir: Path,
    adapter: SourceAdapter | None,
) -> _RenderedClip:
    """渲染一条计划项到 proj_dir。指纹一致的既有产物直接复用（预渲染缓存命中）。"""
    precise_in, precise_out, padded_in, padded_out = timecodes(item, settings)
    ext = "mp4" if item.asset_kind == "video" else "jpg"
    dst = proj_dir / f"{output_stem(item.idx, item.description, item.role)}.{ext}"
    fingerprint = clip_fingerprint(item, settings)

    if await asyncio.to_thread(is_cached, dst, fingerprint):
        size = (await asyncio.to_thread(dst.stat)).st_size
        return _RenderedClip(
            dst=dst,
            kind=item.asset_kind,
            precise_in=precise_in,
            precise_out=precise_out,
            padded_in=padded_in,
            padded_out=padded_out,
            ffmpeg_args=None,
            size_bytes=size,
            cached=True,
        )

    args: list[str] = []
    if item.asset_kind == "video":
        if not item.asset_path:
            raise RenderError(f"镜头 {item.idx} 的视频没有本地文件记录")
        lut_arg: str | None = None
        if item.lut_bytes is not None:
            with tempfile.NamedTemporaryFile(suffix=".cube", delete=False) as tmp:
                tmp.write(item.lut_bytes)
                lut_arg = tmp.name
        try:
            args = build_ffmpeg_args(
                item.asset_path,
                str(dst),
                padded_in,
                padded_out,
                lut_path=lut_arg,
                crf=settings.render_crf,
                preset=settings.render_preset,
            )
            await asyncio.to_thread(_run_ffmpeg, args)
        finally:
            if lut_arg is not None:
                await asyncio.to_thread(Path(lut_arg).unlink, True)
    else:
        payload = await _load_image_bytes(item, adapter)
        await asyncio.to_thread(_render_image, payload, dst, item.lut_bytes)
        del payload

    await asyncio.to_thread(write_fingerprint, dst, fingerprint)
    size = (await asyncio.to_thread(dst.stat)).st_size
    return _RenderedClip(
        dst=dst,
        kind=item.asset_kind,
        precise_in=precise_in,
        precise_out=precise_out,
        padded_in=padded_in,
        padded_out=padded_out,
        ffmpeg_args=" ".join(args) if args else None,
        size_bytes=size,
        cached=False,
    )


def _project_dir(settings: Settings, plan: _ProjectPlan) -> Path:
    return Path(settings.output_dir(str(plan.workspace_id))) / project_dirname(
        plan.project_id, plan.title
    )


# ---------------------------------------------------------------------------
# 两个入口：单镜头预渲染（锁定即触发）与整项目最终渲染（补漏 + 打包）
# ---------------------------------------------------------------------------


async def render_shot(
    settings: Settings,
    project_id: uuid.UUID,
    shot_id: uuid.UUID,
    adapter: SourceAdapter | None = None,
) -> dict[str, object]:
    """预渲染一个已锁定的镜头（主选 + 备选）。锁定动作一发生就入队执行，
    用户还在评审后面的镜头时重活已经在跑 —— 最终渲染只剩核对指纹与打包。

    纯优化路径：镜头已被撤销锁定时静默跳过；失败只标 job 失败，不动项目状态
    （最终渲染会重剪，那才是有权判项目失败的路径）。
    """
    plan = await _load_plan(settings, project_id, only_shot_id=shot_id)
    if not plan.shots:
        return {"clips": 0, "skipped": "镜头未锁定或不存在"}

    proj_dir = _project_dir(settings, plan)
    await asyncio.to_thread(proj_dir.mkdir, parents=True, exist_ok=True)

    rendered = 0
    cached = 0
    for item in plan.shots:
        clip = await _render_item(settings, item, proj_dir, adapter)
        cached += clip.cached
        rendered += not clip.cached
        log.info(
            "shot_prerendered",
            shot=item.idx,
            role=item.role,
            cached=clip.cached,
            bytes=clip.size_bytes,
        )
    return {
        "idx": plan.shots[0].idx,
        "clips": len(plan.shots),
        "rendered": rendered,
        "cached": cached,
    }


async def render_project(
    settings: Settings,
    project_id: uuid.UUID,
    adapter: SourceAdapter | None = None,
) -> dict[str, object]:
    """渲染一个项目的全部锁定镜头。产出片段/照片 + manifest.csv + 打包 zip。

    预渲染命中的片段直接复用（指纹一致才算命中），通常这里只剩打包。

    ⚠️ 连接纪律：ffmpeg 转码以分钟计、多镜头项目以十分钟计 —— 本函数不持有
    数据库连接跨过重活。读取在 _load_plan 的短事务里材料化，结果在最后一个
    短事务里写回（与 media_ingest 同一纪律）。
    """
    plan = await _load_plan(settings, project_id)
    if not plan.shots:
        raise RenderError("没有可渲染的镜头")

    proj_dir = _project_dir(settings, plan)
    await asyncio.to_thread(proj_dir.mkdir, parents=True, exist_ok=True)

    manifest_rows: list[dict[str, object]] = []
    rendered_files: list[Path] = []
    outputs: list[RenderOutput] = []
    reused = 0

    for item in plan.shots:
        clip = await _render_item(settings, item, proj_dir, adapter)
        reused += clip.cached
        rendered_files.append(clip.dst)
        outputs.append(
            RenderOutput(
                project_id=plan.project_id,
                shot_id=item.shot_id,
                path=str(clip.dst),
                kind=clip.kind,
                precise_in_ms=clip.precise_in,
                precise_out_ms=clip.precise_out,
                padded_in_ms=clip.padded_in,
                padded_out_ms=clip.padded_out,
                filter_slug=item.preset_slug,
                filter_checksum=item.preset_checksum,
                tier="crf16",
                ffmpeg_args=clip.ffmpeg_args,
                size_bytes=clip.size_bytes,
            )
        )
        manifest_rows.append(
            {
                "shot": item.idx,
                "role": item.role,
                "description": item.description,
                "file": clip.dst.name,
                "kind": clip.kind,
                # 远端照片没有本地文件，manifest 里记源站 URL 的文件名，后期同样能对回原片
                "source_file": Path(item.asset_path).name
                if item.asset_path
                else Path(item.asset_source_url).name,
                "album": item.asset_album,
                "precise_in_ms": clip.precise_in,
                "precise_out_ms": clip.precise_out,
                "padded_in_ms": clip.padded_in,
                "padded_out_ms": clip.padded_out,
                "filter": item.preset_slug or "",
            }
        )
        log.info(
            "shot_rendered",
            shot=item.idx,
            role=item.role,
            kind=clip.kind,
            cached=clip.cached,
            bytes=clip.size_bytes,
        )

    manifest_path = proj_dir / "manifest.csv"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(manifest_rows[0].keys()))
    writer.writeheader()
    writer.writerows(manifest_rows)
    await asyncio.to_thread(manifest_path.write_text, buf.getvalue(), "utf-8")

    zip_path = proj_dir / f"{project_dirname(plan.project_id, plan.title)}.zip"

    def _make_zip() -> None:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for f in [*rendered_files, manifest_path]:
                zf.write(f, arcname=f.name)

    await asyncio.to_thread(_make_zip)

    # 全部重活结束后，用一个短事务写回渲染留痕
    async with session_scope() as session:
        session.add_all(outputs)

    total_bytes = 0
    for f in rendered_files:
        total_bytes += (await asyncio.to_thread(f.stat)).st_size

    return {
        "shots": len(plan.shots),
        "reused": reused,
        "dir": str(proj_dir),
        "zip": str(zip_path),
        "bytes": total_bytes,
    }
