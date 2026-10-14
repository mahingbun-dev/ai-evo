# tests/unit/legacy

来源：`app/services/test_*.py` 九个「死测试」（整改计划 §3 W8「归位」项）。
它们历史上与实现文件混放、不在 pytest `testpaths` 内、从未被任何 CI 执行。

W8 甄别结论（逐文件实测运行后判定）：

| 文件 | 实测 | 处置 |
|---|---|---|
| test_build_judge_payload_fix.py | 5/5 绿 | 迁入，全部保留 |
| test_e2e_evaluation_targets.py | 2/2 绿 | 迁入，全部保留 |
| test_empty_content_validation.py | 16/16 绿 | 迁入，全部保留 |
| test_evaluation_executor_latency.py | 9/9 绿 | 迁入，全部保留 |
| test_evaluation_targets_chain_result.py | 4/4 绿 | 迁入，全部保留 |
| test_latency_evaluator.py | 10/10 绿 | 迁入，全部保留 |
| test_result_aggregator_latency.py | 5/6 绿 | 迁入；删除过期的 `test_aggregate_results_latency_type_exists`（源码字面量 grep 断言，实现已重构为 `ResultAggregator.aggregate_for_task`，断言对象不存在） |
| test_result_exporter_latency.py | 6/6 绿 | 迁入，全部保留 |
| test_runtime_variables_processing.py | 10/10 绿 | 迁入，全部保留 |

这些用例是当时缺陷修复的回归资产（latency 评测、chain_result 注入、
runtime_variables `{value,type}`、空内容 0 分、build_judge_payload list 格式），
全部带 `pytestmark = pytest.mark.unit`，纳入 W8 起 CI `-m unit` 门禁。
