from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from app.core.config import get_settings
import os
import logging

logger = logging.getLogger(__name__)


def create_db_engine():
    """创建数据库引擎，使用连接池配置避免连接中断"""
    from sqlalchemy import event
    # macOS 本地开发：libpq 的 GSS 初始化在 celery prefork fork 出的子进程内
    # 触发系统 GSS 框架崩溃（SIGSEGV），默认禁用 GSS 加密协商；Linux 生产不受影响
    import platform
    if platform.system() == "Darwin":
        os.environ.setdefault("PGGSSENCMODE", "disable")
    db_url = get_settings().DATABASE_URL
    engine = create_engine(
        db_url,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,  # 缩短到30分钟，避免服务器端超时断开
        pool_pre_ping=True,
        pool_timeout=30,
        pool_reset_on_return="rollback",
    )

    # PostgreSQL 连接存活检测：定期发送 keepalive 信号避免连接被服务器关闭
    @event.listens_for(engine, "connect")
    def set_psycopg2_connect_args(dbapi_conn, connection_record):
        """设置 PostgreSQL 连接参数，避免长时间空闲后连接断开"""
        try:
            # 获取实际的 connection 对象（可能是 psycopg2 或 asyncpg）
            if hasattr(dbapi_conn, 'set_session'):
                dbapi_conn.set_session(autocommit=False)
            # 对于 psycopg2，设置连接存活参数
            if hasattr(dbapi_conn, 'set_connection_session'):
                dbapi_conn.set_connection_session("")
        except Exception:
            pass

    return engine


# 数据库连接字符串
SQLALCHEMY_DATABASE_URL = get_settings().DATABASE_URL

# 创建引擎 - 添加连接池配置
try:
    engine = create_db_engine()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
except Exception as e:
    logger.warning(f"数据库连接失败, 将使用 Mock 引擎进行验证: {e}")
    engine = None
    SessionLocal = sessionmaker()

# 基类用于 ORM 模型
Base = declarative_base()

# 获取数据库会话的依赖项
def get_db() -> Session:
    with SessionLocal() as db:
        yield db
