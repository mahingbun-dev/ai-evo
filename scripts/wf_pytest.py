"""pytest 门控脚本（workflow 用）。

切换工作目录到评测平台 worktree 后再运行 pytest，保证相对路径
（conftest.py、pytest.ini、tests/）正确解析。

用法:
  python wf_pytest.py --full
      等价于 pytest.main(["tests/unit", "-q"])
  python wf_pytest.py --full -k smoke
      基线参数基础上附加其余参数: ["tests/unit", "-q", "-k", "smoke"]
  python wf_pytest.py tests/unit/test_foo.py -q
      其余参数原样透传给 pytest.main
"""
import os
import sys

WORKTREE = "/Users/maqb11/code/qa/ai-evo/.worktrees/hub-dataset"

os.chdir(WORKTREE)

import pytest  # noqa: E402

argv = sys.argv[1:]
if argv and argv[0] == "--full":
    pytest_args = ["tests/unit", "-q"] + argv[1:]
else:
    pytest_args = argv

sys.exit(pytest.main(pytest_args))
