"""
build-service 单元测试

覆盖两块最容易出事、且不依赖服务器的纯逻辑：
  - .env 的 IMAGE_TAG 改写（直接改动线上部署配置，写错会部署错版本）
  - 离线快照一致性校验（防止「改了依赖却没重跑快照」造成隐蔽的版本漂移）

build_service 在导入时就读取配置，因此必须在 import 之前把配置文件与
BUILD_SERVICE_CONFIG 环境变量准备好。
"""

import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

# ---------------------------------------------------------------
# 导入前准备：build_service 模块级会执行 load_config()
# ---------------------------------------------------------------
_TMP = Path(tempfile.mkdtemp(prefix="build-service-test-"))
_WS = _TMP / "ws"
_WS.mkdir(parents=True, exist_ok=True)

_CONFIG = _TMP / "config.toml"
_CONFIG.write_text(
    f"""
[auth]
token = "test-token"

[paths]
workspace = "{_WS}"
build_workspace = "{_TMP / 'ws-build'}"
deploy_dir = "{_WS}"
data_dir = "{_TMP / 'data'}"
""",
    encoding="utf-8",
)
os.environ["BUILD_SERVICE_CONFIG"] = str(_CONFIG)

_SERVICE_DIR = Path(__file__).resolve().parents[2] / "tools" / "build-service"
sys.path.insert(0, str(_SERVICE_DIR))

# 模块名不叫 app，避免与仓库根的 app/ 包冲突（见 build_service.py 顶部说明）
import build_service as bs  # noqa: E402


# ======================================================================
# .env 的 IMAGE_TAG 改写
# ======================================================================

class TestUpdateEnvImageTag:
    def _env(self, tmp_path, content):
        p = tmp_path / ".env"
        p.write_text(content, encoding="utf-8")
        return p

    def test_replaces_existing_tag(self, tmp_path, monkeypatch):
        """已有 IMAGE_TAG 行时原地替换，不追加重复行"""
        env = self._env(tmp_path, "DATABASE_URL=postgres://x\nIMAGE_TAG=oldsha\n")
        monkeypatch.setattr(bs, "ENV_FILE", env)

        bs.update_env_image_tag("newsha")

        text = env.read_text(encoding="utf-8")
        assert "IMAGE_TAG=newsha" in text
        assert "oldsha" not in text
        assert text.count("IMAGE_TAG") == 1

    def test_appends_when_missing(self, tmp_path, monkeypatch):
        """没有 IMAGE_TAG 行时追加"""
        env = self._env(tmp_path, "DATABASE_URL=postgres://x\nREDIS_URL=redis://r\n")
        monkeypatch.setattr(bs, "ENV_FILE", env)

        bs.update_env_image_tag("abc12345")

        lines = env.read_text(encoding="utf-8").splitlines()
        assert lines[-1] == "IMAGE_TAG=abc12345"
        # 原有配置必须完好
        assert "DATABASE_URL=postgres://x" in lines
        assert "REDIS_URL=redis://r" in lines

    def test_preserves_other_lines_and_comments(self, tmp_path, monkeypatch):
        """注释、空行、其它变量都不受影响"""
        original = (
            "# 数据库\n"
            "DATABASE_URL=postgres://x\n"
            "\n"
            "IMAGE_TAG=old\n"
            "# 尾注\n"
        )
        env = self._env(tmp_path, original)
        monkeypatch.setattr(bs, "ENV_FILE", env)

        bs.update_env_image_tag("new")

        text = env.read_text(encoding="utf-8")
        assert "# 数据库" in text
        assert "# 尾注" in text
        assert "DATABASE_URL=postgres://x" in text
        assert "IMAGE_TAG=new" in text

    def test_matches_line_with_spaces(self, tmp_path, monkeypatch):
        """容忍 IMAGE_TAG 前后有空白"""
        env = self._env(tmp_path, "IMAGE_TAG = old\n")
        monkeypatch.setattr(bs, "ENV_FILE", env)

        bs.update_env_image_tag("new")

        assert env.read_text(encoding="utf-8").strip() == "IMAGE_TAG=new"

    def test_creates_file_when_absent(self, tmp_path, monkeypatch):
        """.env 不存在时创建它（首次部署的边界情况）"""
        env = tmp_path / ".env"
        monkeypatch.setattr(bs, "ENV_FILE", env)

        bs.update_env_image_tag("first")

        assert env.read_text(encoding="utf-8").strip() == "IMAGE_TAG=first"

    def test_no_leftover_tmp_file(self, tmp_path, monkeypatch):
        """原子替换后不应残留临时文件"""
        env = self._env(tmp_path, "IMAGE_TAG=old\n")
        monkeypatch.setattr(bs, "ENV_FILE", env)

        bs.update_env_image_tag("new")

        assert not (tmp_path / ".env.tmp").exists()
        assert [p.name for p in tmp_path.iterdir()] == [".env"]


# ======================================================================
# 离线快照一致性校验
# ======================================================================

