from logging.config import fileConfig

import os

from sqlalchemy import engine_from_config

def _resolve_db_url(config) -> str:
    """数据库连接串：优先 AIEVO_DATABASE_URL（部署口径），兼容 DATABASE_URL
    （W6-2 迁移链路集成测试与容器内 docker exec 场景注入），回退 ini 配置"""
    import os
    return (
        os.environ.get("AIEVO_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
    )

from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
from app.db.session import Base
from app.models import *
target_metadata = Base.metadata


def include_object(object, name, type_, reflected, compare_to):
    """仅对 target_metadata 中定义的模型进行迁移。
    排除数据库中存在但不在代码模型中的历史表，避免生成 DROP 语句。
    """
    if type_ == "table":
        # reflected=True 表示对象来自数据库，compare_to=None 表示不在 target_metadata 中
        if reflected and compare_to is None:
            return False  # 排除：不生成 DROP
    return True


# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the string
    to the script output.

    """
    url = _resolve_db_url(config)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    section = dict(config.get_section(config.config_ini_section, {}))
    section["sqlalchemy.url"] = _resolve_db_url(config)
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
