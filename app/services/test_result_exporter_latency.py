"""
测试 ResultExporter 对 latency 类型评测结果的导出支持
"""
import pytest
from app.services.result_aggregator import ResultExporter


class TestResultExporterLatency:
    """测试 ResultExporter 的 latency 导出功能"""

    def test_export_to_excel_latency(self):
        """验证 export_to_excel 对 evaluation_type="latency" 能正确处理"""
        # 构造 latency 评测结果数据
        results = [
            {
                "total_count": 3,
                "passed_count": 2,
                "failed_count": 1,
                "pass_rate": 0.667,
                "avg_duration": 1.5,
                "records": [
                    {
                        "index": 1,
                        "duration": 1.2,
                        "timestamp": "2026-04-09T10:00:00",
                        "passed": True,
                        "input_tokens": 100,
                        "output_tokens": 200,
                    },
                    {
                        "index": 2,
                        "duration": 1.5,
                        "timestamp": "2026-04-09T10:00:01",
                        "passed": True,
                        "input_tokens": 150,
                        "output_tokens": 250,
                    },
                    {
                        "index": 3,
                        "duration": 2.5,
                        "timestamp": "2026-04-09T10:00:02",
                        "passed": False,
                        "input_tokens": 120,
                        "output_tokens": 180,
                    },
                ],
            }
        ]

        # 执行导出
        excel_bytes = ResultExporter.export_to_excel(results, evaluation_type="latency")

        # 验证返回的是 bytes 类型
        assert isinstance(excel_bytes, bytes), "export_to_excel 应返回 bytes 类型"
        assert len(excel_bytes) > 0, "export_to_excel 返回的 bytes 不应为空"

    def test_export_to_csv_latency(self):
        """验证 export_to_csv 对 evaluation_type="latency" 能正确处理"""
        # 构造 latency 评测结果数据
        results = [
            {
                "total_count": 3,
                "passed_count": 2,
                "failed_count": 1,
                "pass_rate": 0.667,
                "avg_duration": 1.5,
                "records": [
                    {
                        "index": 1,
                        "duration": 1.2,
                        "timestamp": "2026-04-09T10:00:00",
                        "passed": True,
                        "input_tokens": 100,
                        "output_tokens": 200,
                    },
                    {
                        "index": 2,
                        "duration": 1.5,
                        "timestamp": "2026-04-09T10:00:01",
                        "passed": True,
                        "input_tokens": 150,
                        "output_tokens": 250,
                    },
                    {
                        "index": 3,
                        "duration": 2.5,
                        "timestamp": "2026-04-09T10:00:02",
                        "passed": False,
                        "input_tokens": 120,
                        "output_tokens": 180,
                    },
                ],
            }
        ]

        # 执行导出
        csv_str = ResultExporter.export_to_csv(results, evaluation_type="latency")

        # 验证返回的是字符串类型
        assert isinstance(csv_str, str), "export_to_csv 应返回 str 类型"
        assert len(csv_str) > 0, "export_to_csv 返回的字符串不应为空"

        # 验证 CSV 包含预期的表头
        assert "index" in csv_str, "CSV 应包含 index 列"
        assert "duration_seconds" in csv_str, "CSV 应包含 duration_seconds 列"
        assert "status" in csv_str, "CSV 应包含 status 列"
        assert "input_tokens" in csv_str, "CSV 应包含 input_tokens 列"
        assert "output_tokens" in csv_str, "CSV 应包含 output_tokens 列"

    def test_export_to_excel_latency_empty_records(self):
        """验证 export_to_excel 对没有 records 的 latency 结果能正确处理"""
        # 构造没有 records 的 latency 评测结果
        results = [
            {
                "total_count": 0,
                "passed_count": 0,
                "failed_count": 0,
                "records": [],
            }
        ]

        # 执行导出不应报错
        excel_bytes = ResultExporter.export_to_excel(results, evaluation_type="latency")
        assert isinstance(excel_bytes, bytes), "export_to_excel 应返回 bytes 类型"

    def test_export_to_csv_latency_empty_records(self):
        """验证 export_to_csv 对没有 records 的 latency 结果能正确处理"""
        # 构造没有 records 的 latency 评测结果
        results = [
            {
                "total_count": 0,
                "passed_count": 0,
                "failed_count": 0,
                "records": [],
            }
        ]

        # 执行导出不应报错
        csv_str = ResultExporter.export_to_csv(results, evaluation_type="latency")
        assert isinstance(csv_str, str), "export_to_csv 应返回 str 类型"

    def test_export_to_excel_latency_no_records_key(self):
        """验证 export_to_excel 对没有 records 键的 latency 结果能正确处理"""
        # 构造没有 records 键的 latency 评测结果
        results = [
            {
                "total_count": 1,
                "passed_count": 1,
                "failed_count": 0,
            }
        ]

        # 执行导出不应报错
        excel_bytes = ResultExporter.export_to_excel(results, evaluation_type="latency")
        assert isinstance(excel_bytes, bytes), "export_to_excel 应返回 bytes 类型"

    def test_export_to_csv_latency_no_records_key(self):
        """验证 export_to_csv 对没有 records 键的 latency 结果能正确处理"""
        # 构造没有 records 键的 latency 评测结果
        results = [
            {
                "total_count": 1,
                "passed_count": 1,
                "failed_count": 0,
            }
        ]

        # 执行导出不应报错
        csv_str = ResultExporter.export_to_csv(results, evaluation_type="latency")
        assert isinstance(csv_str, str), "export_to_csv 应返回 str 类型"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
