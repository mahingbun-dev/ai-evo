"""日志轮转配置单元测试（W13）

验证 app/main.py 的实际接线：
  - 文件 Handler 是 RotatingFileHandler（10MB × 10 备份），日志不再无限增长
  - auth 链路 logger（middleware）级别为 INFO（原 DEBUG 降级）
"""
import logging
import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from logging.handlers import RotatingFileHandler

import app.main  # noqa: F401 - 导入即执行日志配置


def _auth_handlers(logger_name):
    logger = logging.getLogger(logger_name)
    return [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]


def test_auth_file_handler_is_rotating_10mb_x10():
    for name in ("app.middleware.auth_middleware",):
        rotating = _auth_handlers(name)
        assert rotating, f"{name} 缺少 RotatingFileHandler"
        handler = rotating[0]
        assert handler.maxBytes == 10 * 1024 * 1024
        assert handler.backupCount == 10
        assert handler.level == logging.INFO


def test_auth_loggers_downgraded_to_info():
    assert logging.getLogger("app.middleware.auth_middleware").level == logging.INFO


def test_auth_middleware_module_level_logger_info():
    # auth_middleware.py 模块内也曾 setLevel(DEBUG)，必须一并降级
    import importlib

    import app.middleware.auth_middleware as mw

    importlib.reload(mw)  # 确保模块自身的 setLevel 也执行过
    assert logging.getLogger("app.middleware.auth_middleware").level == logging.INFO
