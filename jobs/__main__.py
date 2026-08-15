"""jobs CLI。

python -m jobs migrate
python -m jobs probe  [--album 2026-08-10]        # 探查源站页面结构
python -m jobs ingest [--album ID] [--full]
python -m jobs eval   [--dir /data/eval] [--sweep]
python -m jobs block  --selfie <路径> | --face <uuid> | --photo <uuid> [--reason ...]
python -m jobs invite create --album 2026-08-10 [--label "发给谁"]   # 发一张绑定相册的码
python -m jobs invite list
python -m jobs invite disable --prefix <8位hex>
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
from jobs.sources.base import SourceAdapter

log = get_logger(__name__)


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
        full_code, prefix, code_hash = generate_invite_code()
        async with session_scope() as session:
            session.add(
                InviteCode(
                    prefix=prefix,
                    code_hash=code_hash,
                    album=args.album,
                    label=args.label,
                )
            )
        print(
            json.dumps(
                {
                    "invite_code": full_code,
                    "prefix": prefix,
                    "album": args.album,
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
    invite_sub.add_parser("list", help="列出全部邀请码（只有 prefix，没有完整码）")
    p_inv_disable = invite_sub.add_parser("disable", help="吊销一张码")
    p_inv_disable.add_argument("--prefix", required=True, help="要吊销的码的 prefix（8 位 hex）")

    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)

    handlers = {
        "migrate": cmd_migrate,
        "probe": cmd_probe,
        "ingest": cmd_ingest,
        "eval": cmd_eval,
        "block": cmd_block,
        "invite": cmd_invite,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())
