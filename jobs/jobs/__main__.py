"""jobs CLI。

python -m jobs migrate
python -m jobs ingest [--album ID] [--full]
python -m jobs cluster
python -m jobs eval [--dir /data/eval] [--sweep]
python -m jobs block --person <uuid> | --photo <uuid> [--reason ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from gallery_core.config import get_settings
from gallery_core.db import get_engine, session_scope
from gallery_core.embedding_client import EmbeddingClient
from gallery_core.logging import configure_logging, get_logger

log = get_logger(__name__)


def build_adapter() -> object:
    """按 SOURCE_ADAPTER 选择源站实现。

    photos.zrc.sg 的接入方式确定之前，用 local_dir 推进全部其他工作。
    见 docs/data-source.md。
    """
    s = get_settings()
    if s.source_adapter == "local_dir":
        from jobs.sources.local_dir import LocalDirAdapter

        return LocalDirAdapter(s.source_local_dir)

    from jobs.sources.static_gallery import StaticGalleryAdapter

    return StaticGalleryAdapter(
        base_url=s.source_base_url,
        token=s.source_token,
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


async def cmd_ingest(args: argparse.Namespace) -> int:
    from jobs.pipeline import ingest

    s = get_settings()
    adapter = build_adapter()
    async with EmbeddingClient() as embedding:
        if not await embedding.healthy():
            log.error("embedding_unavailable", url=s.embedding_service_url)
            print("embedding 服务不可用，中止。", file=sys.stderr)
            return 2

        async with session_scope() as session:
            stats = await ingest(
                session,
                adapter,  # type: ignore[arg-type]
                embedding,
                s,
                album_filter=args.album,
                full=args.full,
            )

    report = stats.as_dict()
    print(json.dumps(report, ensure_ascii=False, indent=2))

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


async def cmd_cluster(_args: argparse.Namespace) -> int:
    from jobs.cluster import recluster

    s = get_settings()
    async with session_scope() as session:
        stats = await recluster(session, s)
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    noise_ratio = stats.get("noise_ratio")
    if isinstance(noise_ratio, float) and noise_ratio > 0.4:
        print(
            f"\n提示：噪声点比例 {noise_ratio:.0%} 偏高。这些人脸不属于任何簇，"
            f"只能靠检索的「直接命中」兜底。考虑调大 CLUSTER_EPS（当前 {s.cluster_eps}）。",
            file=sys.stderr,
        )
    return 0


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
    """opt-out。第一期由管理员用 CLI 处理，不做自助 UI。"""
    from sqlalchemy import text

    if bool(args.person) == bool(args.photo):
        print("必须且只能指定 --person 或 --photo 之一", file=sys.stderr)
        return 2

    scope = "person" if args.person else "photo"
    target = args.person or args.photo
    try:
        uuid.UUID(target)
    except ValueError:
        print(f"不是合法的 uuid: {target}", file=sys.stderr)
        return 2

    async with session_scope() as session:
        await session.execute(
            text(
                """
                INSERT INTO block_list (scope, person_id, photo_id, reason, created_by)
                VALUES (
                    :scope,
                    CASE WHEN :scope = 'person' THEN CAST(:target AS uuid) END,
                    CASE WHEN :scope = 'photo'  THEN CAST(:target AS uuid) END,
                    :reason, :created_by
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "scope": scope,
                "target": target,
                "reason": args.reason,
                "created_by": args.by,
            },
        )
    print(f"已屏蔽 {scope} {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobs", description="离线建库与运维任务")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="执行 docs/schema 下未应用的迁移")

    p_ingest = sub.add_parser("ingest", help="拉取源站相册并建库")
    p_ingest.add_argument("--album", help="只处理指定相册；省略则处理全部")
    p_ingest.add_argument("--full", action="store_true", help="忽略 checksum 缓存，全量重新处理")

    sub.add_parser("cluster", help="全量重跑 person 聚类")

    p_eval = sub.add_parser("eval", help="跑评估集，输出 precision/recall 与漏检归因")
    p_eval.add_argument("--dir", default="/data/eval", help="评估集目录")
    p_eval.add_argument("--sweep", action="store_true", help="在阈值网格上扫描并给出建议阈值")

    p_block = sub.add_parser("block", help="把 person 或 photo 加入屏蔽名单（opt-out）")
    p_block.add_argument("--person")
    p_block.add_argument("--photo")
    p_block.add_argument("--reason", default=None)
    p_block.add_argument("--by", default="cli")

    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)

    handlers = {
        "migrate": cmd_migrate,
        "ingest": cmd_ingest,
        "cluster": cmd_cluster,
        "eval": cmd_eval,
        "block": cmd_block,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())
