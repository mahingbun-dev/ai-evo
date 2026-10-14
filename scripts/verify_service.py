import unittest
import sys
import os

# 将项目根目录添加到 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import get_db

class TestService(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("app.db.session.SessionLocal")
    def test_root(self, mock_session):
        """验证根路径"""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "Welcome to AI Evaluation Platform"})

    @patch("app.services.excel_service.ExcelService.upload_and_save")
    def test_upload_excel_mock(self, mock_upload):
        """Mock 数据库上传验证接口逻辑"""
        mock_upload.return_value = MagicMock(id=1, filename="test.xlsx")

        # 模拟文件上传
        files = {"file": ("test.xlsx", b"dummy content", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        response = self.client.post("/api/v1/upload-excel", files=files)

        self.assertEqual(response.status_code, 200)
        self.assertIn("id", response.json())
        self.assertEqual(response.json()["filename"], "test.xlsx")

if __name__ == "__main__":
    unittest.main()