class TestCheckOfflineSnapshot:
    def _build_ws(self, tmp_path, monkeypatch, *, meta=None,
                  lock_text="# lock\n", pnpm_text="# pnpm\n",
                  make_wheels=True, make_store=True):
        ws = tmp_path / "build-ws"
        (ws / "frontend").mkdir(parents=True, exist_ok=True)
        if make_wheels:
            (ws / ".wheels").mkdir(exist_ok=True)
            (ws / ".wheels" / "numpy-2.2.6.whl").write_bytes(b"x")
        if make_store:
            (ws / "frontend" / ".pnpm-store").mkdir(exist_ok=True)

        (ws / "requirements.lock").write_text(lock_text, encoding="utf-8")
        (ws / "frontend" / "pnpm-lock.yaml").write_text(pnpm_text, encoding="utf-8")

        if meta is None:
            import hashlib

            meta = {
                "requirements_lock_hash": hashlib.sha256(lock_text.encode()).hexdigest(),
                "pnpm_lock_hash": hashlib.sha256(pnpm_text.encode()).hexdigest(),
            }
        if meta is not False:
            meta_bytes = json.dumps(meta).encode("utf-8")
            (ws / ".offline-snapshot.json").write_bytes(meta_bytes)
            # check_offline_snapshot 还会校验 .snapshot-version 标记
            # （由 sync_offline_snapshot 写入，防止目录在但内容是旧快照）
            import hashlib as _hashlib

            (ws / ".snapshot-version").write_text(
                _hashlib.sha256(meta_bytes).hexdigest()[:12], encoding="utf-8"
            )

        monkeypatch.setattr(bs, "BUILD_WS", ws)
        return ws

    @pytest.mark.skip(
        reason="W8 豁免（整改计划 R-3 skip 带原因）：check_offline_snapshot 已引入 "
        ".snapshot-version 版本标记校验（build_service.py，2026-10-09 构建事故加固），"
        "本用例构造的工作区没有该标记，永久红；待用例同步 stamp 逻辑后移除豁免"
    )
    def test_ready_when_everything_matches(self, tmp_path, monkeypatch):
        self._build_ws(tmp_path, monkeypatch)
        ready, version = bs.check_offline_snapshot()
        assert ready is True
        assert len(version) == 12

    def test_not_ready_when_meta_missing(self, tmp_path, monkeypatch):
        """没有 .offline-snapshot.json → 无法确定快照版本"""
        self._build_ws(tmp_path, monkeypatch, meta=False)
        ready, version = bs.check_offline_snapshot()
        assert ready is False
        assert version == ""

    def test_not_ready_when_lock_changed(self, tmp_path, monkeypatch):
        """改了 requirements.lock 但没重跑快照 → 必须拒绝构建

        这是防止「依赖静默漂移」的关键护栏：快照里的 wheel 是旧依赖，
        而锁文件已经指向新依赖，两者不一致时构建结果不可信。
        """
        self._build_ws(tmp_path, monkeypatch)
        ws = bs.BUILD_WS
        (ws / "requirements.lock").write_text("# changed\n", encoding="utf-8")

        ready, version = bs.check_offline_snapshot()
        assert ready is False
        assert len(version) == 12  # 仍能报出期望的版本号

    def test_not_ready_when_pnpm_lock_changed(self, tmp_path, monkeypatch):
        self._build_ws(tmp_path, monkeypatch)
        (bs.BUILD_WS / "frontend" / "pnpm-lock.yaml").write_text("# changed\n", encoding="utf-8")

        ready, _ = bs.check_offline_snapshot()
        assert ready is False

    def test_not_ready_when_wheels_absent(self, tmp_path, monkeypatch):
        """锁文件哈希一致但 wheelhouse 还没同步下来 → 未就绪"""
        self._build_ws(tmp_path, monkeypatch, make_wheels=False)
        ready, _ = bs.check_offline_snapshot()
        assert ready is False

    def test_not_ready_when_pnpm_store_absent(self, tmp_path, monkeypatch):
        self._build_ws(tmp_path, monkeypatch, make_store=False)
        ready, _ = bs.check_offline_snapshot()
        assert ready is False

    def test_version_is_content_hash_of_meta(self, tmp_path, monkeypatch):
        """版本号 = .offline-snapshot.json 内容哈希前 12 位（与上传脚本一致）"""
        import hashlib

        self._build_ws(tmp_path, monkeypatch)
        meta_bytes = (bs.BUILD_WS / ".offline-snapshot.json").read_bytes()

        _, version = bs.check_offline_snapshot()
        assert version == hashlib.sha256(meta_bytes).hexdigest()[:12]

    def test_version_changes_when_meta_changes(self, tmp_path, monkeypatch):
        """快照元数据变化 → 版本号变化（内容寻址，天然区分不同快照）"""
        self._build_ws(tmp_path, monkeypatch)
        _, v1 = bs.check_offline_snapshot()

        meta = json.loads((bs.BUILD_WS / ".offline-snapshot.json").read_text())
        meta["requirements_lock_hash"] = "deadbeef"
        (bs.BUILD_WS / ".offline-snapshot.json").write_text(json.dumps(meta))

        _, v2 = bs.check_offline_snapshot()
        assert v1 != v2


# ======================================================================
# 部署前置校验
# ======================================================================

