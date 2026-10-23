class JudgePoolError(RuntimeError):
    """并发池错误基类"""


class JudgePoolTimeout(JudgePoolError):
    """等待资源超时"""


class JudgePoolAborted(JudgePoolError):
    """等待期间任务被停止"""


class JudgePoolRedisSyncError(JudgePoolError):
    """配置已落库但 Redis 同步失败：本进程按新值生效，其他 worker 由扫描器的
    DB→Redis 对账在下一周期拉回一致（Redis hash 仍装旧值，不走「空 Redis 回落」）"""

    def __init__(self, message: str, config):
        super().__init__(message)
        self.config = config
