"""数据库迁移一致性检查（CI check-migrations job 与本地共用）

两个模式，分别对应「合并前」与「合并后」两道门禁：

  --static-only（合并后 / main）
      纯标准库静态检查：迁移链恰好一个 head、down_revision 无断链、revision
      不重复。秒级完成，不需要 alembic 包、不起数据库容器。合并前完整检查
      已经跑过，合并后新增的风险只有「并发合并造成的双 head / 链断裂」，
      静态检查正好覆盖，无需重跑一遍几分钟的全量验证。
      不 import alembic，也不解析 env.py，只读 alembic/versions/*.py 的
      顶层 revision / down_revision 字面量（含合并迁移的 tuple 形式）。

  --database-url（合并前 / MR、分支 push）
      完整检查，拦住两类真实事故（2026-10-14 W19、2026-10-15 plugin_config，
      都是「迁移本身有问题/没对齐，直到部署 verify 探活 503 才暴露」）：
        1. 链完整性：迁移图恰好一个 head——分叉或 down_revision 指错直接红
        2. 空库全量升级：--database-url 指向的空库上 upgrade head 必须一次跑通
           （等价于「新环境从零建库」的预演）
        3. 模型漂移：alembic check——升级后的 schema 必须与 app/models 一致，
           「改了模型忘了写迁移」在这里被拦截。当前为告警模式（仓库有漂移存量，
           见 DRIFT_BLOCKING 注释），明细照常打印不判红。

用法：
  python tools/check_migrations.py --static-only
  python tools/check_migrations.py \
      --database-url postgresql+psycopg2://user:pw@host:5432/dbname

约定：完整模式的目标库必须是空库。脚本不建库不删库，库的生命周期归调用方——
CI 里就是 job 内一次性的 postgres 容器，本地调试自己建一个临时库即可。
检查过程把 DATABASE_URL 环境变量指向目标库，alembic/env.py 的
get_database_url() 优先读环境变量，天然隔离于 alembic.ini 的占位符配置，
不存在「迁移打错库」的可能。
"""
import argparse
import ast
import os
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 模型漂移检查是否阻断。当前仓库存在漂移存量（模型声明 JSON 而迁移建的是
# JSONB、列注释不同步、索引命名差异——json 列迁 jsonb 的 P1 遗留），上线即
# 阻断会把所有流水线打红，故先以告警模式运行：每次流水线都打印漂移明细，
# 存量清理完成后把本开关改为 True。
DRIFT_BLOCKING = False

_MISSING = object()

# Python < 3.8 的字面量节点（3.8 起统一为 ast.Constant，3.14 已彻底移除）。
# 用空元组兜底：getattr 取不到时 isinstance 恒为 False，兼容 3.6 ~ 3.14+。
_LEGACY_STR = getattr(ast, "Str", ())
_LEGACY_NUM = getattr(ast, "Num", ())
_LEGACY_NAME_CONSTANT = getattr(ast, "NameConstant", ())


def _literal(node):
    """取 ast 字面量（str / None / tuple of str）；非字面量返回 _MISSING。

    静态模式刻意不 import alembic：CI 的 main 流水线跑在 shell executor 上，
    没有 Python 依赖，装全量依赖正是要省掉的那几分钟。
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, _LEGACY_STR):
        return node.s
    if isinstance(node, _LEGACY_NUM):
        return node.n
    if isinstance(node, _LEGACY_NAME_CONSTANT):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List)):
        values = []
        for elt in node.elts:
            value = _literal(elt)
            if value is _MISSING:
                return _MISSING
            values.append(value)
        return tuple(values)
    return _MISSING


def _parse_migration(path: Path):
    """解析迁移文件的顶层 revision / down_revision / branch_labels 字面量。

    返回 dict（至少含 revision）；没有 revision 赋值（非迁移文件）返回 None。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if name in ("revision", "down_revision", "branch_labels"):
            found[name] = _literal(value)
    if "revision" not in found:
        return None
    return found


