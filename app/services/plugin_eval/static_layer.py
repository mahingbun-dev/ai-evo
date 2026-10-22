"""Layer 1：静态 lint——纯 Python、无模型调用、确定性强、<2 秒。

移植自 plugin-eval src/plugin_eval/layers/static.py，anti_patterns 与
LayerResult 改为纯 dict 输出（便于直接落 JSON 列）。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.plugin_eval.parser import ParsedSkill, parse_plugin, parse_skill
from app.services.plugin_eval.portability import score_skill_portability

# skill 子分权重（plugin-eval 原版：为 harness_portability 腾出 ~6% 后的重平衡值）
_SKILL_WEIGHTS = {
    "frontmatter_quality": 0.32,
    "orchestration_wiring": 0.23,
    "progressive_disclosure": 0.14,
    "structural_completeness": 0.10,
    "token_efficiency": 0.09,
    "ecosystem_coherence": 0.06,
    "harness_portability": 0.06,
}

# OVER_CONSTRAINED 的 MUST/NEVER/ALWAYS 阈值
_OVER_CONSTRAINED_THRESHOLD = 15

# BLOATED_SKILL 的行数阈值（无 references/ 目录时）
_BLOATED_LINE_THRESHOLD = 800

# 规范触发措辞（祈使第二人称 / 第三人称规范式 / 介词时间式 / 自动加载自述 /
# 显式标记），与 MISSING_TRIGGER、_description_pushiness 共用本模式。
_TRIGGER_PATTERN = re.compile(
    r"\b(?:should\s+be\s+)?used?\s+(?:this\s+skill\s+)?(?:immediately\s+)?"
    r"(?:when|after|before|whenever)\b"
    r"|\buse\s+proactively\b"
    r"|\btrigger(?:s)?\s+(?:when|on)\b"
    r"|\bauto[-\s]?loads?\s+(?:when|on)\b",
    re.IGNORECASE,
)


def anti_pattern_penalty(count: int) -> float:
    """按反模式数量返回 [0.5, 1.0] 乘法惩罚因子。"""
    return max(0.5, 1.0 - 0.05 * count)


def _skill_uses_description_trigger(skill: ParsedSkill) -> bool:
    """skill 是否依赖 description 被模型自动调用。

    `disable-model-invocation: true`（仅斜杠调用）或 `paths:` frontmatter
    （按路径自动加载）走不同触发机制，不参与缺失触发短语检查。
    """
    fm = skill.frontmatter
    if fm.get("disable-model-invocation") is True:
        return False
    paths = fm.get("paths")
    return not (isinstance(paths, str) and paths != "")


class StaticAnalyzer:
    """确定性的插件/skill 结构质量分析。"""

    def analyze_skill(self, skill_or_dir) -> dict:
        """分析单个 skill 目录（或已解析的 ParsedSkill），返回 LayerResult dict。"""
        skill = skill_or_dir if isinstance(skill_or_dir, ParsedSkill) else parse_skill(skill_or_dir)
        anti_patterns = self._detect_skill_anti_patterns(skill)

        sub_scores = {
            "frontmatter_quality": self._score_frontmatter(skill),
            "orchestration_wiring": self._score_orchestration(skill),
            "progressive_disclosure": self._score_progressive_disclosure(skill),
            "structural_completeness": self._score_structural_completeness(skill),
            "token_efficiency": self._score_token_efficiency(skill),
            "ecosystem_coherence": self._score_ecosystem_coherence(skill),
            "harness_portability": score_skill_portability(skill),
        }
        # 可移植性 findings 已体现在上面的子分中，不再重复计入 anti_patterns
        # （避免同一缺陷被扣两次：子分损失 + 乘法反模式惩罚）。

        raw_score = sum(sub_scores[name] * weight for name, weight in _SKILL_WEIGHTS.items())
        penalty = anti_pattern_penalty(len(anti_patterns))
        final_score = max(0.0, min(1.0, raw_score * penalty))

        return {
            "layer": "static",
            "score": final_score,
            "sub_scores": sub_scores,
            "anti_patterns": anti_patterns,
            "metadata": {"skill_name": skill.name, "line_count": skill.line_count},
        }

    def analyze_plugin(self, plugin_dir) -> dict:
        """分析整个插件目录（skills + agents 均分），返回 LayerResult dict。"""
        plugin = parse_plugin(plugin_dir)

        skill_scores = []
        all_anti_patterns = []

        for skill in plugin.skills:
            result = self.analyze_skill(skill)
            skill_scores.append(result["score"])
            all_anti_patterns.extend(result["anti_patterns"])

        agent_scores = []
        for agent in plugin.agents:
            agent_scores.append(self._score_agent(agent))

        all_scores = skill_scores + agent_scores
        final_score = (sum(all_scores) / len(all_scores)) if all_scores else 0.5

        return {
            "layer": "static",
            "score": max(0.0, min(1.0, final_score)),
            "sub_scores": {
                "skill_scores": skill_scores,
                "agent_scores": agent_scores,
            },
            "anti_patterns": all_anti_patterns,
            "metadata": {
                "plugin_name": plugin.name,
                "skill_count": len(plugin.skills),
                "agent_count": len(plugin.agents),
            },
        }

    # ------------------------------------------------------------------
    # 反模式检测
    # ------------------------------------------------------------------

    def _detect_skill_anti_patterns(self, skill: ParsedSkill) -> list:
        patterns = []

        # OVER_CONSTRAINED：MUST/ALWAYS/NEVER 过多
        if skill.must_never_always_count > _OVER_CONSTRAINED_THRESHOLD:
            patterns.append(
                self._anti_pattern(
                    flag="OVER_CONSTRAINED",
                    description=(
                        f"Skill contains {skill.must_never_always_count} MUST/ALWAYS/NEVER "
                        f"directives (threshold: {_OVER_CONSTRAINED_THRESHOLD}). "
                        "Overly prescriptive instructions reduce model flexibility."
                    ),
                    severity=0.10,
                )
            )

        # EMPTY_DESCRIPTION：缺失或过短描述
        if len(skill.description.strip()) < 20:
            patterns.append(
                self._anti_pattern(
                    flag="EMPTY_DESCRIPTION",
                    description=(
                        f"Description is too short ({len(skill.description.strip())} chars). "
                        "A good description should be at least 20 characters."
                    ),
                    severity=0.10,
                )
            )

        # MISSING_TRIGGER：description 缺少规范触发短语（仅适用于依赖 description 触发的 skill）
        if _skill_uses_description_trigger(skill) and not _TRIGGER_PATTERN.search(
            skill.description
        ):
            patterns.append(
                self._anti_pattern(
                    flag="MISSING_TRIGGER",
                    description=(
                        "Skill description lacks a recognised trigger phrase "
                        '(e.g. "Use when …", "This skill should be used when …", '
                        '"Use after …", "Auto-loads when …"). '
                        "Without one, the model cannot determine when to invoke the skill."
                    ),
                    severity=0.15,
                )
            )

        # BLOATED_SKILL：>800 行且无 references/ 目录
        if skill.line_count > _BLOATED_LINE_THRESHOLD and not skill.has_references:
            patterns.append(
                self._anti_pattern(
                    flag="BLOATED_SKILL",
                    description=(
                        f"Skill has {skill.line_count} lines but no references/ directory. "
                        "Large skills should offload supporting material to references/."
                    ),
                    severity=0.10,
                )
            )

        # ORPHAN_REFERENCE：SKILL.md 链接到不存在的 references 文件
        if skill.has_references:
            content = skill.raw_content
            linked_refs = re.findall(r"\(references/([^)]+)\)", content)
            existing = {f.lower() for f in skill.reference_files}
            for linked in linked_refs:
                if Path(linked).name.lower() not in existing:
                    patterns.append(
                        self._anti_pattern(
                            flag="ORPHAN_REFERENCE",
                            description=(
                                f"SKILL.md links to 'references/{linked}' which does not exist. "
                                "Dead reference links waste context and confuse the model."
                            ),
                            severity=0.05,
                        )
                    )

        # DEAD_CROSS_REF：交叉引用无法解析
        skill_parent = skill.path.parent  # skills/ 目录
        for ref in skill.cross_references:
            ref_path = skill_parent / ref
            if not ref_path.exists() and ref.startswith("sub-skills/"):
                ref_path = skill.path / ref
            if not ref_path.exists():
                patterns.append(
                    self._anti_pattern(
                        flag="DEAD_CROSS_REF",
                        description=(
                            f"Cross-reference to skill/agent '{ref}' cannot be resolved. "
                            "Dead links degrade ecosystem coherence."
                        ),
                        severity=0.05,
                    )
                )

        return patterns

    @staticmethod
    def _anti_pattern(flag: str, description: str, severity: float) -> dict:
        return {"flag": flag, "description": description, "severity": severity}

    # ------------------------------------------------------------------
    # skill 子分
    # ------------------------------------------------------------------

    def _score_frontmatter(self, skill: ParsedSkill) -> float:
        """frontmatter 质量：name、描述长度、触发短语、指令推进度。"""
        score = 0.0

        # name 存在（0.15）
        if skill.name and skill.name != skill.path.name:
            score += 0.15
        elif skill.name:
            score += 0.10

        # 描述长度（0.25）
        desc_len = len(skill.description.strip())
        if desc_len >= 100:
            score += 0.25
        elif desc_len >= 60:
            score += 0.20
        elif desc_len >= 30:
            score += 0.15
        elif desc_len >= 10:
            score += 0.05

        # 触发短语质量——主要区分项（0.60）
        pushiness = self._description_pushiness(skill.description)
        score += pushiness * 0.60

        return min(1.0, score)

    def _score_orchestration(self, skill: ParsedSkill) -> float:
        """编排布线：输出格式、输入/接收模式的文档化程度。"""
        # 多数 skill 默认是 worker——0.70 良好基线
        score = 0.70

        body_lower = skill.raw_content.lower()

        # 输出格式文档（+0.10）
        if re.search(r"\boutput\b.*\bformat\b|\bformat\b.*\boutput\b|\breturn.*json\b", body_lower):
            score += 0.10
        # 有 return/output 语言（+0.05）
        if re.search(r"\breturns?\b|\bproduces?\b|\boutputs?\b|\bgenerates?\b", body_lower):
            score += 0.05

        # 输入/接口文档（+0.10）
        if re.search(r"\binput\b|\breceives?\b|\baccepts?\b|\bparameters?\b|\bargs?\b", body_lower):
            score += 0.10

        # 代码示例展示具体 worker 行为（+0.05）
        if skill.code_block_count >= 2:
            score += 0.05

        # 自称 orchestrator 则扣分
        if re.search(r"\borchestrat\w*\b|\bcoordinat\w*\b|\bdispatch\w*\b", body_lower):
            score -= 0.15

        return max(0.0, min(1.0, score))

    def _score_progressive_disclosure(self, skill: ParsedSkill) -> float:
        """渐进式披露：行数与最佳区间、references/assets。"""
        score = 0.0
        lines = skill.line_count

        if 200 <= lines <= 600:
            score += 0.60  # 理想体量——refs 是加分项而非必需
        elif 100 <= lines < 200:
            score += 0.50  # 简洁但可能过短
        elif 600 < lines <= 800:
            score += 0.40  # 偏长——refs 会更有帮助
        elif lines < 100:
            score += 0.20  # stub
        else:
            # >800 行——这个体量必须有 refs
            score += 0.15

        # references 目录加分（大 skill 加分更多）
        if skill.has_references and skill.reference_files:
            score += 0.25 if lines > 400 else 0.15

        # assets 目录加分
        if skill.has_assets and skill.asset_files:
            score += 0.15

        return min(1.0, score)

    def _score_structural_completeness(self, skill: ParsedSkill) -> float:
        """结构完整性与技术深度。"""
        score = 0.0

        # 标题——结构
        heading_count = skill.h2_count + skill.h3_count
        if heading_count >= 6:
            score += 0.20
        elif heading_count >= 4:
            score += 0.15
        elif heading_count >= 2:
            score += 0.10

        # 代码块——可操作性
        if skill.code_block_count >= 5:
            score += 0.20
        elif skill.code_block_count >= 3:
            score += 0.15
        elif skill.code_block_count >= 1:
            score += 0.10

        # 多语言深度——专家受众信号
        unique_langs = set(skill.code_block_languages)
        if len(unique_langs) >= 3:
            score += 0.15
        elif len(unique_langs) >= 2:
            score += 0.10

        # 示例章节
        if skill.has_examples:
            score += 0.15

        # 排错章节
        if skill.has_troubleshooting:
            score += 0.15

        # 技术深度信号：表格、决策/对比章节
        body_lower = skill.raw_content.lower()
        if re.search(r"\|.*\|.*\|", skill.raw_content):  # markdown 表格
            score += 0.10
        if re.search(r"(decision|when to use|comparison|tradeoff)", body_lower):
            score += 0.05

        return min(1.0, score)

    def _score_token_efficiency(self, skill: ParsedSkill) -> float:
        """token 效率：MUST/NEVER/ALWAYS 密度、重复行检测。"""
        score = 1.0
        lines = max(skill.line_count, 1)

        # MUST/NEVER/ALWAYS 密度——超过每 10 行 1 个开始扣分
        density = skill.must_never_always_count / lines
        if density > 0.5:
            score -= 0.4
        elif density > 0.2:
            score -= 0.2
        elif density > 0.1:
            score -= 0.1

        # 重复检测：非空行中重复行的比例
        body_lines = [
            ln.strip()
            for ln in skill.raw_content.splitlines()
            if ln.strip() and not ln.startswith("---")
        ]
        unique = len(set(body_lines))
        total = len(body_lines)
        if total > 0:
            repetition_ratio = 1.0 - (unique / total)
            score -= repetition_ratio * 0.4

        return max(0.0, min(1.0, score))

    def _score_ecosystem_coherence(self, skill: ParsedSkill) -> float:
        """生态一致性：交叉引用、related/see also 提及。"""
        score = 0.50  # 基线——多数 skill 独立运作，这没问题

        if skill.cross_references:
            score += min(0.25, len(skill.cross_references) * 0.08)

        body_lower = skill.raw_content.lower()
        if re.search(r"\brelated\b|\bsee also\b|\bcompanion\b|\bcomplement", body_lower):
            score += 0.25

        return min(1.0, score)

    # ------------------------------------------------------------------
    # agent 打分
    # ------------------------------------------------------------------

    def _score_agent(self, agent) -> float:
        """agent 的简单质量分。"""
        score = 0.5  # 基线

        if agent.has_proactive_trigger:
            score += 0.2

        if agent.has_tools_restriction and len(agent.tools) > 0:
            score += 0.1

        if len(agent.description.strip()) >= 40:
            score += 0.1
        elif len(agent.description.strip()) >= 20:
            score += 0.05

        if agent.model:
            score += 0.1

        return min(1.0, score)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _description_pushiness(self, description: str) -> float:
        """描述对自主调用的引导程度（0-1）。"""
        score = 0.0
        desc_lower = description.lower()

        # 规范触发措辞（0.25）
        if _TRIGGER_PATTERN.search(description):
            score += 0.25

        # "Use PROACTIVELY" / "proactively"（0.15）
        if re.search(r"\bproactively\b", desc_lower):
            score += 0.15

        # 额外触发关键词（0.10）
        if re.search(r"\bautomatically\b|\balways\s+use\b|\btrigger\b|\binvoke\b", desc_lower):
            score += 0.10

        # 具体性加分：列举多个具体场景（0.20）
        use_cases = len(
            re.findall(
                r",\s*(?:or\s+)?(?:when|for|during|implementing|building|creating|debugging|testing|deploying|configuring|setting up)",
                desc_lower,
            )
        )
        if use_cases >= 3:
            score += 0.20
        elif use_cases >= 1:
            score += 0.10

        # 描述了具体上下文或文件类型（0.15）
        if re.search(r"\b(when\s+\w+ing|for\s+\w+ing|during\s+\w+ing)\b", desc_lower):
            score += 0.15

        # 合理长度（>= 40 字符）（0.10）
        if len(description.strip()) >= 40:
            score += 0.10

        return min(1.0, score)