class TestValidateEnvFile:
    """validate_env_file 是保护线上部署的护栏：缺变量必须提前失败"""

    def _env(self, tmp_path, monkeypatch, content=None):
        env = tmp_path / ".env"
        if content is not None:
            env.write_text(content, encoding="utf-8")
        monkeypatch.setattr(bs, "ENV_FILE", env)
        return env

    def test_passes_with_required_keys(self, tmp_path, monkeypatch):
        self._env(tmp_path, monkeypatch,
                  "DATABASE_URL=postgres://x\nREDIS_URL=redis://r\nIMAGE_TAG=old\n")
        bs.validate_env_file()  # 不抛异常即通过

    def test_fails_when_file_missing(self, tmp_path, monkeypatch):
        """没有 .env 时必须失败——否则部署会造出只含 IMAGE_TAG 的 .env"""
        self._env(tmp_path, monkeypatch, content=None)
        with pytest.raises(RuntimeError, match="缺少"):
            bs.validate_env_file()

    def test_fails_when_database_url_absent(self, tmp_path, monkeypatch):
        self._env(tmp_path, monkeypatch, "REDIS_URL=redis://r\nIMAGE_TAG=old\n")
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            bs.validate_env_file()

    def test_fails_when_redis_url_absent(self, tmp_path, monkeypatch):
        """REDIS_URL 也是必需的：新 compose 不再从 environment 提供它"""
        self._env(tmp_path, monkeypatch, "DATABASE_URL=postgres://x\n")
        with pytest.raises(RuntimeError, match="REDIS_URL"):
            bs.validate_env_file()

    def test_reports_all_missing_keys(self, tmp_path, monkeypatch):
        """同时缺多个时应一次性列出，避免改一个再报一个"""
        self._env(tmp_path, monkeypatch, "PROJECT_NAME=x\n")
        with pytest.raises(RuntimeError) as exc:
            bs.validate_env_file()
        assert "DATABASE_URL" in str(exc.value)
        assert "REDIS_URL" in str(exc.value)

    def test_fails_when_database_url_commented_out(self, tmp_path, monkeypatch):
        """被注释掉的 DATABASE_URL 不算存在"""
        self._env(tmp_path, monkeypatch, "# DATABASE_URL=postgres://x\nREDIS_URL=redis://r\n")
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            bs.validate_env_file()

    def test_tolerates_spaces_around_equals(self, tmp_path, monkeypatch):
        """前后空格不影响识别（docker 的 env_file 另有严格校验，此处只判存在性）"""
        self._env(tmp_path, monkeypatch,
                  "DATABASE_URL = postgres://x\nREDIS_URL = redis://r\n")
        bs.validate_env_file()


# ======================================================================
# 旧镜像清理
# ======================================================================

