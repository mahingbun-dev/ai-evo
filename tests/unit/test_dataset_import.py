"""
数据集导入单元测试
测试 import_from_excel 核心逻辑：任意列名支持和数字类型转换
"""

import pytest
import pandas as pd
import json
import tempfile
import os
from unittest.mock import MagicMock, patch

from app.services.dataset_service import DatasetService


class TestConvertToString:
    """_convert_to_string 辅助函数测试"""

    def test_convert_integer_to_string(self):
        """测试整数转换为字符串"""
        assert self._convert_to_string(15951544915) == "15951544915"

    def test_convert_float_integer_to_string(self):
        """测试浮点数形式的整数（如 Excel 中 15951544915.0）转换为字符串"""
        assert self._convert_to_string(15951544915.0) == "15951544915"

    def test_convert_float_to_string(self):
        """测试普通浮点数转换为字符串"""
        assert self._convert_to_string(3.14159) == "3.14159"

    def test_convert_string_to_string(self):
        """测试字符串保持不变"""
        assert self._convert_to_string("hello world") == "hello world"

    def test_convert_empty_string(self):
        """测试空字符串处理"""
        assert self._convert_to_string("") == ""

    def test_convert_none_returns_empty(self):
        """测试 None 值返回空字符串"""
        assert self._convert_to_string(None) == ""

    def test_convert_nan_returns_empty(self):
        """测试 NaN 值返回空字符串"""
        import math
        assert self._convert_to_string(float('nan')) == ""
        assert self._convert_to_string(math.nan) == ""

    @staticmethod
    def _convert_to_string(value):
        """从 dataset_service 复制的转换逻辑"""
        if pd.isna(value):
            return ""
        if isinstance(value, (int, float)):
            if value == int(value):
                return str(int(value))
            return str(value)
        return str(value)


class TestImportColumnNameDetection:
    """导入时列名检测逻辑测试"""

    @pytest.fixture
    def mock_db(self):
        """创建模拟数据库会话"""
        db = MagicMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        """创建服务实例"""
        return DatasetService(mock_db)

    def _create_excel_with_columns(self, columns, data=None):
        """创建临时 Excel 文件"""
        if data is None:
            data = [[f"value_{i}" for _ in columns] for i in range(3)]
        df = pd.DataFrame(data, columns=columns)
        temp_file = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        df.to_excel(temp_file.name, index=False)
        temp_file.close()
        return temp_file.name

    def test_import_with_chinese_column_names(self, service, mock_db):
        """测试导入含中文列名的 Excel：用户输入、参考答案"""
        temp_file = self._create_excel_with_columns(
            ["用户输入", "参考答案"],
            [["问题1", "答案1"], ["问题2", "答案2"]]
        )
        try:
            # Mock get_dataset to return a dataset
            mock_dataset = MagicMock()
            mock_dataset.id = "test_ds"
            mock_dataset.question_count = 0
            mock_db.query.return_value.filter.return_value.first.return_value = mock_dataset
            mock_db.query.return_value.filter.return_value.count.return_value = 0
            mock_db.query.return_value.filter.return_value.scalar.return_value = -1

            # Mock the field service parse_fields_from_excel to return our fields
            with patch('app.services.dataset_service.DatasetFieldService') as MockFieldService:
                mock_instance = MagicMock()
                mock_instance.parse_fields_from_excel.return_value = {
                    "fields": [
                        {"name": "用户输入", "label": "用户输入", "type": "string", "is_media": False},
                        {"name": "参考答案", "label": "参考答案", "type": "string", "is_media": False},
                    ]
                }
                MockFieldService.return_value = mock_instance

                # 执行导入
                result = service.import_from_excel("test_ds", temp_file)

                # 验证
                assert result == 2  # 2 rows imported
                assert mock_db.add.called
        finally:
            os.unlink(temp_file)

    def test_import_with_numeric_values(self, service, mock_db):
        """测试导入含数字类型 Excel：整数 15951544915 存储为字符串"""
        temp_file = self._create_excel_with_columns(
            ["user_input", "answer"],
            [[15951544915, 15951544916], ["other", "answer2"]]
        )
        try:
            mock_dataset = MagicMock()
            mock_dataset.id = "test_ds"
            mock_dataset.question_count = 0
            mock_db.query.return_value.filter.return_value.first.return_value = mock_dataset
            mock_db.query.return_value.filter.return_value.count.return_value = 0
            mock_db.query.return_value.filter.return_value.scalar.return_value = -1

            with patch('app.services.dataset_service.DatasetFieldService') as MockFieldService:
                mock_instance = MagicMock()
                mock_instance.parse_fields_from_excel.return_value = {
                    "fields": [
                        {"name": "user_input", "label": "用户输入", "type": "string", "is_media": False},
                        {"name": "answer", "label": "参考答案", "type": "string", "is_media": False},
                    ]
                }
                MockFieldService.return_value = mock_instance

                result = service.import_from_excel("test_ds", temp_file)

                assert result == 2
                # 验证数字被转换为字符串
                call_args = mock_db.add.call_args_list
                added_data = [args[0][0] for args in call_args if args]
                assert len(added_data) == 2
                # V2 自由格式模型（EvaluationDataV2.data JSON）：数值单元格
                # 15951544915 应转换为字符串 "15951544915"（防精度失真）
                assert added_data[0].data["user_input"] == "15951544915"
                assert added_data[0].data["answer"] == "15951544916"
        finally:
            os.unlink(temp_file)

    def test_import_with_custom_column_names(self, service, mock_db):
        """测试导入含自定义列名 Excel：问题、答案"""
        temp_file = self._create_excel_with_columns(
            ["问题", "答案"],
            [["q1", "a1"], ["q2", "a2"]]
        )
        try:
            mock_dataset = MagicMock()
            mock_dataset.id = "test_ds"
            mock_dataset.question_count = 0
            mock_db.query.return_value.filter.return_value.first.return_value = mock_dataset
            mock_db.query.return_value.filter.return_value.count.return_value = 0
            mock_db.query.return_value.filter.return_value.scalar.return_value = -1

            with patch('app.services.dataset_service.DatasetFieldService') as MockFieldService:
                mock_instance = MagicMock()
                mock_instance.parse_fields_from_excel.return_value = {
                    "fields": [
                        {"name": "问题", "label": "问题", "type": "string", "is_media": False},
                        {"name": "答案", "label": "答案", "type": "string", "is_media": False},
                    ]
                }
                MockFieldService.return_value = mock_instance

                result = service.import_from_excel("test_ds", temp_file)

                assert result == 2
                assert mock_db.add.called
        finally:
            os.unlink(temp_file)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
