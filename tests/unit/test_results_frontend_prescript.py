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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NODE_TEST_FILES = [
    PROJECT_ROOT / "frontend" / "tests" / "unit" / "resultsPreScript.test.mjs",
    PROJECT_ROOT / "frontend" / "tests" / "unit" / "modelsPreScriptAdmin.test.mjs",
    PROJECT_ROOT / "frontend" / "tests" / "unit" / "claudeCodeMcpSwitch.test.mjs",
]


def _node_bin() -> str | None:
    return shutil.which("node")


def _run_node_test(test_file: Path) -> str:
    """运行单个 node:test 文件并断言全过，返回 stdout 供断言统计"""
    node = _node_bin()
    assert node is not None, "未找到 node，无法运行前端逻辑单测"
    assert test_file.is_file(), f"测试文件缺失: {test_file}"
    proc = subprocess.run(
        [node, "--test", str(test_file)],
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
        # 汇总行校验：确有用例执行且全部通过（防止测试文件为空导致假通过）
        summary = [ln for ln in stdout.splitlines() if ln.startswith("ℹ")]
        assert any("tests" in ln for ln in summary), f"未解析到测试统计：\n{stdout}"
        assert "fail 0" in stdout, f"存在失败用例：\n{stdout}"
