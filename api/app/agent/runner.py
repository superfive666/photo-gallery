"""agent 节点执行器：模板渲染 → 调 LLM → JSON 抽取 → pydantic 校验（失败回喂重试一次）。

LLM 未配置时走**无 LLM 回退路径**：按编号/空行把剧本切成镜头、原文即检索 query ——
系统不因为没配模型而不可用，效果打折但流程完整（CI 也靠这条路径测试）。

提示词即配置：prompts/*.md 随代码版本化；可选支持从 {media_root}/agent-prompts/
读同名文件覆盖（不重新部署也能调）。每次调用都记录提示词指纹（content hash），
与 LLM 模型名一起写进 edit_round —— 换模型/改提示词后旧轮次产出仍可归因。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from api.app.agent.client import LlmError, chat, llm_configured
from api.app.agent.schemas import RefinedShot, RefineOut, ShotDraft, ShotListOut
from gallery_core.config import Settings
from gallery_core.logging import get_logger

log = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(settings: Settings, name: str) -> tuple[str, str]:
    """读提示词模板，返回 (内容, 指纹)。挂载卷里的同名文件优先（热调参）。"""
    override = Path(settings.media_root) / "agent-prompts" / name
    path = override if override.is_file() else _PROMPTS_DIR / name
    text = path.read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def render(template: str, **variables: str) -> str:
    """`{{var}}` 占位替换。不用 str.format —— 剧本原文里出现花括号会把它炸掉。"""
    out = template
    for key, value in variables.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def extract_json(text: str) -> str:
    """从模型输出里抠出第一个 JSON 对象（容忍代码围栏与前后废话）。"""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("输出里没有 JSON 对象")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("JSON 对象未闭合")


TModel = TypeVar("TModel", bound=BaseModel)


async def _chat_validated(
    settings: Settings, system: str, user: str, model_cls: type[TModel]
) -> TModel:
    """调 LLM 并按契约校验。第一次校验失败把错误喂回去再试一次。"""
    raw = await chat(settings, system, user)
    try:
        return model_cls.model_validate_json(extract_json(raw))
    except (ValidationError, ValueError) as exc:
        log.warning("llm_output_invalid", error_type=type(exc).__name__)
        retry_user = (
            f"{user}\n\n你上一次的输出无法通过校验：{exc}\n"
            "请重新输出**只含 JSON 对象**的回答，严格符合要求的结构。"
        )
        raw = await chat(settings, system, retry_user)
        return model_cls.model_validate_json(extract_json(raw))


# ---------------------------------------------------------------------------
# 无 LLM 回退路径
# ---------------------------------------------------------------------------

_NUMBERED = re.compile(r"^\s*(?:\d{1,3}[.、)．]|[#*-]|镜头\s*\d+[:：]?)\s*")


def parse_script_fallback(script: str) -> ShotListOut:
    """按编号行/空行段落切镜头。原文即描述与 query —— 效果打折但流程完整。"""
    lines = [line.rstrip() for line in script.strip().splitlines()]
    title = next((line.strip() for line in lines if line.strip()), "未命名剪辑")[:20]

    numbered = [line for line in lines if _NUMBERED.match(line)]
    if len(numbered) >= 2:
        chunks = [_NUMBERED.sub("", line).strip() for line in numbered]
    else:
        chunks = [p.strip().replace("\n", " ") for p in script.strip().split("\n\n") if p.strip()]

    chunks = [c for c in chunks if c][:200] or [script.strip()[:200]]
    shots = [
        ShotDraft(idx=i, source_text=c, description=c[:200], queries=[c[:100]])
        for i, c in enumerate(chunks, start=1)
    ]
    return ShotListOut(title=title, default_filter_slug=None, shots=shots)


def refine_fallback(targets: list[tuple[int, str, str]]) -> RefineOut:
    """(idx, 旧描述, 反馈) → 把反馈拼进 query。没有 LLM 时的最低限度「换血」。"""
    shots = [
        RefinedShot(
            idx=idx,
            description=f"{desc}（{feedback}）"[:200],
            queries=[f"{desc} {feedback}"[:100]],
        )
        for idx, desc, feedback in targets
    ]
    return RefineOut(shots=shots)


# ---------------------------------------------------------------------------
# 两个状态机节点
# ---------------------------------------------------------------------------


async def parse_script(
    settings: Settings, script: str, filter_slugs: list[str]
) -> tuple[ShotListOut, str | None, str | None]:
    """首轮：剧本 → 润色后的镜头清单。返回 (结果, llm_model, 提示词指纹)。"""
    if not llm_configured(settings):
        return parse_script_fallback(script), None, None

    system, sys_fp = load_prompt(settings, "system.md")
    template, tpl_fp = load_prompt(settings, "polish.md")
    user = render(template, script=script, filters=", ".join(filter_slugs) or "（空）")
    try:
        result = await _chat_validated(settings, system, user, ShotListOut)
    except (LlmError, ValidationError, ValueError):
        # LLM 坏了不挡路：回退解析，剧本还能走完流程，评审页可人工修
        log.exception("parse_script_llm_failed_fallback")
        return parse_script_fallback(script), None, None
    return result, settings.llm_model, f"{sys_fp}:{tpl_fp}"


async def refine_shots(
    settings: Settings,
    script: str,
    history: str,
    notes: str,
    targets: list[tuple[int, str, str]],
) -> tuple[RefineOut, str | None, str | None]:
    """反馈闭环：只重写不满意的镜头。targets = [(idx, 旧描述, 反馈原文)]。"""
    if not llm_configured(settings):
        return refine_fallback(targets), None, None

    system, sys_fp = load_prompt(settings, "system.md")
    template, tpl_fp = load_prompt(settings, "refine.md")
    targets_text = "\n".join(
        f"- 镜头 {idx}：上轮描述「{desc}」；用户反馈「{feedback}」"
        for idx, desc, feedback in targets
    )
    user = render(
        template,
        script=script,
        history=history or "（首轮，无历史）",
        notes=notes or "（无）",
        targets=targets_text,
    )
    try:
        result = await _chat_validated(settings, system, user, RefineOut)
    except (LlmError, ValidationError, ValueError):
        log.exception("refine_llm_failed_fallback")
        return refine_fallback(targets), None, None
    return result, settings.llm_model, f"{sys_fp}:{tpl_fp}"


def dumps_shot_list(result: ShotListOut) -> list[dict[str, object]]:
    """edit_round.shot_list 快照的序列化形态。"""
    return [json.loads(s.model_dump_json()) for s in result.shots]
