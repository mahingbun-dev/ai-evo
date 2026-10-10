"""
根 conftest.py
"""

import sys
from pathlib import Path

# 确保项目根目录在 Python 路径中
_root_dir = Path(__file__).parent
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))