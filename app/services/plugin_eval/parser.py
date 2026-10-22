"""解析 Claude Code 插件结构：skills、agents、plugin.json。

移植自 plugin-eval src/plugin_eval/parser.py，改为纯 dataclass +
可选的内联 SKILL.md 内容解析（evaluate_content 场景，无文件系统）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_CROSS_REFERENCE_PATTERN = re.compile(
    r"(?<![\w-])((?:skill|skills|sub-skills)/[a-z0-9-]+(?:/[a-z0-9-]+)*)"
)
_CODE_FENCE_PATTERN = re.compile(r"^\s*(?:>\s*)*(`{3,}|~{3,})(\w*)(.*)")


@dataclass
class ParsedSkill:
    path: Path
    name: str
    description: str
    line_count: int
    h2_count: int
    h3_count: int
    code_block_count: int
    code_block_languages: list
    has_examples: bool
    has_troubleshooting: bool
    has_references: bool
    has_assets: bool
    reference_files: list
    asset_files: list
    total_content_lines: int
    must_never_always_count: int
    cross_references: list
    raw_content: str
    frontmatter: dict


@dataclass
class ParsedAgent:
    path: Path
    name: str
    description: str
    model: str | None
    has_tools_restriction: bool
    tools: list
    has_proactive_trigger: bool
    skill_references: list
    raw_content: str
    frontmatter: dict


@dataclass
class ParsedPlugin:
    path: Path
    name: str
    skills: list = field(default_factory=list)
    agents: list = field(default_factory=list)
    plugin_json: dict = field(default_factory=dict)


def parse_skill(skill_dir: Path) -> ParsedSkill:
    """解析单个 skill 目录。"""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"No SKILL.md found in {skill_dir}")

    content = skill_md.read_text(encoding="utf-8")
    return parse_skill_content(content, name=skill_dir.name, path=skill_dir)


def parse_skill_content(content: str, name: str = "", path: Path | None = None) -> ParsedSkill:
    """解析内联 SKILL.md 文本（无文件系统依赖；references/assets 视为不存在）。"""
    skill_dir = path or Path(name or "inline-skill")
    frontmatter, body = _split_frontmatter(content)
    lines = body.strip().split("\n")

    h2_count = sum(1 for line in lines if re.match(r"^## ", line))
    h3_count = sum(1 for line in lines if re.match(r"^### ", line))

    code_block_count, code_block_languages = _count_code_blocks(content)

    lower_body = body.lower()
    has_examples = bool(re.search(r"(## example|### example|## usage)", lower_body))
    has_troubleshooting = bool(re.search(r"(## troubleshoot|## common issue|## faq)", lower_body))

    total_lines = len(content.split("\n"))

    must_pattern = re.compile(r"\b(MUST|NEVER|ALWAYS)\b")
    must_count = len(must_pattern.findall(content))

    cross_refs = _extract_cross_references(body)

    return ParsedSkill(
        path=skill_dir,
        name=frontmatter.get("name", name or skill_dir.name),
        description=frontmatter.get("description", ""),
        line_count=total_lines,
        h2_count=h2_count,
        h3_count=h3_count,
        code_block_count=code_block_count,
        code_block_languages=code_block_languages,
        has_examples=has_examples,
        has_troubleshooting=has_troubleshooting,
        has_references=False,
        has_assets=False,
        reference_files=[],
        asset_files=[],
        total_content_lines=total_lines,
        must_never_always_count=must_count,
        cross_references=cross_refs,
        raw_content=content,
        frontmatter=frontmatter,
    )


def parse_agent(agent_path: Path) -> ParsedAgent:
    """解析 agent markdown 文件。"""
    content = agent_path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(content)

    tools_raw = frontmatter.get("tools", "")
    if isinstance(tools_raw, list):
        tools = [str(t).strip() for t in tools_raw]
    elif isinstance(tools_raw, str) and tools_raw:
        tools = [t.strip() for t in tools_raw.split(",")]
    else:
        tools = []

    description = frontmatter.get("description", "")
    has_proactive = bool(re.search(r"use proactively", description, re.IGNORECASE))

    skill_refs = _extract_cross_references(body)

    return ParsedAgent(
        path=agent_path,
        name=frontmatter.get("name", agent_path.stem),
        description=description,
        model=frontmatter.get("model"),
        has_tools_restriction=bool(tools),
        tools=tools,
        has_proactive_trigger=has_proactive,
        skill_references=skill_refs,
        raw_content=content,
        frontmatter=frontmatter,
    )


def parse_plugin(plugin_dir: Path) -> ParsedPlugin:
    """解析整个插件目录（.claude-plugin/plugin.json + skills/ + agents/）。"""
    plugin_json_path = plugin_dir / ".claude-plugin" / "plugin.json"
    plugin_json = {}
    if plugin_json_path.exists():
        plugin_json = json.loads(plugin_json_path.read_text(encoding="utf-8"))

    name = plugin_json.get("name", plugin_dir.name)

    skills = []
    skills_dir = plugin_dir / "skills"
    if skills_dir.exists():
        for skill_dir in sorted(skills_dir.iterdir()):
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                skills.append(parse_skill(skill_dir))

    agents = []
    agents_dir = plugin_dir / "agents"
    if agents_dir.exists():
        for agent_file in sorted(agents_dir.glob("*.md")):
            agents.append(parse_agent(agent_file))

    return ParsedPlugin(
        path=plugin_dir,
        name=name,
        skills=skills,
        agents=agents,
        plugin_json=plugin_json,
    )


def detect_target(target_dir: Path) -> str:
    """判断目录类型：'skill'（含 SKILL.md）/'plugin'（含 .claude-plugin/）/unknown。"""
    if (target_dir / "SKILL.md").exists():
        return "skill"
    if (target_dir / ".claude-plugin").exists():
        return "plugin"
    return "unknown"


def _count_code_blocks(content: str) -> tuple:
    """统计围栏代码块数量并收集起始围栏的语言标注。

    围栏成对出现，只数开启侧；反引号/波浪线围栏只能被同字符且不短于开启
    长度、无语言标注、无尾随文本的围栏关闭（CommonMark 规则）。
    """
    count = 0
    languages = []
    open_fence = ""

    for line in content.split("\n"):
        match = _CODE_FENCE_PATTERN.match(line)
        if match is None:
            continue
        fence, language, trailing = match.group(1), match.group(2), match.group(3)
        if open_fence:
            if (
                fence[0] == open_fence[0]
                and len(fence) >= len(open_fence)
                and not language
                and not trailing.strip()
            ):
                open_fence = ""
            continue
        if fence[0] == "`" and "`" in trailing:
            # CommonMark：反引号不能出现在反引号围栏的 info string 中
            continue
        open_fence = fence
        count += 1
        if language:
            languages.append(language)

    return count, languages


def _split_frontmatter(content: str) -> tuple:
    """把 YAML frontmatter 从 markdown 正文中拆出。"""
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    try:
        frontmatter = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        frontmatter = {}

    return frontmatter, parts[2]


def _extract_cross_references(body: str) -> list:
    """提取 skill/agent 交叉引用，保留嵌套 skill 路径。"""
    references = []
    for reference in _CROSS_REFERENCE_PATTERN.findall(body):
        if reference.startswith(("skill/", "skills/")):
            reference = reference.split("/", 1)[1]
        references.append(reference)
    return references
