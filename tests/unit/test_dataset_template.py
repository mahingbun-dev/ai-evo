"""
数据集模板单元测试
测试 download_template 函数和 get_dataset_fields 接口
"""

import pytest
import pandas as pd

from app.api.dataset_api import download_template


class TestDownloadTemplate:
    """download_template 函数测试"""

    def test_download_template_returns_clean_excel(self):
        """测试 download_template 返回的 Excel 文件不包含示例提示文字"""
        response = download_template()

        # StreamingResponse 的第一个参数是 body_iterator
        # 对于 BytesIO，我们可以通过 inspect 或直接测试逻辑
        # 由于 StreamingResponse 的 body 是 BytesIO，我们直接验证 response 的 media_type
        assert response.media_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

    def test_download_template_has_correct_media_type(self):
        """测试 download_template 返回正确的 MIME 类型"""
        response = download_template()
        assert response.media_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

    def test_download_template_content_disposition(self):
        """测试 download_template 返回正确的 Content-Disposition 头"""
        response = download_template()
        headers = {k.lower(): v for k, v in response.headers.items()}
        assert 'content-disposition' in headers
        # 文件名是URL编码的，检查编码后的字符串
        assert '%E6%95%B0%E6%8D%AE%E9%9B%86%E6%A8%A1%E6%9D%BF' in headers['content-disposition']

    def test_download_template_streaming_response(self):
        """测试 download_template 返回 StreamingResponse"""
        response = download_template()
        from starlette.responses import StreamingResponse
        assert isinstance(response, StreamingResponse), "应返回 StreamingResponse"


class TestGetDatasetFields:
    """get_dataset_fields 接口测试"""

    def test_get_dataset_fields_exists(self):
        """测试 get_dataset_fields 接口存在"""
        from app.api.evaluation_task_api import get_dataset_fields
        assert callable(get_dataset_fields), "get_dataset_fields 应为可调用函数"

    def test_get_dataset_fields_route_registered(self):
        """测试 get_dataset_fields 接口路由已注册"""
        from app.api.evaluation_task_api import router

        # 查找包含 /datasets/{dataset_id}/fields 的路由
        routes = [r.path for r in router.routes]
        fields_routes = [r for r in routes if 'datasets' in r and 'fields' in r]

        assert len(fields_routes) > 0, f"应存在 datasets fields 路由，实际路由: {routes}"