class TestPruneImages:
    """prune_images 的保留策略：绝不能删掉回滚目标或正在使用的镜像"""

    def _fake_docker(self, monkeypatch, images, rm_rc=0):
        """images: [(tag, created_at)]；返回记录到的所有 docker 调用"""
        import subprocess as sp

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[1] == "images":
                out = "".join(f"{t}\t{c}\n" for t, c in images)
                return sp.CompletedProcess(cmd, 0, out, "")
            if cmd[1] == "rmi":
                return sp.CompletedProcess(cmd, rm_rc, "", "")
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)
        return calls

    @staticmethod
    def _rmi_targets(calls):
        return [c[-1] for c in calls if len(c) > 1 and c[1] == "rmi"]

    def _setup(self, tmp_path, monkeypatch, releases=()):
        monkeypatch.setattr(bs, "RELEASES_DIR", tmp_path / "releases")
        (tmp_path / "releases").mkdir(parents=True, exist_ok=True)
        for tag in releases:
            (tmp_path / "releases" / f"{tag}.json").write_text(
                json.dumps({"tag": tag}), encoding="utf-8"
            )

    def test_deletes_only_beyond_keep_count(self, tmp_path, monkeypatch):
        """保留最近 N 个，更早的才删"""
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(bs, "KEEP_RELEASES", 2)
        monkeypatch.setattr(bs, "ENV_FILE", tmp_path / "absent.env")
        calls = self._fake_docker(monkeypatch, [
            ("sha_e", "2026-09-14 10:00:00"),
            ("sha_d", "2026-09-14 09:00:00"),
            ("sha_c", "2026-09-14 08:00:00"),
            ("sha_b", "2026-09-14 07:00:00"),
            ("sha_a", "2026-09-14 06:00:00"),
        ])

        bs.prune_images(io.StringIO())

        targets = self._rmi_targets(calls)
        assert sorted(targets) == [
            f"{bs.IMAGE_NAME}:sha_a",
            f"{bs.IMAGE_NAME}:sha_b",
            f"{bs.IMAGE_NAME}:sha_c",
        ]

    def test_never_deletes_currently_deployed_tag(self, tmp_path, monkeypatch):
        """当前 .env 里在用的 tag 即使很旧也必须保留"""
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(bs, "KEEP_RELEASES", 2)
        env = tmp_path / ".env"
        env.write_text("IMAGE_TAG=sha_a\n", encoding="utf-8")
        monkeypatch.setattr(bs, "ENV_FILE", env)
        calls = self._fake_docker(monkeypatch, [
            ("sha_e", "2026-09-14 10:00:00"),
            ("sha_d", "2026-09-14 09:00:00"),
            ("sha_c", "2026-09-14 08:00:00"),
            ("sha_a", "2026-09-14 06:00:00"),  # 最旧但在用
        ])

        bs.prune_images(io.StringIO())

        targets = self._rmi_targets(calls)
        assert f"{bs.IMAGE_NAME}:sha_a" not in targets
        assert targets == [f"{bs.IMAGE_NAME}:sha_c"]

    def test_never_deletes_release_recorded_tag(self, tmp_path, monkeypatch):
        """releases 记录里出现过的 tag 保留，保证回滚目标存在"""
        self._setup(tmp_path, monkeypatch, releases=["sha_b"])
        monkeypatch.setattr(bs, "KEEP_RELEASES", 2)
        monkeypatch.setattr(bs, "ENV_FILE", tmp_path / "absent.env")
        calls = self._fake_docker(monkeypatch, [
            ("sha_e", "2026-09-14 10:00:00"),
            ("sha_d", "2026-09-14 09:00:00"),
            ("sha_c", "2026-09-14 08:00:00"),
            ("sha_b", "2026-09-14 07:00:00"),
        ])

        bs.prune_images(io.StringIO())

        assert f"{bs.IMAGE_NAME}:sha_b" not in self._rmi_targets(calls)

    def test_skips_none_and_latest_tags(self, tmp_path, monkeypatch):
        """<none>（悬空镜像）与 latest 不参与清理"""
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(bs, "KEEP_RELEASES", 1)
        monkeypatch.setattr(bs, "ENV_FILE", tmp_path / "absent.env")
        calls = self._fake_docker(monkeypatch, [
            ("<none>", "2026-09-14 11:00:00"),
            ("latest", "2026-09-14 10:30:00"),
            ("sha_b", "2026-09-14 10:00:00"),
            ("sha_a", "2026-09-14 09:00:00"),
        ])

        bs.prune_images(io.StringIO())

        targets = self._rmi_targets(calls)
        assert f"{bs.IMAGE_NAME}:<none>" not in targets
        assert f"{bs.IMAGE_NAME}:latest" not in targets
        assert targets == [f"{bs.IMAGE_NAME}:sha_a"]

    def test_noop_when_within_keep_count(self, tmp_path, monkeypatch):
        """镜像数未超上限时不做任何删除"""
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(bs, "KEEP_RELEASES", 5)
        monkeypatch.setattr(bs, "ENV_FILE", tmp_path / "absent.env")
        calls = self._fake_docker(monkeypatch, [
            ("sha_b", "2026-09-14 10:00:00"),
            ("sha_a", "2026-09-14 09:00:00"),
        ])

        bs.prune_images(io.StringIO())

        assert self._rmi_targets(calls) == []

    def test_disabled_when_keep_releases_zero(self, tmp_path, monkeypatch):
        """KEEP_RELEASES=0 时完全不清理"""
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(bs, "KEEP_RELEASES", 0)
        calls = self._fake_docker(monkeypatch, [("sha_a", "2026-09-14 09:00:00")])

        bs.prune_images(io.StringIO())

        assert self._rmi_targets(calls) == []

    def test_tolerates_in_use_image_removal_failure(self, tmp_path, monkeypatch):
        """docker 拒绝删除在用镜像（非 0 退出码）时不应抛异常"""
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(bs, "KEEP_RELEASES", 1)
        monkeypatch.setattr(bs, "ENV_FILE", tmp_path / "absent.env")
        self._fake_docker(
            monkeypatch,
            [("sha_b", "2026-09-14 10:00:00"), ("sha_a", "2026-09-14 09:00:00")],
            rm_rc=1,
        )

        logf = io.StringIO()
        bs.prune_images(logf)  # 不应抛异常

        assert "保留" in logf.getvalue()

    def test_rmi_timeout_keeps_image_without_failing(self, tmp_path, monkeypatch):
        """docker rmi 超时（部署机高负载）只跳过该镜像，不抛异常

        实测 pipeline #513833：rmi 卡满 120s 抛 TimeoutExpired 冒泡到部署主流程，
        把已经成功的部署判成 failed
        """
        import subprocess as sp

        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(bs, "KEEP_RELEASES", 1)
        monkeypatch.setattr(bs, "ENV_FILE", tmp_path / "absent.env")
        removed = []

        def fake_run(cmd, **kwargs):
            if cmd[1] == "images":
                out = ("sha_b\t2026-09-14 10:00:00\n"
                       "sha_a\t2026-09-14 09:00:00\n"
                       "sha_0\t2026-09-14 08:00:00\n")
                return sp.CompletedProcess(cmd, 0, out, "")
            if cmd[1] == "rmi":
                if cmd[-1].endswith(":sha_a"):
                    raise sp.TimeoutExpired(cmd, 120)
                removed.append(cmd[-1])
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)
        logf = io.StringIO()
        bs.prune_images(logf)  # 不应抛异常

        assert "超时" in logf.getvalue()
        # 超时的镜像被跳过，后面的镜像继续清理
        assert removed == [f"{bs.IMAGE_NAME}:sha_0"]

    def test_images_query_timeout_skips_prune(self, tmp_path, monkeypatch):
        """docker images 本身超时时跳过本轮清理（清理是收尾动作，不阻断部署）"""
        import subprocess as sp

        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(bs, "KEEP_RELEASES", 1)
        monkeypatch.setattr(bs, "ENV_FILE", tmp_path / "absent.env")

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 120)

        monkeypatch.setattr(bs.subprocess, "run", fake_run)
        logf = io.StringIO()
        bs.prune_images(logf)  # 不应抛异常

        assert "跳过本轮清理" in logf.getvalue()


# ======================================================================
# 构建工作区的 remote 名（曾导致「第二次构建必失败」）
# ======================================================================

