"""跨 harness 可移植性检查（Codex/Cursor/OpenCode）。

移植自 plugin-eval src/plugin_eval/layers/harness_portability.py（skill 部分）。
每条 finding 携带 remediation 修复建议（harness-engineering 模式）。
"""

from __future__ import annotations

import re

# Codex 对 skill 正文的硬上限
_CODEX_SKILL_BYTE_CAP = 8 * 1024

_CLAUDE_TOOLS = (
    "Read",
    "Edit",
    "Write",
    "Bash",
    "Grep",
    "Glob",
    "Agent",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "LS",
    "LSP",
)
_TOOL_ALTERNATION = "|".join(_CLAUDE_TOOLS)

# 仅在 Claude 工具语境下匹配反引号工具名（"the `Bash`" / "`Task` tool" /
# "use `Bash`" 等），排除 API 文档里 `Task` 类型之类的泛指反引号。
_CAMEL_TOOL_PATTERN = re.compile(
    rf"(?:\b(?:the|use|using|call|calling|invoke|invoking|via)\s+)`({_TOOL_ALTERNATION})`"
    rf"|`({_TOOL_ALTERNATION})`\s+tool\b",
    re.IGNORECASE,
)

# "use the Read tool" 类散文措辞；工具名必须精确 CamelCase，避免
# "the bash tool"（指 shell）之类误报。
_TOOL_PROSE_PATTERN = re.compile(rf"(?i:\bthe)\s+(?:`)?({_TOOL_ALTERNATION})(?:`)?\s+tool\b")


def detect_skill_findings(skill) -> list:
    """单个 skill 的全部可移植性 findings，每条为 dict（flag/severity/description/remediation）。"""
    findings = []

    # 1. skill 正文超过 Codex 8KB 硬上限
    body_bytes = len(skill.raw_content.encode("utf-8"))
    if body_bytes > _CODEX_SKILL_BYTE_CAP and not skill.has_references:
        findings.append(
            {
                "flag": "SKILL_OVER_CODEX_CAP",
                "severity": 0.15,
                "description": (
                    f"Skill body is {body_bytes} bytes; Codex hard-truncates skills at "
                    f"{_CODEX_SKILL_BYTE_CAP} bytes. No `references/` directory present."
                ),
                "remediation": (
                    "Move detail sections into `references/details.md` (or similar) and "
                    "leave the SKILL.md body as a navigation summary. Codex will load the "
                    "references on demand."
                ),
            }
        )

    # 2. 正文用反引号引用 CamelCase Claude 工具名（"`Read`"、"`Bash`"）
    raw_matches = _CAMEL_TOOL_PATTERN.findall(skill.raw_content)
    tool_hits = [m[0] or m[1] for m in raw_matches if m[0] or m[1]]
    if tool_hits:
        unique = sorted(set(tool_hits))
        findings.append(
            {
                "flag": "CLAUDE_TOOL_REFS",
                "severity": min(0.10, 0.02 * len(unique)),
                "description": (
                    f"Skill body references Claude Code tools by CamelCase name: {unique}. "
                    "OpenCode requires lowercase (`read`, `bash`); Codex prefers action verbs."
                ),
                "remediation": (
                    "Use action verbs (e.g. 'open the file', 'run the shell command') or "
                    "lowercase the tool reference. The adapter rewrites a conservative set, "
                    "but explicit phrasing is more portable."
                ),
            }
        )

    # 3. "use the Read tool" 类散文
    prose_hits = _TOOL_PROSE_PATTERN.findall(skill.raw_content)
    if prose_hits:
        unique = sorted(set(prose_hits))
        findings.append(
            {
                "flag": "CLAUDE_TOOL_PROSE",
                "severity": 0.05,
                "description": (
                    f"Skill body uses prose like 'use the X tool' for: {unique}. Codex "
                    "doesn't name tools to the model; the model picks them by action."
                ),
                "remediation": (
                    'Rewrite "the Read tool" as "open the file", "the Bash tool" as '
                    '"run the shell command", etc. Talk about the action, not the tool.'
                ),
            }
        )

    return findings


def score_skill_portability(skill) -> float:
    """skill 的 0.0-1.0 可移植性子分。"""
    findings = detect_skill_findings(skill)
    if not findings:
        return 1.0
    penalty = sum(f["severity"] for f in findings)
    return max(0.0, 1.0 - penalty)
