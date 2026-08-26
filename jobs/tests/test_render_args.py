from __future__ import annotations

from jobs.render import build_ffmpeg_args, output_stem, project_dirname, slugify


def test_build_ffmpeg_args_frame_accurate_trim() -> None:
    args = build_ffmpeg_args(
        "/m/a.mp4", "/o/01.mp4", 1500, 4500, lut_path=None, crf=16, preset="medium"
    )
    # -ss/-to 在 -i 之前：快速 seek + 重编码 = 帧精确
    assert args.index("-ss") < args.index("-i")
    assert args[args.index("-ss") + 1] == "1.500"
    assert args[args.index("-to") + 1] == "4.500"
    assert args[args.index("-crf") + 1] == "16"
    # 不缩放：参数里不允许出现 scale
    assert not any("scale" in a for a in args)
    assert "-vf" not in args


def test_build_ffmpeg_args_with_lut() -> None:
    args = build_ffmpeg_args(
        "/m/a.mp4", "/o/01.mp4", 0, 1000, lut_path="/data/luts/x.cube", crf=16, preset="slow"
    )
    vf = args[args.index("-vf") + 1]
    assert "lut3d" in vf
    assert "/data/luts/x.cube" in vf


def test_slugify_keeps_chinese_strips_symbols() -> None:
    assert slugify("毕业视频 v2!!") == "毕业视频-v2"
    assert slugify("") == "untitled"
    assert len(slugify("很长" * 100)) <= 40


def test_output_stem_backup_sorts_next_to_primary() -> None:
    primary = output_stem(3, "切蛋糕的瞬间", "primary")
    backup = output_stem(3, "切蛋糕的瞬间", "backup")
    assert primary == "03_切蛋糕的瞬间"
    assert backup == f"{primary}_alt"
    # 备选文件必须紧跟主选排序（后期软件里按文件名浏览时两条相邻）
    assert sorted([backup, primary, output_stem(4, "x", "primary")])[:2] == [primary, backup]


def test_project_dirname_unique_and_readable() -> None:
    import uuid

    pid = uuid.UUID("01890000-0000-7000-8000-000000000001")
    name = project_dirname(pid, "毕业视频")
    assert name.startswith("01890000-")
    assert "毕业视频" in name