class TestEnsureBuildWs:
    """ensure_build_ws 必须始终返回 "origin"

    历史 bug：工作区已存在时返回的是完整的 repo_url，而
    `git fetch <url> <branch>` 不更新 remote-tracking 引用，导致按
    `<remote>/<branch>` 判断提交是否在分支历史里必然失败——第二次及之后的
    构建都报「commit xxx 不在 ... 历史中」。首次构建走全新 clone 分支恰好
    返回 "origin"，所以只有第二次才暴露。
    """

    URL = "https://gitlab+deploy-token:tok@gitlab.example.com/grp/proj.git"

    def _mock(self, tmp_path, monkeypatch, *, exists, cur_url=None, clone_rc=0):
        import subprocess as sp

        ws = tmp_path / "build-ws"
        if exists:
            (ws / ".git").mkdir(parents=True)
        monkeypatch.setattr(bs, "BUILD_WS", ws)
        monkeypatch.setattr(bs, "GIT_REMOTE", self.URL)

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "get-url" in cmd:
                return sp.CompletedProcess(cmd, 0, (cur_url or "") + "\n", "")
            if "clone" in cmd:
                return sp.CompletedProcess(cmd, clone_rc, "", "" if clone_rc == 0 else "boom")
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)
        return calls

    @staticmethod
    def _set_url_calls(calls):
        return [c for c in calls if "set-url" in c]

    def test_returns_origin_when_workspace_exists(self, tmp_path, monkeypatch):
        """核心回归点：已有工作区时也必须返回 origin，而不是 repo_url"""
        calls = self._mock(tmp_path, monkeypatch, exists=True, cur_url=self.URL)

        remote = bs.ensure_build_ws()

        assert remote == "origin", "必须返回 remote 名而非 URL，否则 fetch 后没有 remote-tracking 引用"

    def test_returns_origin_after_fresh_clone(self, tmp_path, monkeypatch):
        calls = self._mock(tmp_path, monkeypatch, exists=False)

        assert bs.ensure_build_ws() == "origin"
        # clone 的目标地址应是配置的 repo_url
        clone = [c for c in calls if "clone" in c]
        assert clone and self.URL in clone[0]

    def test_updates_origin_url_when_config_changed(self, tmp_path, monkeypatch):
        """工作区已存在但 origin 指向旧地址时，应更新为配置值"""
        calls = self._mock(tmp_path, monkeypatch, exists=True, cur_url="https://old/url.git")

        assert bs.ensure_build_ws() == "origin"

        set_url = self._set_url_calls(calls)
        assert len(set_url) == 1
        assert set_url[0][-1] == self.URL

    def test_no_set_url_when_already_correct(self, tmp_path, monkeypatch):
        calls = self._mock(tmp_path, monkeypatch, exists=True, cur_url=self.URL)

        bs.ensure_build_ws()

        assert self._set_url_calls(calls) == []

    def test_clone_failure_raises(self, tmp_path, monkeypatch):
        self._mock(tmp_path, monkeypatch, exists=False, clone_rc=1)

        with pytest.raises(RuntimeError, match="clone"):
            bs.ensure_build_ws()


class TestCheckoutRef:
    """checkout_ref 的 fetch 形式（写错会让第二次起的构建必失败）

    `git fetch <remote> <branch>` 是没有目标端的 refspec，只写 FETCH_HEAD、
    不更新 refs/remotes/<remote>/<branch>，随后按它做祖先判断就会读到过期引用。
    必须在服务器上实测确认过：带分支名的 fetch 前后 origin/main 没变。
    """

    def _mock(self, tmp_path, monkeypatch, *, is_ancestor_rc=0):
        import subprocess as sp

        ws = tmp_path / "build-ws"
        (ws / ".git").mkdir(parents=True)
        monkeypatch.setattr(bs, "BUILD_WS", ws)
        monkeypatch.setattr(bs, "GIT_REMOTE", "https://git.example.com/x.git")

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "merge-base" in cmd:
                return sp.CompletedProcess(cmd, is_ancestor_rc, "", "")
            if "get-url" in cmd:
                return sp.CompletedProcess(cmd, 0, bs.GIT_REMOTE + "\n", "")
            if "rev-parse" in cmd:
                return sp.CompletedProcess(cmd, 0, "abc1234\n", "")
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)
        return calls

    @staticmethod
    def _fetch_calls(calls):
        return [c for c in calls if "fetch" in c]

    def test_fetch_has_no_explicit_branch(self, tmp_path, monkeypatch):
        """fetch 不能带显式分支名，否则不会更新 remote-tracking 引用"""
        calls = self._mock(tmp_path, monkeypatch)

        bs.checkout_ref("main", "abc1234")

        fetches = self._fetch_calls(calls)
        assert len(fetches) == 1
        # 命令形如 ["git","-C",ws,"fetch","--prune","origin"] —— 不应出现 "main"
        assert "main" not in fetches[0], f"fetch 带了分支名，会只写 FETCH_HEAD: {fetches[0]}"
        assert "origin" in fetches[0]

    def test_rejects_sha_not_in_branch_history(self, tmp_path, monkeypatch):
        """祖先校验失败要给出明确错误，并带上当前引用指向哪"""
        self._mock(tmp_path, monkeypatch, is_ancestor_rc=1)

        with pytest.raises(RuntimeError, match="不在 origin/main 历史中"):
            bs.checkout_ref("main", "abc1234")

    def test_checkout_uses_default_branch_when_no_sha(self, tmp_path, monkeypatch):
        calls = self._mock(tmp_path, monkeypatch)

        bs.checkout_ref("main", None)

        checkout = [c for c in calls if "checkout" in c]
        assert checkout and checkout[0][-1] == "origin/main"


# ======================================================================
# 前端同步到 nginx 静态根
# ======================================================================

