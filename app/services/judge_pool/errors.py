class JudgePoolError(RuntimeError):
    """并发池错误基类"""


class JudgePoolTimeout(JudgePoolError):
    """等待资源超时"""


class JudgePoolAborted(JudgePoolError):
    """等待期间任务被停止"""
