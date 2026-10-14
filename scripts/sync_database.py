#!/usr/bin/env python3
"""
数据库同步脚本
功能：
1. 比对远程数据库和本地数据库中除了 alembic 相关表格外的同名表
2. 将远程数据库的同名表数据覆盖到本地数据库（先物理删除本地数据）
"""

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json
from psycopg2.extensions import register_adapter, parse_dsn
from typing import Set, List, Dict, Any
import logging
import os

# 注册 JSON/JSONB 适配器，避免 psycopg2 对 dict/list 的警告
register_adapter(dict, Json)
register_adapter(list, Json)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _db_config_from_env(var_name: str) -> Dict[str, str]:
    """从环境变量解析 PostgreSQL 连接配置（W6-3：禁止在代码中硬编码口令）。

    接受标准连接串，如 postgresql://user:password@host:5432/dbname
    缺失时 fail-fast 退出并提示注入方式。
    """
    dsn = os.environ.get(var_name, "").strip()
    if not dsn:
        raise SystemExit(
            f"缺少环境变量 {var_name}：请以 PostgreSQL 连接串注入后再运行本脚本，"
            f"例如 {var_name}='postgresql://<user>:<password>@<host>:5432/<database>'"
        )
    try:
        return parse_dsn(dsn)
    except Exception as e:
        raise SystemExit(f"环境变量 {var_name} 不是合法的 PostgreSQL 连接串: {e}")


# 数据库连接配置（远程=数据源，本地=被覆盖方；均从环境变量注入）
REMOTE_DB_CONFIG = _db_config_from_env("REMOTE_DATABASE_URL")
LOCAL_DB_CONFIG = _db_config_from_env("LOCAL_DATABASE_URL")

# 需要排除的表名模式（alembic 相关）
EXCLUDED_PATTERNS = ["alembic", "pg_toast", "pg_catalog"]

# 需要排除的序列名模式
SEQUENCE_EXCLUDED_PATTERNS = ["alembic", "pg_toast", "pg_catalog"]


def get_table_names(connection) -> Set[str]:
    """获取数据库中所有表名（排除系统表）"""
    query = """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename;
    """
    with connection.cursor() as cursor:
        cursor.execute(query)
        tables = {row[0] for row in cursor.fetchall()}
    return tables


def filter_tables(tables: Set[str], exclude_patterns: List[str]) -> Set[str]:
    """根据排除模式过滤表名"""
    filtered = set()
    for table in tables:
        # 排除包含 alembic 等关键字的表
        if any(pattern in table.lower() for pattern in exclude_patterns):
            continue
        filtered.add(table)
    return filtered


def compare_table_structures(remote_conn, local_conn, table_name: str) -> tuple:
    """
    对比远程和本地表的结构
    返回: (is_match, differences)
    """
    def get_columns(connection, table):
        query = """
            SELECT column_name, data_type, is_nullable, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = %s AND table_schema = 'public'
            ORDER BY ordinal_position;
        """
        with connection.cursor() as cursor:
            cursor.execute(query, (table,))
            return {(row[0], row[1], row[2], row[3]) for row in cursor.fetchall()}

    try:
        remote_cols = get_columns(remote_conn, table_name)
        local_cols = get_columns(local_conn, table_name)

        if remote_cols == local_cols:
            return True, []
        else:
            diffs = []
            # 远程有而本地没有的列
            remote_only = remote_cols - local_cols
            for col in remote_only:
                diffs.append(f"远程独有列: {col[0]} ({col[1]})")
            # 本地有而远程没有的列
            local_only = local_cols - remote_cols
            for col in local_only:
                diffs.append(f"本地独有列: {col[0]} ({col[1]})")
            return False, diffs
    except Exception as e:
        return False, [f"结构对比失败: {e}"]


def get_table_columns(connection, table_name: str) -> List[Dict[str, Any]]:
    """获取表的列信息"""
    query = """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = %s AND table_schema = 'public'
        ORDER BY ordinal_position;
    """
    with connection.cursor() as cursor:
        cursor.execute(query, (table_name,))
        columns = [
            {"name": row[0], "type": row[1], "nullable": row[2]}
            for row in cursor.fetchall()
        ]
    return columns


