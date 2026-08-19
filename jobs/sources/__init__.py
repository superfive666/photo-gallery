"""源站 adapter 的工厂。pipeline / media_ingest / worker 都从这里拿实现。"""

from __future__ import annotations

from gallery_core.config import get_settings
from jobs.sources.base import SourceAdapter


def build_adapter() -> SourceAdapter:
    """按 SOURCE_ADAPTER 选择源站实现。"""
    s = get_settings()
    if s.source_adapter == "local_dir":
        from jobs.sources.local_dir import LocalDirAdapter

        return LocalDirAdapter(s.source_local_dir)

    from jobs.sources.static_gallery import StaticGalleryAdapter

    return StaticGalleryAdapter(
        base_url=s.source_base_url,
        user_agent=s.source_user_agent,
        concurrency=s.source_concurrency,
        rate_limit_per_second=s.source_rate_limit_per_second,
    )
