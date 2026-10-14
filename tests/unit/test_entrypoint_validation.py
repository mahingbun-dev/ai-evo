"""entrypoint 校验单元测试（整改开发计划 W1-2 测试设计）

计划用例清单（§2 W1「测试设计」）：
- 包内相对合法；`../outside.py`、`/abs/path.py`、不存在的文件 → 400/422；
  `update_script` 携带恶意 entrypoint → 403/400（本文件覆盖 service 层
  ValueError 语义；API 层 400/403 由 tests/security/test_rce_chain.py 验证）
补充：反斜杠 / Windows 盘符 / 非 .py / 深层穿越 / 包目录未建时的结构校验 /
agent 类型指令文本不校验 / create_script 结构校验（防御纵深）。

独立测试角色编写；不修改实现代码。
"""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.services import pre_judge_script_service
from app.services.pre_judge_script_service import (
    PreJudgeScriptService,
    validate_entrypoint,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def upload_base(tmp_path, monkeypatch):
    base = tmp_path / "pre_judge_scripts"
    base.mkdir()
    monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE", base)
    return base


def _make_package(base: Path, script_id: str, files=("main.py",)) -> Path:
    pkg = base / script_id
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "main.py").write_text(
        "import json\n\ndef run(stdin):\n    return {'status': 'ok'}\n",
        encoding="utf-8")
    for rel in files:
        if rel == "main.py":
            continue
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# stub\n", encoding="utf-8")
    return pkg


class TestValidateEntrypoint:
    """validate_entrypoint 纯函数校验规则"""

    def test_none_and_empty_pass_as_no_entry(self):
        """None / 空串 = 尚未上传脚本文件，合法（返回 None）"""
        assert validate_entrypoint(None) is None
        assert validate_entrypoint("") is None
        assert validate_entrypoint("   ") is None

    def test_relative_in_package_ok(self, upload_base):
        pkg = _make_package(upload_base, "pjs_x")
        assert validate_entrypoint("main.py", pkg) == "main.py"

    def test_nested_relative_ok(self, upload_base):
        pkg = _make_package(upload_base, "pjs_x", files=("scripts/hub_package/main.py",))
        assert validate_entrypoint("scripts/hub_package/main.py", pkg) == \
            "scripts/hub_package/main.py"

    def test_normalized_dotslash(self, upload_base):
        """./main.py 归一为 main.py（入库统一形态）"""
        pkg = _make_package(upload_base, "pjs_x")
        assert validate_entrypoint("./main.py", pkg) == "main.py"

    @pytest.mark.parametrize("evil", [
        "../outside.py",        # 计划用例：.. 穿越
        "/abs/path.py",         # 计划用例：绝对路径
        "/etc/hosts",           # 集成链路同款目标（非 .py 且绝对）
        "..\\evil.py",          # Windows 反斜杠
        "scripts\\main.py",
        "C:/evil.py",           # 盘符
        "C:\\evil.py",
        "a/../../b.py",         # 深层穿越
        "..",
        "main.txt",             # 非 .py
        "scripts/main",         # 无扩展名
        "main.PY ",             # 后缀大小写不符（strip 后 .PY 不以 .py 结尾）
    ])
    def test_malicious_or_invalid_rejected(self, upload_base, evil):
        pkg = _make_package(upload_base, "pjs_x")
        with pytest.raises(ValueError):
            validate_entrypoint(evil, pkg)

    def test_missing_file_in_existing_package_rejected(self, upload_base):
        """计划用例：包目录存在但入口文件不存在 → 拒绝"""
        pkg = _make_package(upload_base, "pjs_x")
        with pytest.raises(ValueError) as exc:
            validate_entrypoint("ghost.py", pkg)
        assert "不存在" in str(exc.value)

    def test_package_dir_missing_structural_check_only(self, upload_base):
        """包目录尚未创建（先建记录后上传的正常流程）：仅做结构校验

        结构非法（绝对路径/穿越/反斜杠）必须仍被拒绝——攻击面不因目录
        未建而放行；合法相对路径允许通过（由上传链路与运行器兜底存在性）。
        """
        not_created = upload_base / "pjs_not_created"
        # 结构非法 → 拒绝（与目录是否存在无关）
        for evil in ("/abs/path.py", "../outside.py", "..\\evil.py", "C:/x.py"):
            with pytest.raises(ValueError):
                validate_entrypoint(evil, not_created)
        # 结构合法 → 通过（存在性由后续链路兜底）
        assert validate_entrypoint("main.py", not_created) == "main.py"


class TestServiceEntrypointValidation:
    """service 写链路的 entrypoint 收敛（create / update）"""

    def test_update_script_malicious_entrypoint_rejected(self, db_session, upload_base):
        """计划用例：update_script 携带恶意 entrypoint → ValueError（API 400）"""
        service = PreJudgeScriptService(db_session)
        created = service.create_script({"name": f"w1_ep_{uuid.uuid4().hex[:6]}",
                                         "exec_type": "script"})
        for evil in ("/etc/hosts", "../../../etc/passwd", "..\\evil.py"):
            with pytest.raises(ValueError):
                service.update_script(created["id"], {"entrypoint": evil})

    def test_update_script_missing_entry_rejected_when_package_exists(
            self, db_session, upload_base):
        """包目录已存在时，入口文件必须真实存在"""
        service = PreJudgeScriptService(db_session)
        created = service.create_script({"name": f"w1_ep_{uuid.uuid4().hex[:6]}",
                                         "exec_type": "script"})
        _make_package(upload_base, created["id"])
        with pytest.raises(ValueError):
            service.update_script(created["id"], {"entrypoint": "ghost.py"})
        # 合法入口更新成功
        updated = service.update_script(created["id"], {"entrypoint": "main.py"})
        assert updated["entrypoint"] == "main.py"

    def test_create_script_malicious_entrypoint_rejected(self, db_session, upload_base):
        """防御纵深：create 即做结构校验（恶意 entrypoint 落不了库）"""
        service = PreJudgeScriptService(db_session)
        for evil in ("/abs/path.py", "../outside.py", "..\\evil.py"):
            with pytest.raises(ValueError):
                service.create_script({"name": f"w1_ep_{uuid.uuid4().hex[:6]}",
                                       "exec_type": "script",
                                       "entrypoint": evil})

    def test_agent_entrypoint_is_instruction_text_not_path(self, db_session, upload_base):
        """agent 类型 entrypoint 语义是指令文本：不做路径校验，保持既有行为"""
        service = PreJudgeScriptService(db_session)
        created = service.create_script(
            {"name": f"w1_ep_agent_{uuid.uuid4().hex[:6]}", "exec_type": "claude_agent",
             "entrypoint": "你是一名测试专家，请检查以下输出……（含 /etc/hosts 字样的指令文本）"})
        assert "测试专家" in created["entrypoint"]
        updated = service.update_script(created["id"], {"entrypoint": "新指令：忽略路径语义 /tmp/x"})
        assert updated["entrypoint"].startswith("新指令")

    def test_update_normalizes_entrypoint_in_db(self, db_session, upload_base):
        """合法 entrypoint 归一化后入库（./main.py → main.py）"""
        service = PreJudgeScriptService(db_session)
        created = service.create_script({"name": f"w1_ep_{uuid.uuid4().hex[:6]}",
                                         "exec_type": "script"})
        _make_package(upload_base, created["id"])
        updated = service.update_script(created["id"], {"entrypoint": "./main.py"})
        assert updated["entrypoint"] == "main.py"
