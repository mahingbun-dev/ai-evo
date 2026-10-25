"""任务上下文用 contextvars（可显式复制到裁判线程池），租约用 thread-local（不跨线程继承）

任务标识/停止信号需要传入裁判线程池里的工作线程；租约必须留在申请它的线程，
否则子线程会误判为「重入」而不申请额度。
"""
import contextvars
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class JudgeContext:
    eval_task_id: Optional[str] = None
    row_idx: Optional[int] = None
    celery_task_id: Optional[str] = None
    stop_checker: Any = None


EMPTY = JudgeContext()
_ctx_var: contextvars.ContextVar[JudgeContext] = contextvars.ContextVar("judge_context", default=EMPTY)
_state = threading.local()


def current_judge_context() -> JudgeContext:
    return _ctx_var.get()


@contextmanager
def bind_judge_context(
    eval_task_id: Optional[str] = None,
    row_idx: Optional[int] = None,
    celery_task_id: Optional[str] = None,
    stop_checker: Any = None,
):
    previous = current_judge_context()
    token = _ctx_var.set(JudgeContext(
        eval_task_id=eval_task_id if eval_task_id is not None else previous.eval_task_id,
        row_idx=row_idx if row_idx is not None else previous.row_idx,
        celery_task_id=celery_task_id if celery_task_id is not None else previous.celery_task_id,
        stop_checker=stop_checker if stop_checker is not None else previous.stop_checker,
    ))
    try:
        yield
    finally:
        _ctx_var.reset(token)


def capture_context() -> contextvars.Context:
    """为每个子线程单独复制一份（同一 Context 不可被多线程并发进入）"""
    return contextvars.copy_context()


def run_in_context(ctx: contextvars.Context, fn, *args, **kwargs):
    return ctx.run(fn, *args, **kwargs)


def _leases() -> Dict[str, Any]:
    if not hasattr(_state, "leases"):
        _state.leases = {}
    return _state.leases


def current_lease(engine) -> Optional[Any]:
    return _leases().get(str(getattr(engine, "value", engine)))


def set_current_lease(engine, lease) -> None:
    _leases()[str(getattr(engine, "value", engine))] = lease


def clear_current_lease(engine, lease) -> None:
    key = str(getattr(engine, "value", engine))
    if _leases().get(key) is lease:
        _leases().pop(key, None)
