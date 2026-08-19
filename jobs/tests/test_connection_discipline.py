"""连接纪律的守卫：长任务函数不得持有数据库 session 跨过重活。

事故背景：建库曾在一个 session（一条连接）里跑完全程，下载视频的几个小时里
连接空闲，被宿主机 Postgres/内核掐掉，写库时爆 "connection is closed"。
修复后的契约是：下载/拆条/向量化/ffmpeg 期间不持连接，写库用函数内部的短事务。

这两个函数的签名一旦重新出现 session 参数，说明有人把长事务模式加回来了 ——
这个测试就是那时的警报。
"""

from __future__ import annotations

import inspect

from jobs.media_ingest import ingest_album_media
from jobs.render import render_project


def test_ingest_owns_its_sessions() -> None:
    params = inspect.signature(ingest_album_media).parameters
    assert "session" not in params, (
        "ingest_album_media 不得接受外部 session：下载以小时计，攥着一条连接"
        "跨过它会被当空闲掐掉。写库请用函数内部的 session_scope 短事务。"
    )


def test_render_owns_its_sessions() -> None:
    params = inspect.signature(render_project).parameters
    assert "session" not in params, (
        "render_project 不得接受外部 session：ffmpeg 转码以分钟计，"
        "读取在 _load_plan 短事务里材料化，结果在结尾短事务里写回。"
    )
