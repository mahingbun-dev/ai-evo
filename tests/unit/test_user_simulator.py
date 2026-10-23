"""用户模拟器单测（M7 QE7-1）

- persona 引擎：专业度/耐心/对抗性归一化与越界收敛、组合模式校验；
- 提示词构建：persona 规格/对话回放/预算/协议进 prompt（纯函数确定性）；
- 协议解析：完整 JSON/代码块/缺省字段/非法输入；
- next_message：mock 裁判 LLM（仓库既有手法：patch JudgeClient.compress），
  rotate 多 persona 轮换、blend 目标写全量键；
- 终止条件：goal_achieved / max_turns / give_up（主动+耐心失控保护）；
- 裁判通道异常/连续解析失败 → UserSimulatorError（不伪装放弃/达成）。
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services.user_simulator import (
    COMBO_MODES,
    DEFAULT_MAX_TURNS,
    MAX_PERSONAS,
    SimulatedPersona,
    SimulationDialogState,
    UserSimulator,
    UserSimulatorError,
    build_simulator_prompts,
    check_termination,
    parse_simulator_reply,
    validate_persona_configs,
)

pytestmark = pytest.mark.unit


def _personas(n=1):
    return [
        {
            "name": f"用户{chr(ord('A') + i)}",
            "role": f"角色{chr(ord('A') + i)}",
            "goal": f"完成目标{chr(ord('A') + i)}",
            "expertise": "novice",
            "patience": 3,
            "adversarial": 0.0,
        }
        for i in range(n)
    ]


def _reply_json(message="好的", goal="pending", progress=True, action="speak"):
    return (
        '{"message": "%s", "goal_status": "%s", "progress": %s, "action": "%s"}'
        % (message, goal, "true" if progress else "false", action)
    )


class TestSimulatedPersona:
    def test_expertise_aliases_and_numeric(self):
        assert SimulatedPersona.from_config({"goal": "g", "expertise": "novice"}).expertise == "novice"
        assert SimulatedPersona.from_config({"goal": "g", "expertise": "专家"}).expertise == "expert"
        assert SimulatedPersona.from_config({"goal": "g", "expertise": 0.9}).expertise == "expert"
        assert SimulatedPersona.from_config({"goal": "g", "expertise": 0.5}).expertise == "intermediate"
        assert SimulatedPersona.from_config({"goal": "g", "expertise": 0.1}).expertise == "novice"
        # 非法值回落 intermediate，缺省名/角色有默认
        p = SimulatedPersona.from_config({"goal": "g", "expertise": "???"})
        assert p.expertise == "intermediate"
        assert p.name == "用户1"
        assert p.role == "普通终端用户"

    def test_patience_and_adversarial_clamped(self):
        p = SimulatedPersona.from_config({"goal": "g", "patience": 99, "adversarial": 7})
        assert p.patience == 15
        assert p.adversarial == 1.0
        p2 = SimulatedPersona.from_config({"goal": "g", "patience": "bad", "adversarial": "x"})
        assert p2.patience == 5
        assert p2.adversarial == 0.0

    def test_describe_covers_all_axes(self):
        p = SimulatedPersona.from_config(
            {"name": "小王", "goal": "报销", "role": "财务新人", "style_hint": "口语化"}
        )
        text = p.describe()
        for fragment in ("小王", "财务新人", "报销", "专业度", "耐心", "对抗性", "口语化"):
            assert fragment in text


class TestValidatePersonaConfigs:
    def test_ok_single_and_rotate(self):
        personas, err = validate_persona_configs(_personas(2), "rotate")
        assert err == "" and len(personas) == 2
        personas, err = validate_persona_configs(_personas(1))
        assert err == "" and len(personas) == 1

    def test_single_mode_takes_first(self):
        personas, err = validate_persona_configs(_personas(3), "single")
        assert err == ""
        assert [p.name for p in personas] == ["用户A"]

    def test_errors(self):
        assert validate_persona_configs([], "")[1] != ""
        assert validate_persona_configs(_personas(MAX_PERSONAS + 1), "rotate")[1] != ""
        assert validate_persona_configs([{"role": "r"}], "")[1] != ""  # 缺 goal
        assert validate_persona_configs(_personas(1), "party")[1] != ""  # 非法组合模式
        assert validate_persona_configs(["not-dict"], "")[1] != ""


class TestBuildSimulatorPrompts:
    def test_prompt_contains_persona_and_protocol(self):
        personas, _ = validate_persona_configs(
            [{"name": "小王", "goal": "拿到受理编号", "expertise": "expert", "adversarial": 0.8, "patience": 2}]
        )
        state = SimulationDialogState(goal_status={"小王": False})
        system, user = build_simulator_prompts(personas, state, personas[0], "single", 15)
        for fragment in ("小王", "拿到受理编号", "专家", "对抗性：0.8", "message", "goal_status", "action"):
            assert fragment in system
        assert "对话尚未开始" in user
        assert "还可发起 15 轮" in user

    def test_prompt_replays_dialog_with_tools(self):
        personas, _ = validate_persona_configs(_personas(1))
        state = SimulationDialogState(goal_status={personas[0].name: False})
        state.record_user_turn({"speaker": "用户A", "message": "怎么提交报销？", "goal_status": False})
        state.record_agent_reply("请先上传发票", tools=["upload_form(...) -> ok"])
        system, user = build_simulator_prompts(personas, state, personas[0])
        assert "[第1轮][用户·用户A] 怎么提交报销？" in user
        assert "[第1轮][Agent] 请先上传发票" in user
        assert "[Agent 工具] upload_form(...) -> ok" in user
        assert "还可发起 14 轮" in user

    def test_blend_mode_lists_all_personas(self):
        personas, _ = validate_persona_configs(_personas(2), "blend")
        state = SimulationDialogState(goal_status={"用户A": False, "用户B": False})
        system, _ = build_simulator_prompts(personas, state, personas[0], "blend")
        assert "同时兼具以上 2 个身份" in system
        assert "用户A" in system and "用户B" in system

    def test_rotate_highlights_pending_goals(self):
        personas, _ = validate_persona_configs(_personas(2), "rotate")
        state = SimulationDialogState(goal_status={"用户A": True, "用户B": False})
        _, user = build_simulator_prompts(personas, state, personas[1], "rotate")
        assert "用户A: 已达成" in user
        assert "用户B: 未达成" in user


class TestParseSimulatorReply:
    def test_full_json_and_fenced(self):
        parsed = parse_simulator_reply(_reply_json("多少钱", "achieved", False, "speak"))
        assert parsed["parse_error"] is None
        assert parsed["message"] == "多少钱"
        assert parsed["goal_status"] is True
        assert parsed["progress"] is False
        assert parsed["action"] == "speak"
        fenced = "```json\n%s\n```" % _reply_json(action="give_up")
        parsed2 = parse_simulator_reply(fenced)
        assert parsed2["action"] == "give_up" and parsed2["parse_error"] is None

    def test_defaults_and_lenient_fields(self):
        parsed = parse_simulator_reply('{"message": "继续"}')
        assert parsed["parse_error"] is None
        assert parsed["goal_status"] is False
        assert parsed["progress"] is None
        assert parsed["action"] == "speak"
        # 布尔/字符串宽松口径
        assert parse_simulator_reply('{"message": "x", "goal_status": true}')["goal_status"] is True
        assert parse_simulator_reply('{"message": "x", "progress": "false"}')["progress"] is False
        assert parse_simulator_reply('{"message": "x", "action": "WHAT"}')["action"] == "speak"

    def test_errors_and_giveup_default_message(self):
        assert parse_simulator_reply("")["parse_error"]
        assert parse_simulator_reply("闲聊不带 JSON")["parse_error"]
        assert parse_simulator_reply('{"goal_status": "pending"}')["parse_error"]  # speak 无 message
        # give_up 无 message 给默认告别语，不视为解析失败
        parsed = parse_simulator_reply('{"action": "give_up"}')
        assert parsed["parse_error"] is None
        assert parsed["message"]


class TestCheckTermination:
    def _state(self):
        return SimulationDialogState(goal_status={"A": False, "B": False})

    def test_goal_achieved_beats_others(self):
        state = self._state()
        state.record_user_turn({"speaker": "A", "message": "m", "goal_status": True, "goal_apply": ["A"]})
        state.record_user_turn({"speaker": "B", "message": "m", "goal_status": True, "goal_apply": ["B"]})
        result = check_termination(state, max_turns=1, min_patience=1)
        assert result["terminated"] and result["reason"] == "goal_achieved"

    def test_partial_goal_not_terminated(self):
        state = self._state()
        state.record_user_turn({"speaker": "A", "message": "m", "goal_status": True, "goal_apply": ["A"]})
        result = check_termination(state, max_turns=15, min_patience=3)
        assert result["terminated"] is False

    def test_give_up_action(self):
        state = self._state()
        state.record_user_turn({"speaker": "A", "message": "算了", "action": "give_up"})
        result = check_termination(state, max_turns=15, min_patience=3)
        assert result["terminated"] and result["reason"] == "give_up"

    def test_patience_guard(self):
        state = self._state()
        for _ in range(3):  # 连续 3 回合无进展 ≥ 最小耐心 3
            state.record_user_turn({"speaker": "A", "message": "?", "progress": False})
        result = check_termination(state, max_turns=15, min_patience=3)
        assert result["terminated"] and result["reason"] == "give_up"
        assert "失控保护" in result["detail"]

    def test_progress_resets_counter(self):
        state = self._state()
        state.record_user_turn({"speaker": "A", "message": "?", "progress": False})
        state.record_user_turn({"speaker": "A", "message": "?", "progress": False})
        state.record_user_turn({"speaker": "A", "message": "好", "progress": True})
        state.record_user_turn({"speaker": "A", "message": "?", "progress": False})
        assert state.no_progress_rounds == 1
        result = check_termination(state, max_turns=15, min_patience=3)
        assert result["terminated"] is False

    def test_max_turns(self):
        state = self._state()
        state.record_user_turn({"speaker": "A", "message": "m"})
        state.record_user_turn({"speaker": "A", "message": "m"})
        result = check_termination(state, max_turns=2, min_patience=3)
        assert result["terminated"] and result["reason"] == "max_turns"


class TestUserSimulator:
    @patch("app.services.judge_client.JudgeClient")
    def test_single_round_trip(self, mock_judge_cls):
        mock_client = MagicMock()
        mock_client.compress.return_value = _reply_json("我要报销 3580 元", "pending", True)
        mock_judge_cls.return_value = mock_client

        sim = UserSimulator(MagicMock(), _personas(1))
        assert sim.max_turns == DEFAULT_MAX_TURNS  # 缺省 15（方案 §7.1）
        state = sim.initial_state()
        reply = sim.next_message(state)

        assert reply["speaker"] == "用户A"
        assert reply["message"] == "我要报销 3580 元"
        assert reply["goal_apply"] == ["用户A"]
        # persona 目标进了 system prompt，对话状态进了 user prompt
        sent_system, sent_user = mock_client.compress.call_args[0]
        assert "完成目标A" in sent_system
        assert "对话尚未开始" in sent_user
        state.record_user_turn(reply)
        assert state.goal_status == {"用户A": False}
        assert state.to_dict()["user_turn_count"] == 1

    @patch("app.services.judge_client.JudgeClient")
    def test_rotate_pending_pool_and_rotation(self, mock_judge_cls):
        mock_client = MagicMock()
        replies = [
            _reply_json("A1", "pending"),
            _reply_json("B1", "pending"),
            _reply_json("A2", "achieved"),  # 用户A 达成后退出轮换
            _reply_json("B2", "achieved"),
        ]
        mock_client.compress.side_effect = replies
        mock_judge_cls.return_value = mock_client

        sim = UserSimulator(MagicMock(), _personas(2), combo_mode="rotate")
        state = sim.initial_state()
        speakers = []
        for _ in range(4):
            reply = sim.next_message(state)
            speakers.append(reply["speaker"])
            state.record_user_turn(reply)
        # 轮换：A→B→A(达成)→B；全部达成后轮换池退回全量 persona
        assert speakers == ["用户A", "用户B", "用户A", "用户B"]
        assert state.pending_personas() == []
        term = sim.check_termination(state)
        assert term["terminated"] and term["reason"] == "goal_achieved"

    @patch("app.services.judge_client.JudgeClient")
    def test_blend_goal_writes_all_keys(self, mock_judge_cls):
        mock_client = MagicMock()
        mock_client.compress.return_value = _reply_json("两个都办好了", "achieved")
        mock_judge_cls.return_value = mock_client

        sim = UserSimulator(MagicMock(), _personas(2), combo_mode="blend")
        state = sim.initial_state()
        reply = sim.next_message(state)
        assert reply["speaker"] == "用户A"  # blend 以首个身份发言
        assert reply["goal_apply"] == ["用户A", "用户B"]  # 判定写全部子目标
        state.record_user_turn(reply)
        assert state.goal_status == {"用户A": True, "用户B": True}
        assert sim.check_termination(state)["reason"] == "goal_achieved"

    @patch("app.services.judge_client.JudgeClient")
    def test_parse_failure_retries_once_then_raises(self, mock_judge_cls):
        mock_client = MagicMock()
        mock_client.compress.side_effect = ["不是 JSON 的闲聊", "还是闲聊"]
        mock_judge_cls.return_value = mock_client

        sim = UserSimulator(MagicMock(), _personas(1))
        with pytest.raises(UserSimulatorError):
            sim.next_message(sim.initial_state())
        assert mock_client.compress.call_count == 2  # 纠偏重试一次
        # 重试的 prompt 带纠偏标记
        retry_prompt = mock_client.compress.call_args[0][1]
        assert "纠偏" in retry_prompt

    @patch("app.services.judge_client.JudgeClient")
    def test_parse_failure_recovers_on_retry(self, mock_judge_cls):
        mock_client = MagicMock()
        mock_client.compress.side_effect = ["格式错了", _reply_json("重来")]
        mock_judge_cls.return_value = mock_client

        sim = UserSimulator(MagicMock(), _personas(1))
        reply = sim.next_message(sim.initial_state())
        assert reply["message"] == "重来"

    @patch("app.services.judge_client.JudgeClient")
    def test_llm_exception_raises_without_retry(self, mock_judge_cls):
        mock_client = MagicMock()
        mock_client.compress.side_effect = RuntimeError("裁判不可用")
        mock_judge_cls.return_value = mock_client

        sim = UserSimulator(MagicMock(), _personas(1))
        with pytest.raises(UserSimulatorError):
            sim.next_message(sim.initial_state())
        assert mock_client.compress.call_count == 1  # 通道异常不重试（JudgeClient 内部已有重试）

    def test_constructor_rejects_invalid_config(self):
        with pytest.raises(ValueError):
            UserSimulator(MagicMock(), [])
        with pytest.raises(ValueError):
            UserSimulator(MagicMock(), [{"role": "无目标"}])
        with pytest.raises(ValueError):
            UserSimulator(MagicMock(), _personas(1), combo_mode="party")

    @patch("app.services.judge_client.JudgeClient")
    def test_determinism_with_fixed_replies(self, mock_judge_cls):
        """固定 seed 口径的确定性：同样的 mock 回复序列 → 同样的对话与终止结果"""
        canned = [
            _reply_json("q1", "pending", False),
            _reply_json("q2", "pending", False),
            _reply_json("q3", "achieved", True),
        ]

        def run():
            mock_client = MagicMock()
            mock_client.compress.side_effect = list(canned)
            mock_judge_cls.return_value = mock_client
            sim = UserSimulator(MagicMock(), _personas(1))
            state = sim.initial_state()
            term = {"terminated": False}
            while not term["terminated"]:
                reply = sim.next_message(state)
                state.record_user_turn(reply)
                state.record_agent_reply("agent 回复")
                term = sim.check_termination(state)
            return state.to_dict(), term

        first = run()
        second = run()
        assert first == second
        assert first[1]["reason"] == "goal_achieved"
        assert first[0]["user_turn_count"] == 3
