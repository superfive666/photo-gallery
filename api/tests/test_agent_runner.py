"""agent 执行器的纯逻辑：JSON 抽取、模板渲染、无 LLM 回退路径。

LLM 未配置时系统必须照常可用（CI 也靠这条路径）——回退解析是被测的正式功能，
不是权宜之计。
"""

from __future__ import annotations

import pytest

from api.app.agent.runner import (
    extract_json,
    parse_script_fallback,
    refine_fallback,
    render,
)


def test_render_survives_braces_in_script() -> None:
    # 剧本原文里出现 {} 不能把模板渲染炸掉（这就是不用 str.format 的原因）
    out = render("剧本：{{script}}", script="镜头{特写}：他笑了 {x}")
    assert out == "剧本：镜头{特写}：他笑了 {x}"


def test_extract_json_plain() -> None:
    assert extract_json('{"a": 1}') == '{"a": 1}'


def test_extract_json_with_fences_and_noise() -> None:
    text = '好的，这是结果：\n```json\n{"a": {"b": 2}}\n```\n希望有帮助！'
    assert extract_json(text) == '{"a": {"b": 2}}'


def test_extract_json_nested_without_fences() -> None:
    text = '前言 {"shots": [{"idx": 1}]} 后记'
    assert extract_json(text) == '{"shots": [{"idx": 1}]}'


def test_extract_json_missing_raises() -> None:
    with pytest.raises(ValueError, match="没有 JSON"):
        extract_json("抱歉，我不能这样做")


def test_fallback_numbered_script() -> None:
    script = "毕业视频\n1. 校门口全景，清晨\n2. 教室里同学们大笑\n3. 操场合影"
    result = parse_script_fallback(script)
    assert result.title == "毕业视频"
    assert [s.idx for s in result.shots] == [1, 2, 3]
    assert result.shots[0].description.startswith("校门口全景")
    assert result.shots[0].queries


def test_fallback_paragraph_script() -> None:
    script = "开场：大家陆续到场\n\n中段：切蛋糕\n\n结尾：全体合影"
    result = parse_script_fallback(script)
    assert len(result.shots) == 3


def test_fallback_single_blob() -> None:
    result = parse_script_fallback("就一句话的剧本")
    assert len(result.shots) == 1


def test_refine_fallback_mixes_feedback_into_queries() -> None:
    result = refine_fallback([(2, "教室大笑", "要有阳光的感觉")])
    assert result.shots[0].idx == 2
    assert "阳光" in result.shots[0].queries[0]
