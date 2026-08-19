"""滤镜库导入：/photo-gallery/luts/*.cube + 内置预设 → filter_preset 表。

幂等：按 slug upsert；checksum 未变则跳过。畸形 .cube 明确报错跳过并计数，
不静默入库。吊销用 --disable <slug> 软下架（enabled=false），不删行 ——
render_output 里引用它的历史记录还要能解释。
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from typing import cast as _cast

from PIL import Image
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_core.logging import get_logger
from gallery_core.models import FilterPreset
from jobs.luts import Lut, LutError, apply_lut, builtin_presets, parse_cube, preview_test_image

log = get_logger(__name__)

_PREVIEW_QUALITY = 80


@dataclass
class FilterImportStats:
    imported: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "imported": self.imported,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "rejected": self.rejected,
            "errors": self.errors[:20],
        }


def _render_preview(lut: Lut) -> bytes:
    """对标准测试图套用 LUT，产出预览 JPEG。所有预设基于同一张图，可横向比较。"""
    rendered = apply_lut(preview_test_image(), lut)
    buf = io.BytesIO()
    Image.fromarray(rendered, mode="RGB").save(buf, format="JPEG", quality=_PREVIEW_QUALITY)
    return buf.getvalue()


def _slug_from_filename(path: Path) -> str:
    # 文件名即 slug：小写、空格转连字符。改文件重跑即更新同一行。
    return path.stem.strip().lower().replace(" ", "-")[:100]


async def _upsert(
    session: AsyncSession,
    stats: FilterImportStats,
    *,
    slug: str,
    display_name: str,
    lut: Lut,
    cube_bytes: bytes,
    builtin: bool,
) -> None:
    existing = (
        await session.execute(select(FilterPreset).where(FilterPreset.slug == slug))
    ).scalar_one_or_none()

    checksum = lut.checksum
    if existing is not None and existing.checksum == checksum:
        stats.unchanged += 1
        return

    preview = await asyncio.to_thread(_render_preview, lut)
    if existing is None:
        session.add(
            FilterPreset(
                slug=slug,
                display_name=display_name,
                lut=cube_bytes,
                preview=preview,
                checksum=checksum,
                builtin=builtin,
                enabled=True,
            )
        )
        stats.imported += 1
        log.info("filter_imported", slug=slug, builtin=builtin)
    else:
        existing.display_name = display_name
        existing.lut = cube_bytes
        existing.preview = preview
        existing.checksum = checksum
        stats.updated += 1
        log.info("filter_updated", slug=slug)


async def import_filters(session: AsyncSession, luts_dir: Path) -> FilterImportStats:
    """内置预设 + 目录下全部 .cube，一条管线。重复执行幂等。"""
    stats = FilterImportStats()

    # 内置预设：由变换函数生成 LUT，与导入模版走完全相同的存储与渲染路径
    from jobs.luts import to_cube_bytes

    for slug, display_name, lut in builtin_presets():
        await _upsert(
            session,
            stats,
            slug=slug,
            display_name=display_name,
            lut=lut,
            cube_bytes=to_cube_bytes(lut, display_name),
            builtin=True,
        )

    if await asyncio.to_thread(luts_dir.is_dir):
        cube_files = sorted(await asyncio.to_thread(lambda: list(luts_dir.glob("*.cube"))))
        for path in cube_files:
            payload = await asyncio.to_thread(path.read_bytes)
            try:
                lut = parse_cube(payload)
            except LutError as exc:
                stats.rejected += 1
                stats.errors.append(f"{path.name}: {exc}")
                log.warning("filter_rejected", file=path.name, reason=str(exc))
                continue
            await _upsert(
                session,
                stats,
                slug=_slug_from_filename(path),
                display_name=path.stem.strip(),
                lut=lut,
                cube_bytes=payload,
                builtin=False,
            )
    else:
        log.info("luts_dir_missing", dir=str(luts_dir), detail="只导入内置预设")

    return stats


async def disable_filter(session: AsyncSession, slug: str) -> bool:
    """软下架。返回是否真的有这一行。"""
    result = await session.execute(
        update(FilterPreset).where(FilterPreset.slug == slug).values(enabled=False)
    )
    return bool(_cast("CursorResult[Any]", result).rowcount)