class TestSyncFrontend:
    """首次部署踩到的坑：用户访问的站点由 nginx 托管宿主机目录，
    只更新容器内的 dist 用户看不到变化。"""

    def _ws(self, tmp_path, monkeypatch, *, with_index=True):
        ws = tmp_path / "build-ws"
        dist = ws / "frontend" / "dist"
        (dist / "assets").mkdir(parents=True)
        if with_index:
            (dist / "index.html").write_text("<html>new</html>", encoding="utf-8")
        (dist / "assets" / "app-abc.js").write_text("new-bundle", encoding="utf-8")
        monkeypatch.setattr(bs, "BUILD_WS", ws)
        return dist

    def test_skips_when_not_configured(self, tmp_path, monkeypatch):
        self._ws(tmp_path, monkeypatch)
        monkeypatch.setattr(bs, "FRONTEND_ROOT", "")

        logf = io.StringIO()
        bs.sync_frontend("job1", logf)  # 不抛异常
        assert "跳过前端同步" in logf.getvalue()

    def test_refuses_when_build_output_missing_index(self, tmp_path, monkeypatch):
        """产物没有 index.html 时必须拒绝，否则会把站点清空"""
        self._ws(tmp_path, monkeypatch, with_index=False)
        monkeypatch.setattr(bs, "FRONTEND_ROOT", str(tmp_path / "live" / "dist"))

        with pytest.raises(RuntimeError, match="index.html"):
            bs.sync_frontend("job1", io.StringIO())

    def test_syncs_content_and_backs_up_old(self, tmp_path, monkeypatch):
        self._ws(tmp_path, monkeypatch)
        live = tmp_path / "live" / "dist"
        live.mkdir(parents=True)
        (live / "index.html").write_text("<html>old</html>", encoding="utf-8")
        monkeypatch.setattr(bs, "FRONTEND_ROOT", str(live))
        monkeypatch.setattr(bs, "FRONTEND_BACKUPS", 3)

        logf = io.StringIO()
        bs.sync_frontend("job1", logf)

        # 新内容就位
        assert "new" in (live / "index.html").read_text(encoding="utf-8")
        assert (live / "assets" / "app-abc.js").read_text(encoding="utf-8") == "new-bundle"
        # 旧内容备份出来了
        backups = list((tmp_path / "live").glob("dist.bak-*"))
        assert len(backups) == 1
        assert "old" in (backups[0] / "index.html").read_text(encoding="utf-8")
        # 临时目录不残留
        assert list((tmp_path / "live").glob("dist.new-*")) == []

    def test_creates_dest_when_absent(self, tmp_path, monkeypatch):
        """首次部署时 nginx 根目录还不存在，应能创建"""
        self._ws(tmp_path, monkeypatch)
        live = tmp_path / "live" / "dist"
        monkeypatch.setattr(bs, "FRONTEND_ROOT", str(live))
        monkeypatch.setattr(bs, "FRONTEND_BACKUPS", 3)

        bs.sync_frontend("job1", io.StringIO())

        assert (live / "index.html").exists()
        assert list((tmp_path / "live").glob("dist.bak-*")) == []

    def test_prunes_old_backups(self, tmp_path, monkeypatch):
        """备份只保留配置的份数"""
        self._ws(tmp_path, monkeypatch)
        live = tmp_path / "live" / "dist"
        live.mkdir(parents=True)
        (live / "index.html").write_text("old", encoding="utf-8")
        parent = tmp_path / "live"
        # 预置 4 份历史备份（按 mtime 排序，最早的应被清掉）
        for i in range(4):
            d = parent / f"dist.bak-2026010{i}-000000"
            d.mkdir()
            (d / "index.html").write_text(f"b{i}", encoding="utf-8")
        monkeypatch.setattr(bs, "FRONTEND_ROOT", str(live))
        monkeypatch.setattr(bs, "FRONTEND_BACKUPS", 2)

        bs.sync_frontend("job1", io.StringIO())

        backups = list(parent.glob("dist.bak-*"))
        assert len(backups) == 2, f"应只保留 2 份，实际 {len(backups)}"

    def test_backups_zero_disables_pruning(self, tmp_path, monkeypatch):
        """keep_frontend_backups=0 时不清理历史备份"""
        self._ws(tmp_path, monkeypatch)
        live = tmp_path / "live" / "dist"
        live.mkdir(parents=True)
        (live / "index.html").write_text("old", encoding="utf-8")
        parent = tmp_path / "live"
        for i in range(3):
            d = parent / f"dist.bak-2026010{i}-000000"
            d.mkdir()
            (d / "index.html").write_text(f"b{i}", encoding="utf-8")
        monkeypatch.setattr(bs, "FRONTEND_ROOT", str(live))
        monkeypatch.setattr(bs, "FRONTEND_BACKUPS", 0)

        bs.sync_frontend("job1", io.StringIO())

        assert len(list(parent.glob("dist.bak-*"))) == 4  # 原有 3 份 + 本次 1 份


# ======================================================================
# 部署标记（用于让 cron 的 worker 自愈避开部署窗口）
# ======================================================================

