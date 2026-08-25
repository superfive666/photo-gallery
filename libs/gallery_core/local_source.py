"""本地相册素材的 URL↔路径换算。**单一定义点**。

`local://album/<相对路径>` 是本地素材在库里的 photo_url 形态，与远端的
`https://...` 在语义上对齐（都是幂等键、都能反解出字节来源）。换算规则只在
这里定义一次 —— jobs 建库、评估集、api 原图分发三处共用。两处各写一遍的话，
评估会把每张照片判成 not_ingested、api 会 404，且都表现为「检索坏了」，极难误诊。

放在 gallery_core 而不是 jobs/sources/ 里，是因为 api 也要做反解
（本地原图分发），而 api 不 import jobs 包。
"""

from __future__ import annotations

from pathlib import Path

LOCAL_SCHEME = "local://album"
_PREFIX = f"{LOCAL_SCHEME}/"


def is_local_url(url: str) -> bool:
    """photo_url / source_url 是否指向本地相册素材。"""
    return url.startswith(_PREFIX)


def photo_url_for(relative_path: str) -> str:
    """相对相册根目录的路径（形如 `2026-08-10/IMG_0001.jpg`）→ photo_url。"""
    return f"{_PREFIX}{relative_path.lstrip('/')}"


def resolve_local_path(root: str | Path, photo_url: str) -> Path | None:
    """local:// URL → 相册根目录下的绝对路径；解不出安全路径就返回 None。

    photo_url 正常只来自建库流程，但这里仍做防御纵深：`..`、绝对路径、
    符号链接逃逸等任何解析后落在 root 之外的情况一律拒绝 ——
    api 拿 None 统一回 404，不区分「不存在」与「越界」。
    """
    if not is_local_url(photo_url):
        return None
    relative = photo_url[len(_PREFIX) :]
    if not relative:
        return None
    root_resolved = Path(root).resolve()
    try:
        candidate = (root_resolved / relative).resolve()
    except OSError:
        return None
    if candidate == root_resolved or not candidate.is_relative_to(root_resolved):
        return None
    return candidate
