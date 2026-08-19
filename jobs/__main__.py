"""jobs CLI。

python -m jobs migrate
python -m jobs probe  [--album 2026-08-10]        # 探查源站页面结构
python -m jobs ingest [--album ID] [--full]
python -m jobs eval   [--dir /data/eval] [--sweep]
python -m jobs block  --selfie <路径> | --face <uuid> | --photo <uuid> [--reason ...]
python -m jobs invite create --album 2026-08-10 [--label "发给谁"]   # 发一张绑定相册的码
python -m jobs invite list
python -m jobs invite disable --prefix <8位hex>
python -m jobs face-thumbs [--album ID]        # 回填存量人脸小图（003 之前的数据）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from gallery_core.config import get_settings
from gallery_core.db import get_engine, session_scope
from gallery_core.embedding_client import EmbeddingClient, EmbeddingServiceError
from gallery_core.logging import configure_logging, get_logger
from jobs.sources import build_adapter

log = get_logger(__name__)


async def cmd_migrate(_args: argparse.Namespace) -> int:
    from jobs.migrate import run

    s = get_settings()
    executed = await run(get_engine(), Path(s.schema_dir))
    print(json.dumps({"applied": executed}, ensure_ascii=False))
    return 0


async def cmd_probe(args: argparse.Namespace) -> int:
    """对着真站跑一次解析，打印看到了什么。不写数据库。

    photos.zrc.sg 的相册页标记结构尚未确认，`static_gallery.py` 里是通用解析。
    用这个命令确认解析结果是否正确，再把解析收敛成精确的选择器。
    """
    adapter = build_adapter()
    report: dict[str, Any] = {"adapter": get_settings().source_adapter}

    try:
        albums = await adapter.list_albums()
        report["albums_discovered"] = len(albums)
        report["albums_sample"] = albums[:10]
    except Exception as exc:
        report["albums_error"] = f"{type(exc).__name__}: {exc}"
        albums = []

    target = args.album or (albums[0] if albums else None)
    if target is None:
        report["hint"] = "没发现相册且未指定 --album，无法探查资产。请用 --album 指定一个 slug。"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    report["probed_album"] = target
    try:
        assets = [a async for a in adapter.list_assets(target)]
    except Exception as exc:
        report["assets_error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    images = [a for a in assets if a.kind == "image"]
    videos = [a for a in assets if a.kind == "video"]
    report["assets_total"] = len(assets)
    report["images"] = len(images)
    report["videos"] = len(videos)
    report["with_source_thumbnail"] = sum(1 for a in images if a.thumbnail_url)
    report["assets_sample"] = [
        {"filename": a.filename, "photo_url": a.photo_url, "thumbnail_url": a.thumbnail_url}
        for a in assets[:5]
    ]

    if not assets:
        report["hint"] = (
            "解析到 0 个资产 —— 通用 HTML 解析没匹配上这个站点的结构。"
            "把相册页的 HTML 片段贴出来，就能把 _parse_album_page 收敛成精确选择器。"
        )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if hasattr(adapter, "aclose"):
        await adapter.aclose()
    return 0 if assets else 1


async def cmd_ingest(args: argparse.Namespace) -> int:
    from jobs.pipeline import ingest

    s = get_settings()
    adapter = build_adapter()
    async with EmbeddingClient() as embedding:
        try:
            health: dict[str, Any] | None = await embedding.health()
        except EmbeddingServiceError as exc:
            log.warning("embedding_health_failed", error_type=type(exc).__name__)
            health = None
        if not health or not health.get("model_loaded"):
            log.error("embedding_unavailable", url=s.embedding_service_url)
            print("embedding 服务不可用，中止。", file=sys.stderr)
            return 2

        if not health.get("batch_supported"):
            print(
                "提示：embedding 服务报告识别模型不支持可变 batch，批量会退化成逐张前向。"
                "GPU 利用率上不去，值得排查模型导出方式。",
                file=sys.stderr,
            )

        async with session_scope() as session:
            stats = await ingest(
                session,
                adapter,
                embedding,
                s,
                album_filter=args.album,
                full=args.full,
            )

    if hasattr(adapter, "aclose"):
        await adapter.aclose()

    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))

    # 丢弃的人脸数偏高时主动提示 —— 这是「后排的人搜不到」的根因，
    # 遇到它该调 MIN_FACE_PX / det_size，而不是调相似度阈值。
    total_faces = stats.faces_added + stats.faces_discarded
    if total_faces and stats.faces_discarded / total_faces > 0.3:
        print(
            f"\n提示：{stats.faces_discarded}/{total_faces} 张人脸因质量门控被丢弃"
            f"（MIN_FACE_PX={s.min_face_px}）。若召回率不足，先看 docs/evaluation.md"
            " 的「漏检归因」，不要直接调阈值。",
            file=sys.stderr,
        )
    return 1 if stats.failed else 0


async def cmd_eval(args: argparse.Namespace) -> int:
    """跑评估集，输出 precision/recall 与漏检归因。见 docs/evaluation.md。"""
    from jobs.eval import evaluate, sweep

    s = get_settings()
    eval_dir = Path(args.dir)
    if not await asyncio.to_thread(eval_dir.is_dir):
        print(f"找不到评估集目录 {eval_dir}（用 --dir 指定，或设 EVAL_DIR）", file=sys.stderr)
        return 2

    async with EmbeddingClient() as embedding:
        if not await embedding.healthy():
            print("embedding 服务不可用，中止。", file=sys.stderr)
            return 2

        async with session_scope() as session:
            if args.sweep:
                report: dict[str, object] = await sweep(session, embedding, s, eval_dir)
            else:
                _, report = await evaluate(session, embedding, s, eval_dir)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        "\n把这份结果（指标 + 阈值 + 模型版本 + 日期）追加到 docs/evaluation-history.md，"
        "该文件只增不改。",
        file=sys.stderr,
    )
    return 0


async def cmd_block(args: argparse.Namespace) -> int:
    """opt-out。第一期由管理员用 CLI 处理，不做自助 UI。

    没有 person 表，所以「屏蔽某个人」= 屏蔽他的那一批 face。
    `--selfie` 就是为此存在的：用当事人的自拍跑一次检索，把命中的 face 全部屏蔽。
    这条命令是幂等的，可以在新照片入库后重复运行。
    """
    from sqlalchemy import text

    from api.app.services.search import find_faces_for_blocking

    given = [bool(args.selfie), bool(args.face), bool(args.photo)]
    if sum(given) != 1:
        print("必须且只能指定 --selfie / --face / --photo 之一", file=sys.stderr)
        return 2

    settings = get_settings()

    if args.selfie:
        selfie_path = Path(args.selfie)
        if not await asyncio.to_thread(selfie_path.is_file):
            print(f"找不到文件 {selfie_path}", file=sys.stderr)
            return 2

        async with EmbeddingClient() as embedding:
            if not await embedding.healthy():
                print("embedding 服务不可用，中止。", file=sys.stderr)
                return 2
            # 与线上检索完全一致：只取最明显的一张脸
            result = await embedding.extract(
                await asyncio.to_thread(selfie_path.read_bytes),
                filename=selfie_path.name,
                primary_only=True,
            )

        if not result.faces:
            print("这张自拍里没检测到人脸，换一张正面清晰的。", file=sys.stderr)
            return 1

        async with session_scope() as session:
            face_ids = await find_faces_for_blocking(
                session, result.faces[0].embedding, settings, threshold=args.threshold
            )
            if not face_ids:
                print(json.dumps({"blocked_faces": 0}, ensure_ascii=False))
                return 0
            await session.execute(
                text(
                    """
                    INSERT INTO block_list (scope, face_id, reason, created_by)
                    SELECT 'face', CAST(fid AS uuid), :reason, :created_by
                    FROM unnest(CAST(:ids AS text[])) AS fid
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "ids": [str(fid) for fid in face_ids],
                    "reason": args.reason or "opt-out via selfie",
                    "created_by": args.by,
                },
            )
        print(
            json.dumps(
                {
                    "blocked_faces": len(face_ids),
                    "threshold": args.threshold or settings.face_match_threshold,
                },
                ensure_ascii=False,
            )
        )
        print(
            "\n提示：这条命令只屏蔽了当前库里的人脸。之后有新相册入库时需要再跑一次。",
            file=sys.stderr,
        )
        return 0

    scope = "face" if args.face else "photo"
    target = args.face or args.photo
    try:
        uuid.UUID(target)
    except ValueError:
        print(f"不是合法的 uuid: {target}", file=sys.stderr)
        return 2

    async with session_scope() as session:
        await session.execute(
            text(
                """
                INSERT INTO block_list (scope, face_id, photo_id, reason, created_by)
                VALUES (
                    :scope,
                    CASE WHEN :scope = 'face'  THEN CAST(:target AS uuid) END,
                    CASE WHEN :scope = 'photo' THEN CAST(:target AS uuid) END,
                    :reason, :created_by
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {"scope": scope, "target": target, "reason": args.reason, "created_by": args.by},
        )
    print(f"已屏蔽 {scope} {target}")
    return 0


async def cmd_invite(args: argparse.Namespace) -> int:
    """邀请码运维：发码 / 列码 / 吊销。

    完整码只在 create 的输出里出现一次 —— 库里只有 prefix + argon2(secret)，
    丢了就吊销重发，没有「找回」。
    """
    from sqlalchemy import select

    from api.app.auth import generate_invite_code
    from gallery_core.models import InviteCode

    if args.invite_command == "create":
        if args.role == "edit" and not args.album:
            # 一码一相册是剪辑域的硬语义：拿到码 = 拿到用这个相册剪辑的权限
            print("剪辑码必须绑定相册：--role edit 时 --album 必填", file=sys.stderr)
            return 2
        full_code, prefix, code_hash = generate_invite_code()
        async with session_scope() as session:
            invite = InviteCode(
                prefix=prefix,
                code_hash=code_hash,
                album=args.album,
                role=args.role,
                label=args.label,
            )
            session.add(invite)
            await session.flush()
            workspace_id = str(invite.id)
        print(
            json.dumps(
                {
                    "invite_code": full_code,
                    "prefix": prefix,
                    "album": args.album,
                    "role": args.role,
                    # 剪辑码的工作区 id：项目/成片都挂在它下面，排查数据归属用
                    "workspace_id": workspace_id if args.role == "edit" else None,
                    "label": args.label,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(
            "\n⚠️ 完整邀请码只显示这一次，请立即发给对方。库里只存 hash，无法找回。",
            file=sys.stderr,
        )
        if args.album is None:
            print("⚠️ 未指定 --album：这是一张全相册的管理码。", file=sys.stderr)
        return 0

    if args.invite_command == "list":
        async with session_scope() as session:
            rows = (
                (await session.execute(select(InviteCode).order_by(InviteCode.created_at)))
                .scalars()
                .all()
            )
        print(
            json.dumps(
                [
                    {
                        "prefix": r.prefix,
                        "album": r.album,
                        "role": r.role,
                        "label": r.label,
                        "disabled": r.disabled_at is not None,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in rows
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    # disable
    import datetime as dt

    async with session_scope() as session:
        row = (
            await session.execute(select(InviteCode).where(InviteCode.prefix == args.prefix))
        ).scalar_one_or_none()
        if row is None:
            print(f"没有 prefix 为 {args.prefix} 的邀请码", file=sys.stderr)
            return 2
        if row.disabled_at is not None:
            print(f"{args.prefix} 已经是吊销状态（{row.disabled_at.isoformat()}）")
            return 0
        row.disabled_at = dt.datetime.now(tz=dt.UTC)
    print(f"已吊销 {args.prefix}（已登录的 session 会随 JWT 过期自然失效）")
    return 0


async def cmd_face_thumbs(args: argparse.Namespace) -> int:
    """回填存量人脸小图（003 迁移之前入库的脸）。

    bbox 已经在库里，所以只需要重新下载原图裁剪 —— **不经过 embedding 服务、
    不动 GPU**。幂等：只处理 thumb IS NULL 的脸，可以随时中断重跑。
    """
    from sqlalchemy import text

    from jobs.sources.base import SourceAsset
    from jobs.thumbnails import crop_face

    adapter = build_adapter()
    done_faces = 0
    failed_photos = 0

    async with session_scope() as session:
        photos = (
            await session.execute(
                text(
                    """
                    SELECT ph.id, ph.album, ph.photo_url
                    FROM photo ph
                    WHERE ph.deleted_at IS NULL
                      AND ph.kind = 'image'
                      AND (CAST(:album AS text) IS NULL OR ph.album = :album)
                      AND EXISTS (
                          SELECT 1 FROM face f
                          WHERE f.photo_id = ph.id AND f.thumb IS NULL
                      )
                    ORDER BY ph.id
                    """
                ),
                {"album": args.album},
            )
        ).all()

    print(f"待回填照片：{len(photos)} 张", file=sys.stderr)

    for photo in photos:
        asset = SourceAsset(
            album=photo.album,
            filename=photo.photo_url.rsplit("/", 1)[-1],
            photo_url=photo.photo_url,
        )
        try:
            chunks = [chunk async for chunk in adapter.open_asset(asset)]
            payload = b"".join(chunks)
        except Exception as exc:
            failed_photos += 1
            log.warning("face_thumb_download_failed", error_type=type(exc).__name__)
            continue

        # 每张照片独立事务：中断/失败不影响已完成的部分
        async with session_scope() as session:
            faces = (
                await session.execute(
                    text(
                        """
                        SELECT id, bbox_x, bbox_y, bbox_w, bbox_h
                        FROM face
                        WHERE photo_id = :pid AND thumb IS NULL
                        """
                    ),
                    {"pid": str(photo.id)},
                )
            ).all()
            for face in faces:
                try:
                    thumb = crop_face(payload, (face.bbox_x, face.bbox_y, face.bbox_w, face.bbox_h))
                except Exception as exc:
                    log.warning("face_thumb_crop_failed", error_type=type(exc).__name__)
                    continue
                await session.execute(
                    text("UPDATE face SET thumb = :thumb WHERE id = :id"),
                    {"thumb": thumb, "id": str(face.id)},
                )
                done_faces += 1
        del payload

    if hasattr(adapter, "aclose"):
        await adapter.aclose()

    print(json.dumps({"faces_backfilled": done_faces, "photos_failed": failed_photos}))
    return 1 if failed_photos else 0


async def cmd_filters_import(args: argparse.Namespace) -> int:
    """滤镜库导入：内置预设 + 目录下的 .cube 模版。幂等，可反复执行。"""
    from jobs.filters import disable_filter, import_filters

    s = get_settings()
    if args.disable:
        async with session_scope() as session:
            found = await disable_filter(session, args.disable)
        print(json.dumps({"disabled": args.disable, "found": found}, ensure_ascii=False))
        return 0 if found else 1

    luts_dir = Path(args.dir or s.luts_dir())
    async with session_scope() as session:
        stats = await import_filters(session, luts_dir)
    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
    return 1 if stats.rejected and not (stats.imported or stats.updated) else 0


async def cmd_media_ingest(args: argparse.Namespace) -> int:
    """剪辑素材建库（手动预热）。视频多的相册建议提前跑，用户体验就是秒开。"""
    from gallery_core.clip_client import ClipClient
    from jobs.media_ingest import ingest_album_media

    s = get_settings()
    adapter = None if args.local_only else build_adapter()
    try:
        # 不在这里开 session：建库的长活（下载/拆条）以小时计，攥着连接会被掐掉。
        # 写库的短事务由 ingest_album_media 内部按素材粒度自行管理。
        async with ClipClient() as clip:
            stats = await ingest_album_media(s, clip, args.album, adapter)
    finally:
        if adapter is not None and hasattr(adapter, "aclose"):
            await adapter.aclose()
    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
    return 1 if stats.failed else 0


async def cmd_worker(_args: argparse.Namespace) -> int:
    """常驻 worker：认领并执行剪辑域任务（建库/解析检索/渲染/滤镜导入）。"""
    from jobs.worker import run_forever

    await run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobs", description="离线建库与运维任务")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="执行 docs/schema 下未应用的迁移")

    p_probe = sub.add_parser("probe", help="探查源站页面结构，不写数据库")
    p_probe.add_argument("--album", help="要探查的 album slug，例如 2026-08-10")

    p_ingest = sub.add_parser("ingest", help="拉取源站相册并建库（批量）")
    p_ingest.add_argument("--album", help="只处理指定相册；省略则处理全部")
    p_ingest.add_argument("--full", action="store_true", help="忽略已入库记录，全部重新处理")

    p_eval = sub.add_parser("eval", help="跑评估集，输出 precision/recall 与漏检归因")
    p_eval.add_argument("--dir", default="/data/eval", help="评估集目录")
    p_eval.add_argument("--sweep", action="store_true", help="在阈值网格上扫描并给出建议阈值")

    p_block = sub.add_parser("block", help="把人脸或照片加入屏蔽名单（opt-out）")
    p_block.add_argument("--selfie", help="当事人的自拍路径：检索并屏蔽全部命中的人脸（推荐用法）")
    p_block.add_argument("--face", help="直接屏蔽单个 face id")
    p_block.add_argument("--photo", help="屏蔽整张照片")
    p_block.add_argument(
        "--threshold", type=float, default=None, help="--selfie 的匹配阈值，默认用检索阈值"
    )
    p_block.add_argument("--reason", default=None)
    p_block.add_argument("--by", default="cli")

    p_invite = sub.add_parser("invite", help="邀请码运维：发码 / 列码 / 吊销")
    invite_sub = p_invite.add_subparsers(dest="invite_command", required=True)
    p_inv_create = invite_sub.add_parser("create", help="发一张新码（完整码只显示一次）")
    p_inv_create.add_argument("--album", default=None, help="绑定的相册 slug；省略 = 全相册管理码")
    p_inv_create.add_argument("--label", default=None, help="备注：发给谁 / 用途")
    p_inv_create.add_argument(
        "--role",
        choices=["search", "edit"],
        default="search",
        help="search=查照片（默认）；edit=剪辑聊天窗（必须绑相册，一码一相册）",
    )
    invite_sub.add_parser("list", help="列出全部邀请码（只有 prefix，没有完整码）")
    p_inv_disable = invite_sub.add_parser("disable", help="吊销一张码")
    p_inv_disable.add_argument("--prefix", required=True, help="要吊销的码的 prefix（8 位 hex）")

    p_ft = sub.add_parser("face-thumbs", help="回填存量人脸小图（只下载原图裁剪，不动 GPU）")
    p_ft.add_argument("--album", default=None, help="只处理指定相册；省略则处理全部")

    p_filters = sub.add_parser("filters-import", help="导入滤镜库（内置预设 + LUT 目录）")
    p_filters.add_argument("--dir", default=None, help="LUT 目录，默认 {MEDIA_ROOT}/luts")
    p_filters.add_argument("--disable", default=None, help="软下架指定 slug（不删行）")

    p_media = sub.add_parser("media-ingest", help="剪辑素材建库（下载原片 + 拆条 + 向量化）")
    p_media.add_argument("--album", required=True, help="相册 slug")
    p_media.add_argument(
        "--local-only", action="store_true", help="不访问源站，只处理已在 media 目录里的文件"
    )

    sub.add_parser("worker", help="常驻 worker：认领并执行剪辑域任务")

    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)

    handlers = {
        "migrate": cmd_migrate,
        "probe": cmd_probe,
        "ingest": cmd_ingest,
        "eval": cmd_eval,
        "block": cmd_block,
        "invite": cmd_invite,
        "face-thumbs": cmd_face_thumbs,
        "filters-import": cmd_filters_import,
        "media-ingest": cmd_media_ingest,
        "worker": cmd_worker,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())
