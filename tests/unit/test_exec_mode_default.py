"""ARCH-1-4 执行模式缺省切换门禁：container 为企业部署默认（L1，静态断言）

设计条目 1-4（plans/2026-10-15-企业级评测系统架构设计.md §3 Phase 1 表 1-4 行）：
SCRIPT_EXEC_MODE=container 设为企业部署默认，灰度（内测团队 → 全量）；local
需显式 env 才可用。本文件钉住三件事，任一回退（缺省改回 local / compose 显式
声明被删 / 灰度文档失散）即红：

1. 代码缺省：Settings 字段默认 container（字段级断言不依赖运行环境 .env/
   环境变量）；解析级断言以 _env_file=None 干净 Settings 验证缺省值经
   get_script_exec_mode 透出——注意 tests/unit/conftest.py 把套件钉在 local，
   本文件用例体内自行 delenv 覆盖；
2. compose 接线：worker environment 显式 SCRIPT_EXEC_MODE=${SCRIPT_EXEC_MODE:-
   container}（environment 优先于 env_file、插值缺省即 container、.env 显式
   设值可覆盖——回退开关的机械基础）；
3. 文档成文：build-pipeline.md 9.1 载明灰度步骤（内测团队 → 全量）与回退
   口径（改 env 重启，不改 compose/不回滚镜像）。
"""
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit  # W8 分类门禁：纯静态断言，无 docker/网络/DB

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
BUILD_DOC_PATH = REPO_ROOT / "docs" / "build-pipeline.md"


# ---------------- 1. 代码缺省 ----------------

class TestCodeDefaultIsContainer:
    def test_settings_field_default(self):
        """字段级：缺省值就是 container（不读 .env/环境变量，跨环境确定）"""
        from app.core.config import Settings

        assert Settings.model_fields["SCRIPT_EXEC_MODE"].default == "container"

    def test_default_resolves_through_get_script_exec_mode(self, monkeypatch):
        """解析级：env 未设 + 干净 Settings（_env_file=None 绕开部署 .env）→
        get_script_exec_mode 透出缺省 container（套件级 local 钉扎先行 delenv）"""
        from app.core.config import Settings
        from app.services import script_container

        monkeypatch.delenv("SCRIPT_EXEC_MODE", raising=False)
        monkeypatch.setattr(script_container, "get_settings",
                            lambda: Settings(_env_file=None))
        assert script_container.get_script_exec_mode() == "container"
        assert script_container.container_mode_enabled() is True

    def test_explicit_local_env_wins(self, monkeypatch):
        """local 需显式 env：显式声明优先于缺省（企业部署的回退开关语义）"""
        from app.services import script_container

        monkeypatch.setenv("SCRIPT_EXEC_MODE", "local")
        assert script_container.get_script_exec_mode() == "local"
        assert script_container.container_mode_enabled() is False


# ---------------- 2. compose 接线 ----------------

class TestComposeWorkerPinsContainer:
    def test_worker_env_explicit_container_with_env_override(self):
        """worker environment 显式声明且留 .env 覆盖口：插值缺省 = container
        （environment 优先于 env_file——.env 显式设 local 即回退，见 9.1）"""
        compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        env = compose["services"]["worker"]["environment"]
        assert "SCRIPT_EXEC_MODE=${SCRIPT_EXEC_MODE:-container}" in env, (
            "worker environment 须显式 SCRIPT_EXEC_MODE（插值缺省 container）"
        )


# ---------------- 3. 灰度/回退文档成文 ----------------

class TestGrayReleaseDocs:
    def test_build_doc_documents_rollout_and_rollback(self):
        """完成标准「灰度/回退步骤成文」：9.1 载明放量路径与回退口径"""
        text = BUILD_DOC_PATH.read_text(encoding="utf-8")
        for needle in (
            "ARCH-1-4",            # 条目标记（第九节标题 + 9.1）
            "内测团队",              # 灰度第一步
            "全量",                 # 灰度第二步
            "SCRIPT_EXEC_MODE=local",  # 回退动作（.env 显式声明）
            "docker compose up -d worker api",  # 回退 = 改 env 重启
            "改 env 重启即回退",       # 回退口径原文（不改 compose/不回滚镜像）
        ):
            assert needle in text, f"build-pipeline.md 缺 {needle}"

    def test_build_doc_marks_new_default_in_policy_section(self):
        """第九节现状描述与缺省切换一致：不再有「代码默认值目前仍是 local」"""
        text = BUILD_DOC_PATH.read_text(encoding="utf-8")
        assert "代码默认值目前仍是" not in text
        assert "缺省 `container`" in text  # 配置表行新口径