def check_static(versions_dir: Path) -> int:
    """静态检查迁移链：单 head、down_revision 无断链、revision 不重复。

    与 alembic 的 ScriptDirectory.get_heads() 同口径（head = 未被任何
    down_revision 引用的 revision）；不支持 branch_labels（本项目全部为 None，
    遇到非 None 直接报错，避免给出错误结论）。
    """
    files = sorted(versions_dir.glob("*.py"))
    if not files:
        print(f"FAIL：{versions_dir} 下没有迁移文件", file=sys.stderr)
        return 1

    revisions = {}       # revision -> 文件名
    parents = []         # (文件名, revision, parents)
    problems = []
    skipped = []

    for path in files:
        try:
            info = _parse_migration(path)
        except SyntaxError as exc:
            problems.append(
                f"{path.name}: 语法错误，无法解析（{exc.msg}，第 {exc.lineno} 行）"
            )
            continue
        if info is None:
            # alembic 对缺 revision 的 .py 会直接 CommandError；静态检查不能
            # 静默放过（拼错 revison 这类笔误正是要拦的）。只放行约定的
            # 非迁移文件：__init__.py 与下划线开头的辅助模块。
            if path.name == "__init__.py" or path.name.startswith("_"):
                skipped.append(path.name)
            else:
                problems.append(
                    f"{path.name}: 未找到顶层 revision 赋值——迁移文件必须有 "
                    f"revision / down_revision（拼写错误？）；非迁移文件请以下划线开头命名"
                )
            continue
        rev = info.get("revision", _MISSING)
        if rev is _MISSING or not isinstance(rev, str):
            problems.append(f"{path.name}: revision 不是可解析的字符串字面量")
            continue
        down = info.get("down_revision", _MISSING)
        if down is _MISSING:
            problems.append(f"{path.name}: down_revision 不是可解析的字面量，无法静态检查")
            continue
        labels = info.get("branch_labels", _MISSING)
        if labels not in (None, _MISSING):
            problems.append(
                f"{path.name}: 含 branch_labels={labels!r}，静态模式不支持分支标签，"
                f"请改用完整模式（--database-url）"
            )
            continue
        if rev in revisions:
            problems.append(
                f"revision 重复：{rev} 同时出现在 {revisions[rev]} 和 {path.name}"
            )
            continue
        revisions[rev] = path.name
        if down is None:
            parents.append((path.name, rev, ()))
        elif isinstance(down, str):
            parents.append((path.name, rev, (down,)))
        elif isinstance(down, tuple) and all(isinstance(p, str) for p in down):
            parents.append((path.name, rev, down))
        else:
            problems.append(f"{path.name}: down_revision 类型异常：{down!r}")

    referenced = set()
    for name, rev, rev_parents in parents:
        for parent in rev_parents:
            referenced.add(parent)
            if parent not in revisions:
                problems.append(
                    f"{name}（revision {rev}）: down_revision 指向不存在的迁移 "
                    f"{parent!r}（断链）"
                )

    heads = sorted(set(revisions) - referenced)
    print(f"[静态] 扫描 {len(files)} 个迁移文件，解析 {len(revisions)} 个")
    if skipped:
        print(f"[静态] 跳过无 revision 的文件：{', '.join(skipped)}")
    print(f"[静态] heads：{heads}")

    if len(heads) != 1:
        problems.append(
            f"迁移链存在 {len(heads)} 个 head（{heads}）——分叉或 down_revision 指错。"
            f"并发合并时最常见：把并行那条链接回主链，或写 merge 迁移"
            f"（参考 alembic/versions/6951565fd619_merge_two_heads.py）"
        )

    if problems:
        print("FAIL：迁移链静态检查未通过：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"通过：迁移链恰好一个 head（{heads[0]}），down_revision 无断链")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="alembic 迁移一致性检查")
    parser.add_argument(
        "--database-url",
        help="空库的 SQLAlchemy 连接串（postgresql+psycopg2://...）；完整模式必填",
    )
    parser.add_argument(
        "--static-only", action="store_true",
        help="只做静态迁移链检查（单 head / 无断链），不连数据库、不依赖 alembic",
    )
    args = parser.parse_args()

    if args.static_only:
        if args.database_url:
            parser.error("--static-only 与 --database-url 不能同时使用")
        return check_static(REPO_ROOT / "alembic" / "versions")

    if not args.database_url:
        parser.error("完整模式需要 --database-url（静态检查请用 --static-only）")

    # alembic.ini 的 script_location 是相对仓库根的相对路径，必须以仓库根为 cwd
    os.chdir(REPO_ROOT)
    os.environ["DATABASE_URL"] = args.database_url

    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")

    try:
        heads = ScriptDirectory.from_config(cfg).get_heads()
        print(f"[1/3] 迁移链 head 检查：{heads}")
        if len(heads) != 1:
            print(f"FAIL：迁移链存在 {len(heads)} 个 head（{heads}）——"
                  f"分叉或 down_revision 指错，先修链再提 MR", file=sys.stderr)
            return 1

        print("[2/3] 空库全量升级：alembic upgrade head ...")
        command.upgrade(cfg, "head")

        print("[3/3] 模型漂移检查：alembic check（schema 必须与 app/models 一致）")
        try:
            command.check(cfg)
        except Exception as e:
            if DRIFT_BLOCKING:
                raise
            # 告警模式：漂移明细照常打印（每次流水线可见），不判红
            print("【告警·不阻断】存在模型-迁移漂移存量（JSON/JSONB、列注释、"
                  "索引命名——P1 遗留，清理后把 check_migrations.DRIFT_BLOCKING 改 True）：",
                  file=sys.stderr)
            print(str(e), file=sys.stderr)
            print("迁移检查通过（漂移检查为告警模式）：链唯一 head、空库可全量升级")
            return 0
    except Exception as e:
        print(f"FAIL：{e}", file=sys.stderr)
        traceback.print_exc()
        return 1

    print("迁移检查通过：链唯一 head、空库可全量升级、schema 与模型一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
