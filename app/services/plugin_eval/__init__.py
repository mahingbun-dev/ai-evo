"""插件评测核心引擎（移植自 agents/plugins/plugin-eval 的三层评测思路）

- parser.py：解析 Claude Code 插件结构（SKILL.md / agents / plugin.json）
- static_layer.py：Layer 1 静态 lint（纯 Python、确定性、无模型调用）
- portability.py：跨 harness 可移植性检查（Codex/Cursor/OpenCode）
- engine.py：维度权重 × 层混合 × 反模式惩罚 → 复合分（0-100）与徽章

judge 层（Layer 2）不在此包内：统一由 app/services/plugin_evaluator.py
经平台模型调用器（ModelCallerSync）实现，与 ai-evo 的裁判模型体系一致。
"""
