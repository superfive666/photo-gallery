"""渲染导出：锁定镜头 → ffmpeg 帧精确剪裁 + LUT 烧入 → output/<workspace>/<project>/。

质量纪律（见 docs/plans/0005「关于重编码与质量」）：
  · 不缩放 —— 输出保持源片原分辨率；
  · 默认 H.264 CRF16，视觉无损；重编码只发生在导出的片段文件上，原片永不改写；
  · 评审确认的 in/out 点前后各留 1s 余量；manifest 同时记「精确点」与「含余量点」。
"""

from __future__ import annotations

import asyncio
import csv
import io
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


def _render_image(src: Path, dst: Path, lut_bytes: bytes | None) -> None:
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(src) as im:
        oriented = ImageOps.exif_transpose(im) or im
        rgb = np.asarray(oriented.convert("RGB"), dtype=np.uint8)
    if lut_bytes is not None:
        rgb = apply_lut(rgb, parse_cube(lut_bytes))
    Image.fromarray(rgb, mode="RGB").save(dst, format="JPEG", quality=90)


@dataclass(slots=True)
class _RenderItem:
    shot: Shot
    candidate: ShotCandidate
    scene: Scene
    asset: MediaAsset
    preset: FilterPreset | None


async def _load_items(session: AsyncSession, project: EditProject) -> list[_RenderItem]:
    shots = (
        (
            await session.execute(
                select(Shot).where(Shot.project_id == project.id).order_by(Shot.idx)
            )
        )
        .scalars()
        .all()
    )
    items: list[_RenderItem] = []
    for shot in shots:
        if not shot.locked or shot.locked_candidate_id is None:
            raise RenderError(f"镜头 {shot.idx} 尚未锁定，不能渲染")
        candidate = await session.get(ShotCandidate, shot.locked_candidate_id)
        if candidate is None:
            raise RenderError(f"镜头 {shot.idx} 锁定的候选不存在")
        scene = await session.get(Scene, candidate.scene_id)
        if scene is None:
            raise RenderError(f"镜头 {shot.idx} 的候选 scene 不存在")
        asset = await session.get(MediaAsset, scene.asset_id)
        if asset is None:
            raise RenderError(f"镜头 {shot.idx} 的素材记录不存在")

        preset: FilterPreset | None = None
        slug = shot.filter_slug or project.default_filter_slug
        if slug:
            preset = (
                await session.execute(select(FilterPreset).where(FilterPreset.slug == slug))
            ).scalar_one_or_none()
        items.append(
            _RenderItem(shot=shot, candidate=candidate, scene=scene, asset=asset, preset=preset)
        )
    return items


async def render_project(
    session: AsyncSession, settings: Settings, project_id: uuid.UUID
) -> dict[str, object]:
    """渲染一个项目的全部锁定镜头。产出片段/照片 + manifest.csv + 打包 zip。"""
    project = await session.get(EditProject, project_id)
    if project is None:
        raise RenderError(f"项目不存在: {project_id}")

    items = await _load_items(session, project)
    if not items:
        raise RenderError("没有可渲染的镜头")

    out_root = Path(settings.output_dir(str(project.workspace_id)))
    proj_dir = out_root / project_dirname(project.id, project.title)
    await asyncio.to_thread(proj_dir.mkdir, parents=True, exist_ok=True)

    manifest_rows: list[dict[str, object]] = []
    rendered_files: list[Path] = []

    for item in items:
        shot, cand, scene, asset = item.shot, item.candidate, item.scene, item.asset
        # 「原色」是恒等 LUT，烧不烧结果一样 —— 跳过烧入省一次全帧计算
        lut_bytes = (
            item.preset.lut if item.preset is not None and item.preset.slug != "original" else None
        )

        if asset.kind == "video":
            precise_in = cand.in_ms if cand.in_ms is not None else scene.start_ms
            precise_out = cand.out_ms if cand.out_ms is not None else scene.end_ms
            padded_in = max(0, precise_in - settings.render_handle_ms)
            duration = asset.duration_ms or precise_out
            padded_out = min(duration, precise_out + settings.render_handle_ms)

            dst = proj_dir / f"{shot.idx:02d}_{slugify(shot.description, 24)}.mp4"
            lut_arg: str | None = None
            args: list[str]
            if lut_bytes is not None:
                with tempfile.NamedTemporaryFile(suffix=".cube", delete=False) as tmp:
                    tmp.write(lut_bytes)
                    lut_arg = tmp.name
            try:
                args = build_ffmpeg_args(
                    str(asset.path),
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
            kind = "video"
        else:
            precise_in = precise_out = padded_in = padded_out = 0
            dst = proj_dir / f"{shot.idx:02d}_{slugify(shot.description, 24)}.jpg"
            await asyncio.to_thread(_render_image, Path(asset.path), dst, lut_bytes)
            args = []
            kind = "image"

        size = (await asyncio.to_thread(dst.stat)).st_size
        rendered_files.append(dst)
        session.add(
            RenderOutput(
                project_id=project.id,
                shot_id=shot.id,
                path=str(dst),
                kind=kind,
                precise_in_ms=precise_in,
                precise_out_ms=precise_out,
                padded_in_ms=padded_in,
                padded_out_ms=padded_out,
                filter_slug=item.preset.slug if item.preset else None,
                filter_checksum=item.preset.checksum if item.preset else None,
                tier="crf16",
                ffmpeg_args=" ".join(args) if args else None,
                size_bytes=size,
            )
        )
        manifest_rows.append(
            {
                "shot": shot.idx,
                "description": shot.description,
                "file": dst.name,
                "kind": kind,
                "source_file": Path(asset.path).name,
                "album": asset.album,
                "precise_in_ms": precise_in,
                "precise_out_ms": precise_out,
                "padded_in_ms": padded_in,
                "padded_out_ms": padded_out,
                "filter": item.preset.slug if item.preset else "",
            }
        )
        log.info("shot_rendered", shot=shot.idx, kind=kind, bytes=size)

    manifest_path = proj_dir / "manifest.csv"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(manifest_rows[0].keys()))
    writer.writeheader()
    writer.writerows(manifest_rows)
    await asyncio.to_thread(manifest_path.write_text, buf.getvalue(), "utf-8")

    zip_path = proj_dir / f"{project_dirname(project.id, project.title)}.zip"

    def _make_zip() -> None:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for f in [*rendered_files, manifest_path]:
                zf.write(f, arcname=f.name)

    await asyncio.to_thread(_make_zip)

    total_bytes = 0
    for f in rendered_files:
        total_bytes += (await asyncio.to_thread(f.stat)).st_size

    return {
        "shots": len(items),
        "dir": str(proj_dir),
        "zip": str(zip_path),
        "bytes": total_bytes,
    }
