"""
Celery 应用配置

使用 Redis 作为消息代理和结果后端。
"""
from celery import Celery
from app.core.config import get_settings

settings = get_settings()

# 创建 Celery 实例
celery_app = Celery(
    "ai_eval",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

# Celery 配置
celery_app.conf.update(
    # 序列化配置
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # Worker 配置
    worker_pool="prefork",               # 多进程模式
    worker_concurrency=4,                # 并发 worker 数

    # 任务确认配置
    task_acks_late=True,                 # 任务完成后再确认
    worker_prefetch_multiplier=1,        # 每次只取一个任务

    # 任务跟踪配置
    task_track_started=True,             # 跟踪任务启动状态
    task_time_limit=7200,                # 单任务最长 2 小时
    task_soft_time_limit=6600,           # 软超时 110 分钟

    # 结果配置
    result_expires=3600,                 # 结果过期时间 1 小时

    # 时区配置
    timezone="Asia/Shanghai",
    enable_utc=True,
)

# 自动发现任务模块
celery_app.autodiscover_tasks(["app.tasks"])
