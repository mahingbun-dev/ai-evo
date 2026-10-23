"""M7 QE7-5 前端仿真视图契约测试

仓库 W12 起的前端契约测试模式（无组件级测试运行器，以源码关键结构断言
钉住功能不回退，同 test_safety_frontend_view.py）：

- Simulation.vue：场景构建器（persona 卡片五轴编辑/组合模式/trials/max_turns/
  软超时/环境初始文件/终态校验器三类型编辑/故障注入勾选与注入回合）、
  发布门禁前端提示（至少一个校验器）、运行列表（状态/进度/pass 卡片）、
  逐 trial 对话回放（双气泡 + 工具调用轨道 + 故障标签）、失败 trial
  一键转数据集（question=对话回放 + 元数据平铺）；
- 路由 /simulation 与侧边栏菜单项；
- api/simulation.js：CRUD + run + getRun + stopRun 与后端端点对齐。
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


class TestSimulationViewBuilder:
    """场景构建器"""

    def test_persona_card_axes(self):
        content = _read("views/Simulation.vue")
        # 五轴：角色/目标/专业度/耐心/对抗性
        for binding in (
            'v-model="persona.role"', 'v-model="persona.goal"',
            'v-model="persona.expertise"', 'v-model="persona.patience"',
            'v-model="persona.adversarial"', 'v-model="persona.style_hint"',
        ):
            assert binding in content, f"persona 编辑项缺失: {binding}"
        # 三档专业度按钮与耐心/对抗性边界（与后端 QE7-1 归一化一致）
        assert '"novice"' in content and '"intermediate"' in content and '"expert"' in content
        assert ":min=\"1\"" in content and ":max=\"15\"" in content  # 耐心
        assert ":step=\"0.1\"" in content  # 对抗性 0-1

    def test_combo_mode_and_trial_params(self):
        content = _read("views/Simulation.vue")
        assert 'v-model="editor.combo_mode"' in content
        for mode in ("single", "blend", "rotate"):
            assert f'value="{mode}"' in content
        assert 'v-model="editor.trials"' in content and ":max=\"5\"" in content  # pass^k 的 k
        assert 'v-model="editor.max_turns"' in content
        assert 'v-model="editor.trial_soft_timeout_seconds"' in content

    def test_agent_config(self):
        content = _read("views/Simulation.vue")
        assert 'v-model="editor.agent.exec_type"' in content
        assert "claude_agent" in content and "deepseek_agent" in content
        assert 'v-model="editor.agent.instruction"' in content

    def test_env_files_staging(self):
        content = _read("views/Simulation.vue")
        assert "editor.envFiles" in content
        # 组装为 config.env.files（后端 staging 契约）；编辑态从 config.env?.files 回填
        assert "config.env = { files: envFiles }" in content
        assert "config.env?.files" in content

    def test_verifier_editor_covers_m2_types(self):
        content = _read("views/Simulation.vue")
        for v_type in ("db_assert", "file_assert", "http_assert"):
            assert v_type in content, f"校验器类型缺失: {v_type}"
        # 发布门禁：前端提交前校验至少一个校验器（与后端 400 门禁同口径）
        assert "至少配置一个终态校验器" in content

    def test_fault_injection_checkbox_and_turns(self):
        content = _read("views/Simulation.vue")
        assert 'v-model="editor.faultTypes"' in content
        for fault in ("tool_timeout", "tool_error", "dirty_data", "contradiction", "context_overflow"):
            assert fault in content, f"故障类型缺失: {fault}"
        assert 'v-model="editor.faultTurns"' in content  # 注入回合（确定性）
        assert 'v-model="editor.faultPadChars"' in content


class TestSimulationRunView:
    """运行列表与逐 trial 回放"""

    def test_run_list_columns(self):
        content = _read("views/Simulation.vue")
        for fragment in ("pass@1", "pass^k", "trials_done", "运行ID"):
            assert fragment in content, f"运行列表列缺失: {fragment}"

    def test_metric_cards(self):
        content = _read("views/Simulation.vue")
        for metric in (
            "task_success_rate", "pass_pow_k", "average_turns",
            "tool_waste_rate", "fault_recovery_rate",
        ):
            assert metric in content, f"指标卡缺失: {metric}"

    def test_dialog_replay_bubbles_and_tool_track(self):
        content = _read("views/Simulation.vue")
        # 双气泡时间线（用户左 / Agent 右）与工具调用轨道
        assert 'turn.role === \'user\'' in content
        assert "user-bubble" in content and "agent-bubble" in content
        assert "tool-track" in content and "tool-chip" in content
        # 回放元信息：终止原因标签 + 故障注入标签
        assert "terminationLabel" in content and "faultLabel" in content

    def test_verification_results_table(self):
        content = _read("views/Simulation.vue")
        assert "trial.verification" in content
        assert "verification.results" in content  # 逐校验器条目表

    def test_failed_trial_to_dataset(self):
        content = _read("views/Simulation.vue")
        assert "convertTrialToDataset" in content
        assert "转数据集" in content
        # 转数据集载荷：question=对话回放 + 终态期望与元数据平铺（回归资产）
        assert "question: dialogText" in content
        assert "ref_answer" in content
        assert "termination_reason" in content and "simulation_run_id" in content
        assert "/import/json" in content


class TestSimulationRoutingAndApi:
    """路由/侧边栏/API 封装"""

    def test_route_registered(self):
        router = _read("router/index.js")
        assert "path: '/simulation'" in router
        assert "views/Simulation.vue" in router

    def test_sidebar_entry(self):
        app = _read("App.vue")
        assert 'index="/simulation"' in app
        assert "仿真评测" in app

    def test_api_client_covers_endpoints(self):
        api = _read("api/simulation.js")
        for endpoint in (
            "get('/simulation/scenarios'", "post('/simulation/scenarios'",
            "put(`/simulation/scenarios/${id}`", "delete(`/simulation/scenarios/${id}`",
            "post(`/simulation/scenarios/${id}/run`)",
            "get(`/simulation/scenarios/${id}/runs`",
            "get(`/simulation/runs/${runId}`)",
            "post(`/simulation/runs/${runId}/stop`)",
        ):
            assert endpoint in api, f"API 封装缺失: {endpoint}"