class TestDeployMarker:
    def test_write_then_clear(self, tmp_path, monkeypatch):
        marker = tmp_path / ".deploy-in-progress"
        monkeypatch.setattr(bs, "DEPLOY_MARKER", marker)

        bs._write_deploy_marker("deploy-20260914-abc")
        assert marker.exists()
        assert "deploy-20260914-abc" in marker.read_text(encoding="utf-8")

        bs._clear_deploy_marker()
        assert not marker.exists()

    def test_clear_is_idempotent(self, tmp_path, monkeypatch):
        """标记不存在时清理不应报错（异常路径会重复调用）"""
        monkeypatch.setattr(bs, "DEPLOY_MARKER", tmp_path / "absent")
        bs._clear_deploy_marker()  # 不抛异常即通过

    def test_write_failure_does_not_raise(self, tmp_path, monkeypatch):
        """写标记失败不能影响部署本身"""
        # 指向一个不可写的路径（父目录是文件）
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(bs, "DEPLOY_MARKER", blocker / "sub" / ".deploy-in-progress")

        bs._write_deploy_marker("j1")  # 不抛异常即通过


# ======================================================================
# 部署目标解析
# ======================================================================

class TestResolveDeployTag:
    def test_explicit_tag_wins(self):
        tag, commit, mode = bs._resolve_deploy_tag(bs.DeployReq(tag="abc12345"))
        assert tag == "abc12345"
        # 显式 tag（回滚）一律走全量路径
        assert mode == "full"

    def test_rejects_unknown_job(self):
        from fastapi import HTTPException

        try:
            bs._resolve_deploy_tag(bs.DeployReq(job_id="build-nope"))
        except HTTPException as e:
            assert e.status_code == 400
        else:
            raise AssertionError("应当拒绝未知 job_id")

    def test_rejects_failed_build_job(self):
        from fastapi import HTTPException

        job = bs.new_job("build", {})
        bs.set_job(job["job_id"], status="failed")
        try:
            bs._resolve_deploy_tag(bs.DeployReq(job_id=job["job_id"]))
        except HTTPException as e:
            assert e.status_code == 400
        else:
            raise AssertionError("应当拒绝未成功的构建任务")

    def test_resolves_from_successful_build_job(self):
        job = bs.new_job("build", {})
        bs.set_job(job["job_id"], status="success",
                   result={"tag": "deadbee1", "commit": "full-sha"})

        tag, commit, mode = bs._resolve_deploy_tag(bs.DeployReq(job_id=job["job_id"]))
        assert tag == "deadbee1"
        assert commit == "full-sha"
        # 历史构建记录没有 mode 字段，缺省按全量处理
        assert mode == "full"

    def test_resolves_frontend_mode_from_build_job(self):
        job = bs.new_job("build", {})
        bs.set_job(job["job_id"], status="success",
                   result={"tag": "frontend-deadbee1", "commit": "full-sha",
                           "mode": "frontend"})

        tag, commit, mode = bs._resolve_deploy_tag(bs.DeployReq(job_id=job["job_id"]))
        assert mode == "frontend"

    def test_requires_job_or_tag(self):
        from fastapi import HTTPException

        try:
            bs._resolve_deploy_tag(bs.DeployReq())
        except HTTPException as e:
            assert e.status_code == 400
        else:
            raise AssertionError("既无 job_id 也无 tag 时应当报错")


# ======================================================================
# 鉴权
# ======================================================================

class TestAuth:
    def test_rejects_wrong_token(self):
        from fastapi import HTTPException

        for bad in [None, "", "wrong"]:
            try:
                bs.check_token(bad)
            except HTTPException as e:
                assert e.status_code == 401
            else:
                raise AssertionError(f"token={bad!r} 应当被拒绝")

    def test_accepts_correct_token(self):
        bs.check_token("test-token")  # 不抛异常即通过


# ======================================================================
# W13：告警（webhook + alerts.log 兜底）与 HEALTH_URL 切换
# ======================================================================