def get_table_row_count(connection, table_name: str) -> int:
    """获取表的行数"""
    query = sql.SQL("SELECT COUNT(*) FROM {}").format(
        sql.Identifier(table_name)
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
        return cursor.fetchone()[0]


def truncate_table(connection, table_name: str):
    """物理删除表中所有数据（不使用CASCADE，避免绕过FK检查）"""
    with connection.cursor() as cursor:
        # 使用 DELETE 而不是 TRUNCATE，因为 TRUNCATE CASCADE 会绕过
        # session_replication_role = REPLICA 的 FK 检查禁用
        cursor.execute(sql.SQL("DELETE FROM {}").format(
            sql.Identifier(table_name)
        ))
    connection.commit()

    # 重置序列 - 需要从 column_default 中提取真正的序列名
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT column_name, column_default
            FROM information_schema.columns
            WHERE table_name = %s AND table_schema = 'public'
            AND column_default LIKE 'nextval%%'
        """, (table_name,))
        for row in cursor.fetchall():
            col_name = row[0]
            default = row[1]
            # 提取序列名: nextval('sequence_name'::regclass)
            seq_start = default.find("'") + 1
            seq_end = default.find("'", seq_start)
            seq_name = default[seq_start:seq_end]
            with connection.cursor() as cur:
                cur.execute(sql.SQL("SELECT setval(%s, 1, false)"), (seq_name,))
    connection.commit()


def fetch_all_data(connection, table_name: str) -> List[tuple]:
    """获取表的所有数据"""
    query = sql.SQL("SELECT * FROM {}").format(
        sql.Identifier(table_name)
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
    return columns, rows


def adapt_value(v):
    """适配 PostgreSQL 特殊类型"""
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return Json(v)
    # bytes 类型 (bytea) - 直接传递
    if isinstance(v, bytes):
        return v
    # UUID 类型 - 转为字符串
    if hasattr(v, '__iter__') and not isinstance(v, (str, bytes, list)):
        # 可能是其他特殊迭代器类型，转为字符串
        return str(v)
    return v


def insert_data(connection, table_name: str, columns: List[str], rows: List[tuple]):
    """插入数据到表"""
    if not rows:
        return

    # 构建插入语句
    column_names = [sql.Identifier(col) for col in columns]
    placeholders = [sql.Placeholder() for _ in columns]

    insert_query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(column_names),
        sql.SQL(", ").join(placeholders)
    )

    with connection.cursor() as cursor:
        for row in rows:
            adapted_row = tuple(adapt_value(v) for v in row)
            cursor.execute(insert_query, adapted_row)
    connection.commit()


def disable_foreign_keys(connection):
    """禁用外键检查"""
    with connection.cursor() as cursor:
        cursor.execute("SET session_replication_role = REPLICA;")
    connection.commit()


def enable_foreign_keys(connection):
    """启用外键检查"""
    with connection.cursor() as cursor:
        cursor.execute("RESET session_replication_role;")
    connection.commit()


def get_table_sequences(connection, table_name: str) -> List[Dict[str, str]]:
    """获取表的所有序列信息（包括列名和序列名）"""
    query = """
        SELECT
            column_name,
            column_default
        FROM information_schema.columns
        WHERE table_name = %s
        AND table_schema = 'public'
        AND column_default LIKE 'nextval%%'
    """
    with connection.cursor() as cursor:
        cursor.execute(query, (table_name,))
        sequences = []
        for row in cursor.fetchall():
            column_name = row[0]
            # 提取序列名: nextval('table_id_seq'::regclass)
            default = row[1]
            seq_start = default.find("'") + 1
            seq_end = default.find("'", seq_start)
            seq_name = default[seq_start:seq_end]
            sequences.append({
                "column": column_name,
                "sequence": seq_name
            })
    return sequences


def sync_table_sequences(remote_conn, local_conn, table_name: str):
    """同步表的序列当前值"""
    sequences = get_table_sequences(remote_conn, table_name)
    for seq_info in sequences:
        seq_name = seq_info["sequence"]
        col_name = seq_info["column"]

        # 获取远程序列的当前值
        remote_query = sql.SQL("SELECT last_value FROM {}").format(
            sql.Identifier(seq_name)
        )
        with remote_conn.cursor() as cursor:
            cursor.execute(remote_query)
            remote_last_value = cursor.fetchone()[0]

        # 设置本地序列的当前值
        local_query = sql.SQL("SELECT setval('{}', %s, true)").format(
            sql.Identifier(seq_name)
        )
        with local_conn.cursor() as cursor:
            cursor.execute(local_query, (remote_last_value,))

        logger.info(f"  序列 {seq_name} (列: {col_name}) 设置为: {remote_last_value}")


def validate_tables_before_sync(remote_conn, local_conn, tables_to_sync: List[str]) -> bool:
    """
    同步前验证所有表的结构是否匹配
    返回 True 表示验证通过，False 表示有问题
    """
    logger.info("\n[验证步骤] 检查所有表的结构一致性...")
    all_valid = True

    for table in tables_to_sync:
        is_match, diffs = compare_table_structures(remote_conn, local_conn, table)
        if is_match:
            logger.info(f"  ✓ {table}: 结构一致")
        else:
            logger.warning(f"  ✗ {table}: 结构不一致")
            for diff in diffs:
                logger.warning(f"      - {diff}")
            all_valid = False

    if not all_valid:
        logger.warning("\n警告: 部分表结构不一致，同步可能会失败或数据不完整!")
        confirm = input("是否继续执行同步? (yes/no): ")
        if confirm.lower() != 'yes':
            return False
    else:
        logger.info("所有表结构验证通过!")

    return True


def get_table_dependency_order(remote_conn) -> List[str]:
    """
    获取表的外键依赖顺序
    被其他表依赖的表（被外键指向的表）应该先同步
    """
    query = """
        SELECT
            tc.table_name,
            ccu.table_name AS foreign_table_name
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
        JOIN information_schema.constraint_column_usage AS ccu
          ON ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
        AND tc.table_schema = 'public'
    """
    with remote_conn.cursor() as cursor:
        cursor.execute(query)
        fk_relations = cursor.fetchall()

    # 构建依赖图
    dependents = {}  # table -> set of tables that depend on it
    all_tables = set()

    for row in fk_relations:
        table = row[0]
        foreign_table = row[1]
        all_tables.add(table)
        all_tables.add(foreign_table)

        if foreign_table not in dependents:
            dependents[foreign_table] = set()
        dependents[foreign_table].add(table)

    # 拓扑排序：被依赖的表排在前面
    sorted_tables = []
    visited = set()
    temp_mark = set()

    def visit(table):
        if table in temp_mark:
            # 循环依赖，搁置
            logger.warning(f"  检测到循环依赖，涉及表: {table}")
            return
        if table in visited:
            return

        temp_mark.add(table)
        # 先处理当前表依赖的表（如果有的话）
        for dep in dependents.get(table, set()):
            visit(dep)
        temp_mark.remove(table)
        visited.add(table)
        sorted_tables.append(table)

    for table in all_tables:
        if table not in visited:
            visit(table)

    return sorted_tables


def sync_table(remote_conn, local_conn, table_name: str, sync_seqs: bool = True) -> bool:
    """同步单个表的数据"""
    try:
        # 获取远程表数据
        columns, rows = fetch_all_data(remote_conn, table_name)
        remote_count = len(rows)
        logger.info(f"  远程表 {table_name}: {remote_count} 条数据")

        if not rows:
            logger.info(f"  远程表 {table_name} 无数据，跳过")
            return True

        # 禁用外键检查
        disable_foreign_keys(local_conn)

        # 清空本地表
        logger.info(f"  清空本地表 {table_name}")
        truncate_table(local_conn, table_name)

        # 插入远程数据
        logger.info(f"  插入 {remote_count} 条数据到本地表 {table_name}")
        insert_data(local_conn, table_name, columns, rows)

        # 启用外键检查
        enable_foreign_keys(local_conn)

        # 同步序列值
        if sync_seqs:
            sync_table_sequences(remote_conn, local_conn, table_name)

        # 验证同步结果
        local_count = get_table_row_count(local_conn, table_name)
        if local_count != remote_count:
            logger.error(f"  警告: {table_name} 数据不一致! 远程={remote_count}, 本地={local_count}")
            raise Exception(f"数据不一致: 远程{remote_count}条 vs 本地{local_count}条")

        logger.info(f"  ✓ 表 {table_name} 同步完成 (验证通过: {local_count} 条)")
        return True

    except Exception as e:
        logger.error(f"  ✗ 同步表 {table_name} 失败: {e}")
        try:
            enable_foreign_keys(local_conn)
        except:
            pass
        return False


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("数据库同步脚本启动")
    logger.info("=" * 60)

    remote_conn = None
    local_conn = None

    try:
        # 连接远程数据库
        logger.info("\n[步骤1] 连接远程数据库...")
        remote_conn = psycopg2.connect(**REMOTE_DB_CONFIG)
        logger.info("远程数据库连接成功")

        # 连接本地数据库
        logger.info("\n[步骤2] 连接本地数据库...")
        local_conn = psycopg2.connect(**LOCAL_DB_CONFIG)
        logger.info("本地数据库连接成功")

        # 获取表名
        logger.info("\n[步骤3] 获取数据库表名...")
        remote_tables = get_table_names(remote_conn)
        local_tables = get_table_names(local_conn)
        logger.info(f"远程数据库表数量: {len(remote_tables)}")
        logger.info(f"本地数据库表数量: {len(local_tables)}")

        # 过滤掉 alembic 相关表
        remote_tables_filtered = filter_tables(remote_tables, EXCLUDED_PATTERNS)
        local_tables_filtered = filter_tables(local_tables, EXCLUDED_PATTERNS)

        # 找出共同表名
        common_tables = remote_tables_filtered & local_tables_filtered
        logger.info(f"\n排除 alembic 相关表后:")
        logger.info(f"远程数据库有效表数量: {len(remote_tables_filtered)}")
        logger.info(f"本地数据库有效表数量: {len(local_tables_filtered)}")
        logger.info(f"共同表数量: {len(common_tables)}")

        # 显示共同表名
        logger.info("\n共同表名列表:")
        for i, table in enumerate(sorted(common_tables), 1):
            remote_count = get_table_row_count(remote_conn, table)
            local_count = get_table_row_count(local_conn, table)
            logger.info(f"  {i}. {table} (远程: {remote_count} 条, 本地: {local_count} 条)")

        # 获取表的外键依赖顺序
        logger.info("\n[步骤3.5] 计算表依赖顺序...")
        all_tables_order = get_table_dependency_order(remote_conn)

        # 根据依赖顺序过滤出需要同步的表
        tables_to_sync = [t for t in all_tables_order if t in common_tables]

        # 将没有外键依赖的表（按字母顺序）添加到末尾
        no_dep_tables = sorted(common_tables - set(tables_to_sync))
        tables_to_sync.extend(no_dep_tables)

        logger.info(f"同步顺序（共 {len(tables_to_sync)} 个表）:")
        for i, table in enumerate(tables_to_sync, 1):
            logger.info(f"  {i}. {table}")

        # 验证表结构
        if not validate_tables_before_sync(remote_conn, local_conn, tables_to_sync):
            logger.info("用户取消操作")
            return

        if not common_tables:
            logger.warning("\n没有找到共同表名，退出脚本")
            return

        # 确认是否继续
        logger.info("\n" + "=" * 60)
        confirm = input(f"确认开始同步这 {len(tables_to_sync)} 个表? (yes/no): ")
        if confirm.lower() != 'yes':
            logger.info("用户取消操作")
            return

        # 执行同步
        logger.info("\n[步骤4] 开始同步数据...")
        logger.info("=" * 60)

        success_count = 0
        fail_count = 0

        for table in tables_to_sync:
            logger.info(f"\n同步表: {table}")
            if sync_table(remote_conn, local_conn, table, sync_seqs=False):
                success_count += 1
            else:
                fail_count += 1

        # 统一在最后同步所有序列值
        if success_count > 0:
            logger.info("\n" + "=" * 60)
            logger.info("[步骤5] 同步所有序列值...")
            logger.info("=" * 60)
            for table in tables_to_sync:
                try:
                    sync_table_sequences(remote_conn, local_conn, table)
                except Exception as e:
                    logger.warning(f"  同步表 {table} 的序列失败: {e}")

        # 最终同步报告
        logger.info("\n" + "=" * 60)
        logger.info("同步完成! 最终状态报告:")
        logger.info("=" * 60)

        for table in tables_to_sync:
            remote_count = get_table_row_count(remote_conn, table)
            local_count = get_table_row_count(local_conn, table)
            status = "✓" if local_count == remote_count else "✗"
            logger.info(f"  {status} {table}: 远程={remote_count}, 本地={local_count}")

        logger.info("=" * 60)
        logger.info(f"总计: 成功={success_count}, 失败={fail_count}")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"发生错误: {e}")
        raise

    finally:
        # 关闭连接
        if remote_conn:
            remote_conn.close()
            logger.info("远程数据库连接已关闭")
        if local_conn:
            local_conn.close()
            logger.info("本地数据库连接已关闭")


if __name__ == "__main__":
    main()
