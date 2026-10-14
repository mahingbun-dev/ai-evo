"""W3 集成测试 SIGKILL 场例子进程入口

在独立进程中安装固定打分表 mock 后运行 run_evaluation_async，
父进程在若干行落库后 SIGKILL 本进程，验证行级 write-through：
已完成行在 evaluation_results 可查（P0-E：中途被杀零结果丢失）。

环境变量：
- DATABASE_URL：临时库 DSN（父进程注入）
- W3_TASK_ID：待执行任务 ID
- W3_CALL_LOG：模型调用日志文件（逐行追加，SIGKILL 后可读）
- W3_ROW_DELAY_MS：每行模型调用延迟（拉开被杀时间窗）
"""
import os
import sys
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from tests.integration.w3_mocks import install_w3_mocks  # noqa: E402


def main() -> int:
    from app.tasks.evaluation_tasks_async import run_evaluation_async

    hooks = {
        "row_delay_ms": int(os.environ.get("W3_ROW_DELAY_MS", "0")),
        "call_log_path": os.environ.get("W3_CALL_LOG"),
    }
    task_id = os.environ["W3_TASK_ID"]
    with install_w3_mocks(hooks):
        result = run_evaluation_async(task_id)
    print(f"W3_CHILD_RESULT: {result.get('status')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
