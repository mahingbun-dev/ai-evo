class JudgePoolError(RuntimeError):
    """并发池错误基类"""


class JudgePoolTimeout(JudgePoolError):
    """等待资源超时"""


class JudgePoolAborted(JudgePoolError):
    """等待期间任务被停止"""


class JudgePoolRedisSyncError(JudgePoolError):
    """配置已落库但 Redis 同步失败：本次配置只在当前进程可见，恢复后由空 Redis 回填"""

    def __init__(self, message: str, config):
        super().__init__(message)
        self.config = config
