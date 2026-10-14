"""二期前端（预置脚本产物渲染 / 脚本完整管理界面 / MCP工具化开关）缺陷修复 —— 前端逻辑单测（pytest 入口）

真实断言逻辑在 frontend/tests/unit/ 下的 node:test 文件（提取 Results.vue /
Models.vue / api/index.js 真实函数源码，喂以后端真实挂载形态的数据）。
本文件把前端逻辑测试挂进项目 pytest 套件统一执行：
    pytest tests/unit/test_results_frontend_prescript.py -v

覆盖缺陷：
- 结果观察区对真实后端数据永不渲染、log_grep/xml_diff/db_assert 专属渲染失效、
  观察条目映射缺失、统计 chips key 重复（resultsPreScript.test.mjs）
- D1 导入脚本前后端契约不匹配（multipart vs JSON）、D2 导入摘要字段不对齐、
  D3 前端管理员门控、D4 删除软删除文案、D5 缺文件脚本（modelsPreScriptAdmin.test.mjs）
- CC 配置卡缺失「脚本 MCP 工具」开关（enable_script_mcp，默认关）、UI 保存
  静默关闭该开关（claudeCodeMcpSwitch.test.mjs）
"""
import shutil
import subprocess
from pathlib import Path

import pytest

# W8 分类：unit（无网络/无 DB）；本文件是 frontend/tests/unit/*.test.mjs 的
# pytest 桥接入口，依赖宿主机 node 运行时——离线 CI runner 无 node 时整套
# 明确 skip（本文件 test_node_available 已声明的意图），绝不假绿也绝不误报。
pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("node") is None, reason="未找到 node，跳过前端逻辑单测（CI 离线 runner 无 node）"),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NODE_TEST_FILES = [
    PROJECT_ROOT / "frontend" / "tests" / "unit" / "resultsPreScript.test.mjs",
    PROJECT_ROOT / "frontend" / "tests" / "unit" / "modelsPreScriptAdmin.test.mjs",
    PROJECT_ROOT / "frontend" / "tests" / "unit" / "claudeCodeMcpSwitch.test.mjs",
]


def _node_bin() -> str | None:
    return shutil.which("node")


def _run_node_test(test_file: Path) -> str:
    """运行单个 node:test 文件并断言全过，返回 TAP stdout 供断言统计

    --test-reporter=tap 钉死输出格式：Node 默认报告器随版本/TTY 变化
    （Node ≥22 非 TTY 也输出 spec 的 ℹ 汇总行，Node 20 输出 TAP），直接解析
    默认输出会跨平台误报。
    """
    node = _node_bin()
    assert node is not None, "未找到 node，无法运行前端逻辑单测"
    assert test_file.is_file(), f"测试文件缺失: {test_file}"
    proc = subprocess.run(
        [node, "--test", "--test-reporter=tap", str(test_file)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"前端逻辑单测失败（{test_file.name}，exit={proc.returncode}）\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc.stdout


@pytest.mark.p0
class TestFrontendLogicNodeSuite:
    def test_node_available(self):
        """环境具备 node 运行时（无 node 时后续用例 skip 而非误报）"""
        if _node_bin() is None:
            pytest.skip("未找到 node，跳过前端逻辑单测")

    @pytest.mark.parametrize(
        "test_file",
        NODE_TEST_FILES,
        ids=[p.stem for p in NODE_TEST_FILES],
    )
    def test_frontend_logic_node_suite(self, test_file):
        """运行各 node:test 前端逻辑测试文件的全量断言"""
        if _node_bin() is None:
            pytest.skip("未找到 node，跳过前端逻辑单测")
        stdout = _run_node_test(test_file)
        # TAP 计划行校验：确有用例执行（防止测试文件为空导致假通过）；
        # 全部通过由 _run_node_test 的 returncode == 0 保证
        plan_lines = [ln for ln in stdout.splitlines() if ln.startswith("1..")]
        assert plan_lines, f"未解析到 TAP 计划行：\n{stdout}"
        assert plan_lines[0] != "1..0", f"测试文件没有执行任何用例：\n{stdout}"
