"""tests/ 公共 fixture（整改开发计划 §5.1「分层与基建」按需落地）

本文件按 W1 验收需要落地两个公共 fixture（独立测试角色维护）：
- readonly_dsn：W1 db_assert 专用测试 DSN——sqlite 临时文件库，自建自清，
  库内带 w1_probe 标记表（marker 行），同时供探针脚本验证「平台凭据
  可达性」：子进程若泄漏到该 DSN，能读到 marker 行即证明凭据泄漏。
- sample_script_package：由 app/static/script_templates 脚手架模板
  （hub_package）生成的真实脚本包（解包到 tmp 目录，含真实样例输入与
  sample_stdin），供冒烟与 W1 回归共用。

注意：不在此处定义 db_session 等数据库 fixture——既有测试文件各自本地
定义（模块级 fixture 优先于 conftest，为避免同名遮蔽歧义，遵循现状）。
"""
import json
import os
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# ── ragas 遥测线程歼灭（测试进程专用，必须在任何测试模块被收集前执行）──
# import ragas 会在模块级无条件启动 AnalyticsBatcher daemon 线程：
# `while self._running: time.sleep(1)` 每秒空醒检查上报缓冲，永不退出；
# RAGAS_DO_NOT_TRACK 只挡事件上报（track()）、不挡线程本身。
# 这对套件是隐形地雷：任何把全局 time.sleep 换成 no-op 假时钟的测试
# （LLM 退避序列断言等 7 处）都会被它以 CPU 速度塞入千万级记录——
# 2026-10-26 CI 三轮「整段无输出 30 分钟被杀」的真凶（fixture 窗口实测
# 1858 万条 sleep 记录，线程栈指认 ragas._analytics._flush_loop）。
# 测试进程不该发遥测：这里设 DO_NOT_TRACK 并抢先 import 后置 _running=False，
# 线程在下一次 1 秒检查时静默退出。代价是每次 pytest 会话多付一次 ragas
# 导入（~2s）；全量套件本来就要导入它，只有跑单个无关测试时纯多付。
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
try:
    from ragas import _analytics as _ragas_analytics

    _ragas_analytics._analytics_batcher._running = False
except Exception:  # pragma: no cover - ragas 缺失/内部结构变化不阻塞测试
    pass

# 脚手架模板 zip（预置模板，包根含 scripts/{name}/main.py 与 inputs/ 样例）
_TEMPLATE_ZIP = Path(__file__).resolve().parents[1] / "app" / "static" / "script_templates" / "hub_package.zip"


@pytest.fixture
def readonly_dsn(tmp_path_factory) -> str:
    """W1 db_assert 专用测试 DSN（sqlite 临时库，自建自清）

    库内结构：
    - w1_probe(id INTEGER, marker TEXT)：插一行 marker='leaked'——探针脚本
      若能连上该库并读到该行，即证明平台凭据泄漏到了脚本子进程；
    - db_assert_smoke(id INTEGER, ok INTEGER)：db_assert 断言用例的目标表
      （1 行数据），供 db_assert 经 stdin.db_dsn 自建 engine 的链路验证。

    返回 SQLAlchemy sqlite DSN（sqlite:////绝对路径）。清理由 tmp 目录承担。
    """
    db_file = tmp_path_factory.mktemp("w1_readonly_dsn") / "readonly.sqlite"
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("CREATE TABLE w1_probe (id INTEGER PRIMARY KEY, marker TEXT)")
        conn.execute("INSERT INTO w1_probe VALUES (1, 'leaked')")
        conn.execute("CREATE TABLE db_assert_smoke (id INTEGER PRIMARY KEY, ok INTEGER)")
        conn.execute("INSERT INTO db_assert_smoke VALUES (7, 0)")
        conn.commit()
    finally:
        conn.close()
    return f"sqlite:///{db_file}"


@pytest.fixture
def sample_script_package(tmp_path) -> SimpleNamespace:
    """由 app/static/script_templates 脚手架模板生成的真实脚本包

    把预置模板 zip（hub_package）解包到 tmp 目录。模板设计意图（见包内
    README）：scripts/hub_package/ 是脚本包根（main.py + lib/ 兄弟模块），
    inputs/ 是真实样例执行包数据（32 文件 + execution-manifest.json）。
    返回：
    - script_dir：脚本包根（应整体放到 SCRIPT_UPLOAD_BASE/{script_id}/ 下）
    - entrypoint：包内相对入口（main.py，__main__ 传统式 + lib 兄弟 import）
    - stdin：按 sample_stdin.json 绝对化后的标准输入（inputs.root /
      inputs.index_path 指向解包目录内真实样例数据）
    """
    pkg_root = tmp_path / "sample_pkg"
    with zipfile.ZipFile(_TEMPLATE_ZIP) as zf:
        zf.extractall(pkg_root)

    sample = json.loads((pkg_root / "sample_stdin.json").read_text(encoding="utf-8"))
    stdin = dict(sample)
    stdin["inputs"] = {
        field: {
            **spec,
            "root": str(pkg_root / spec["root"]),
            "index_path": str(pkg_root / spec["index_path"]),
        }
        for field, spec in (sample.get("inputs") or {}).items()
    }
    stdin["task_id"] = "w1_template_smoke"
    stdin["data_id"] = "sample_attempt"
    stdin["_run_dir"] = str(tmp_path / "smoke_run_dir")

    return SimpleNamespace(
        script_dir=pkg_root / "scripts" / "hub_package",
        entrypoint="main.py",
        stdin=stdin,
    )