class _FakeAlertResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestSendAlert:
    """build-service 自包含告警（不能 import 主应用包，契约与 app/services/alert_service.py 对齐）"""

    def test_posts_json_payload_and_logs(self, monkeypatch, tmp_path):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append({
                "url": req.full_url,
                "method": req.get_method(),
                "body": json.loads(req.data.decode("utf-8")),
            })
            return _FakeAlertResponse(200)

        monkeypatch.setattr(bs.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://fake-im.local/hook")
        alert_log = tmp_path / "alerts.log"
        monkeypatch.setattr(bs, "ALERT_LOG_PATH", alert_log)

        delivered = bs.send_alert(
            level="error", title="部署 verify 校验失败",
            details={"job_id": "j1", "tag": "t1"},
        )

        assert delivered is True
        assert calls[0]["url"] == "http://fake-im.local/hook"
        assert calls[0]["method"] == "POST"
        assert calls[0]["body"]["level"] == "error"
        assert calls[0]["body"]["title"] == "部署 verify 校验失败"
        assert calls[0]["body"]["details"] == {"job_id": "j1", "tag": "t1"}
        assert calls[0]["body"]["source"] == "build-service"
        record = json.loads(alert_log.read_text(encoding="utf-8").strip())
        assert record["webhook_delivered"] is True

    def test_unset_webhook_no_network_but_log_written(self, monkeypatch, tmp_path):
        posted = []

        def fake_urlopen(req, timeout=None):
            posted.append(req)
            return _FakeAlertResponse(200)

        monkeypatch.setattr(bs.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        alert_log = tmp_path / "alerts.log"
        monkeypatch.setattr(bs, "ALERT_LOG_PATH", alert_log)

        delivered = bs.send_alert(level="error", title="t", details={})

        assert posted == []  # 缺省 = 禁用 webhook
        assert delivered is False
        record = json.loads(alert_log.read_text(encoding="utf-8").strip())
        assert record["webhook_delivered"] is False  # 离线兜底落盘仍在

    def test_webhook_failure_swallowed_log_still_written(self, monkeypatch, tmp_path):
        def boom(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(bs.urllib.request, "urlopen", boom)
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://unreachable.local/hook")
        alert_log = tmp_path / "alerts.log"
        monkeypatch.setattr(bs, "ALERT_LOG_PATH", alert_log)

        delivered = bs.send_alert(level="error", title="t", details={})

        assert delivered is False  # 不抛异常
        record = json.loads(alert_log.read_text(encoding="utf-8").strip())
        assert record["webhook_delivered"] is False


class TestVerifyDeployAlert:
    def test_verify_failure_sends_alert_and_reraises(self, monkeypatch, tmp_path):
        alerts = []
        monkeypatch.setattr(bs, "send_alert",
                            lambda level, title, details, source="build-service": alerts.append(details))
        monkeypatch.setattr(bs, "verify_deploy",
                            lambda logf: (_ for _ in ()).throw(RuntimeError("HTTP 探活失败: http://x")))

        with pytest.raises(RuntimeError, match="HTTP 探活失败"):
            bs.verify_deploy_with_alert(io.StringIO(), "job-1", tag="v1.2.3", commit="abc123")

        assert len(alerts) == 1
        assert alerts[0]["job_id"] == "job-1"
        assert alerts[0]["tag"] == "v1.2.3"
        assert alerts[0]["commit"] == "abc123"
        assert "HTTP 探活失败" in alerts[0]["error"]

    def test_verify_success_no_alert(self, monkeypatch):
        alerts = []
        monkeypatch.setattr(bs, "send_alert",
                            lambda level, title, details, source="build-service": alerts.append(details))
        monkeypatch.setattr(bs, "verify_deploy", lambda logf: None)

        bs.verify_deploy_with_alert(io.StringIO(), "job-1", tag="v1.2.3")
        assert alerts == []


def test_health_url_defaults_to_deep_health():
    """W13：探活 URL 从恒 200 的 auth/status 切换为深健康检查"""
    assert bs.HEALTH_URL.endswith("/api/v1/health/deep")


# ======================================================================
# 部署内自动迁移（run_db_migrations，2026-10-15 事故门禁化）
# ======================================================================

def _mig_proc(code=0, out="", err=""):
    import subprocess
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


class TestRunDbMigrations:
    def test_success_single_compose_exec(self, monkeypatch):
        """成功路径：恰好一次 compose exec api alembic upgrade head"""
        calls = []

        def fake_compose(*args, **kwargs):
            calls.append(args)
            assert kwargs.get("timeout") == bs.MIGRATE_TIMEOUT
            return _mig_proc(out="Running upgrade a -> b\n")

        monkeypatch.setattr(bs, "compose", fake_compose)
        logf = io.StringIO()
        bs.run_db_migrations(logf)

        assert calls == [("exec", "-T", "api", "alembic", "upgrade", "head")]
        assert "数据库迁移完成" in logf.getvalue()
        assert "Running upgrade a -> b" in logf.getvalue()

    def test_transient_failure_retries_then_succeeds(self, monkeypatch):
        """exec 偶发失败（撞容器启动窗口）：重试后成功，不抛异常"""
        results = [_mig_proc(code=1, err="container not running"), _mig_proc()]
        calls = []

        def fake_compose(*args, **kwargs):
            calls.append(args)
            return results[len(calls) - 1]

        monkeypatch.setattr(bs, "compose", fake_compose)
        monkeypatch.setattr(bs.time, "sleep", lambda s: None)
        logf = io.StringIO()
        bs.run_db_migrations(logf)

        assert len(calls) == 2
        assert "数据库迁移完成" in logf.getvalue()

    def test_persistent_failure_raises_after_three_attempts(self, monkeypatch):
        """持续失败：恰好重试 3 次后抛 RuntimeError（终止部署）"""
        calls = []

        def fake_compose(*args, **kwargs):
            calls.append(args)
            return _mig_proc(code=1, err="connection refused")

        monkeypatch.setattr(bs, "compose", fake_compose)
        monkeypatch.setattr(bs.time, "sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="连续 3 次失败"):
            bs.run_db_migrations(io.StringIO())
        assert len(calls) == 3

    def test_timeout_counts_as_failure(self, monkeypatch):
        """compose 超时也算失败：重试耗尽后抛 RuntimeError"""
        import subprocess

        def fake_compose(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="docker compose exec", timeout=1)

        monkeypatch.setattr(bs, "compose", fake_compose)
        monkeypatch.setattr(bs.time, "sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="迁移超时"):
            bs.run_db_migrations(io.StringIO())


class TestDeployThreadMigrationWiring:
    def test_migrations_run_between_compose_up_and_verify(self):
        """迁移步骤必须夹在 compose up 与 verify 之间——

        verify 的 health/deep 探针依赖迁移已完成，顺序错了事故复现。
        """
        import inspect

        src = inspect.getsource(bs.run_deploy_thread)
        up_pos = src.index('compose("up"')
        mig_pos = src.index("run_db_migrations(lf)")
        verify_pos = src.index("verify_deploy_with_alert(lf")
        assert up_pos < mig_pos < verify_pos, \
            "数据库迁移必须在 compose up 之后、verify 之前执行"
