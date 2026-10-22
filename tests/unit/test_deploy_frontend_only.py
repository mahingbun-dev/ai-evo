"""前端-only 快速通道单元测试（classify_change / deployed ref / run_build_thread / run_deploy_thread）

覆盖 feat/frontend-only-fastpath-worker-drain 的优化 1：
  - latest_deployed_commit / mark_deployed：「线上已部署 commit」基线的读写
  - classify_change：相对基线的变更全部落在 frontend/ 前缀内 → frontend，否则 full
  - run_build_thread：frontend 模式跳过 claude/git bin 就位与 .build-image 读取，
    docker run 注入 BUILD_FRONTEND_ONLY=1，release 记录 tag=frontend-<sha前8位>；
    force_mode="full" 强制走全量分支
  - run_deploy_thread：frontend 模式只 sync_frontend + mark_deployed，不碰容器/
    迁移/镜像清理；full 模式编排顺序（compose up < 迁移 < verify < 前端同步 <
    worker 换代，且 swap_worker 在 _clear_deploy_marker 的 finally 之后）
  - 配套静态断言（.gitlab-ci.yml / docker-compose.yml / build-in-container.sh /
    config.example.toml）

测试手法与 test_build_service.py 一致：模块级一次性准备配置并 setdefault 导入
build_service；路径类常量与外部命令全部 monkeypatch 假体，绝不真的敲 docker/git。
"""
import inspect
import io
import json
import os
import re
import subprocess as sp
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB/无 docker）

# ---------------------------------------------------------------
# 导入前准备：build_service 模块级会执行 load_config()
# ---------------------------------------------------------------
_TMP = Path(tempfile.mkdtemp(prefix="build-service-test-fe-"))
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
# 必须 setdefault 而非赋值：pytest 按文件名字母序收集，test_build_service.py
# 先加载模块，后加载的文件不得破坏已加载模块的配置
os.environ.setdefault("BUILD_SERVICE_CONFIG", str(_CONFIG))

_SERVICE_DIR = Path(__file__).resolve().parents[2] / "tools" / "build-service"
sys.path.insert(0, str(_SERVICE_DIR))

import build_service as bs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

_BASELINE = "0123456789abcdef"
_HEAD = "abcdef1234567890"


# ---------------------------------------------------------------
# 公共假体
# ---------------------------------------------------------------

def _run_build_job(branch="main", sha=None, force_mode="auto"):
    """同步调用 run_build_thread。

    run_build_thread 的 finally 会 release BUSY_LOCK（正常流程由 API 端点先
    acquire），直接调用必须先持有锁，否则 release 未持有的锁会 RuntimeError。
    """
    job = bs.new_job("build", {})
    assert bs.BUSY_LOCK.acquire(blocking=False), "BUSY_LOCK 被先前用例泄漏"
    bs.run_build_thread(job["job_id"], branch, sha, force_mode)
    return job  # 线程函数 finally 已 release，此处不得重复 release


def _run_deploy_job(tag, commit, mode):
    """同步调用 run_deploy_thread（BUSY_LOCK 语义同上）"""
    job = bs.new_job("deploy", {})
    assert bs.BUSY_LOCK.acquire(blocking=False), "BUSY_LOCK 被先前用例泄漏"
    bs.run_deploy_thread(job["job_id"], tag, commit, mode)
    return job


def _forbid_popen(monkeypatch):
    """快速路径里绝不应当启动后台 watcher——Popen 被调用即测试失败（且不产生真进程）"""
    def boom(*args, **kwargs):
        raise AssertionError("不应调用 subprocess.Popen（不该启动后台 watcher）")
    monkeypatch.setattr(bs.subprocess, "Popen", boom)


def _recording_compose(monkeypatch, events=None, rc=0):
    calls = []

    def fake_compose(*args, **kwargs):
        calls.append(args)
        if events is not None:
            events.append(("compose", args))
        return sp.CompletedProcess(args, rc, "", "")

    monkeypatch.setattr(bs, "compose", fake_compose)
    return calls


def _patch_run(monkeypatch, *, diff_out="", diff_rc=0, cmd_rc=0, events=None):
    """替换 bs.subprocess.run：diff 命令返回 diff_out/diff_rc，其它命令返回 cmd_rc"""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if events is not None:
            events.append(("subprocess", list(cmd)))
        if "diff" in cmd:
            return sp.CompletedProcess(cmd, diff_rc, diff_out, "")
        return sp.CompletedProcess(cmd, cmd_rc, "", "")

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    return calls


# ======================================================================
# BuildReq.force_mode
# ======================================================================

class TestBuildReqForceMode:
    def test_defaults_to_auto(self):
        """force_mode 缺省 auto：按相对线上已部署 commit 的变更判定构建模式"""
        assert bs.BuildReq().force_mode == "auto"

    def test_full_forces_full_build(self):
        assert bs.BuildReq(force_mode="full").force_mode == "full"


# ======================================================================
# 线上已部署 commit 基线（latest_deployed_commit / mark_deployed）
# ======================================================================

class TestDeployedRefPersistence:
    def _patch_ref(self, monkeypatch, tmp_path):
        ref = tmp_path / "deployed_commit"
        monkeypatch.setattr(bs, "DEPLOYED_REF", ref)
        return ref

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        """基线文件缺失 → ""（安全默认：全量构建）"""
        self._patch_ref(monkeypatch, tmp_path)
        assert bs.latest_deployed_commit() == ""

    def test_mark_then_read_back(self, tmp_path, monkeypatch):
        """部署成功写入的 commit 必须能作为下次构建的 diff 基线读回"""
        ref = self._patch_ref(monkeypatch, tmp_path)

        bs.mark_deployed("abcdef1234567890")

        assert bs.latest_deployed_commit() == "abcdef1234567890"
        # 原子替换后不残留临时文件
        assert not (tmp_path / "deployed.tmp").exists()

    def test_corrupted_content_does_not_raise(self, tmp_path, monkeypatch):
        """基线文件损坏（非 commit 内容）不得抛异常——最坏退化为全量构建"""
        ref = self._patch_ref(monkeypatch, tmp_path)
        ref.write_text("\x00\x01junk\nsecond line\n", encoding="utf-8")

        value = bs.latest_deployed_commit()  # 不抛异常即可通过

        assert isinstance(value, str)

    def test_non_utf8_corruption_returns_empty(self, tmp_path, monkeypatch):
        """回归（c5c1485 修复验收）：基线文件被二进制内容污染（非 UTF-8）时
        必须返回 "" 而不是抛 UnicodeDecodeError——UnicodeDecodeError 是
        ValueError 不是 OSError，漏捕获会让后续每次构建永久失败，
        而不是按设计退化安全默认（全量构建）"""
        ref = self._patch_ref(monkeypatch, tmp_path)
        ref.write_bytes(b"\xff\xfe\xfa broken")  # 非法 UTF-8 字节序列

        value = bs.latest_deployed_commit()  # 不抛异常

        assert value == ""

    def test_mark_empty_commit_writes_nothing(self, tmp_path, monkeypatch):
        """commit 为空串（如回滚无记录）时不写基线文件，避免污染判定"""
        ref = self._patch_ref(monkeypatch, tmp_path)

        bs.mark_deployed("")

        assert not ref.exists()


# ======================================================================
# 变更分类（classify_change）
# ======================================================================

class TestClassifyChange:
    def test_all_frontend_paths_is_frontend(self, tmp_path, monkeypatch):
        """变更全部落在 frontend/ 前缀内 → frontend（跳过镜像构建）"""
        monkeypatch.setattr(bs, "BUILD_WS", tmp_path)
        calls = _patch_run(
            monkeypatch, diff_out="frontend/src/App.vue\nfrontend/package.json\n"
        )

        assert bs.classify_change(_BASELINE, _HEAD) == "frontend"
        # diff 必须是 git -C <BUILD_WS> diff --name-only <baseline> <head>
        assert calls[0] == ["git", "-C", str(tmp_path), "diff", "--name-only",
                            _BASELINE, _HEAD]

    def test_backend_path_forces_full(self, tmp_path, monkeypatch):
        """夹带 app/** 等非 frontend 路径 → full（漏部署后端比多构建一次严重）"""
        monkeypatch.setattr(bs, "BUILD_WS", tmp_path)
        _patch_run(monkeypatch, diff_out="frontend/src/App.vue\napp/api/routes.py\n")

        assert bs.classify_change(_BASELINE, _HEAD) == "full"

    def test_no_baseline_forces_full_without_diff(self, tmp_path, monkeypatch):
        """无基线（首次部署/基线丢失）→ full，且不发起 diff"""
        monkeypatch.setattr(bs, "BUILD_WS", tmp_path)
        calls = _patch_run(monkeypatch, diff_out="frontend/x\n")

        assert bs.classify_change("", _HEAD) == "full"
        assert calls == []  # 空 baseline 应短路返回，不敲 git

    def test_diff_failure_forces_full(self, tmp_path, monkeypatch):
        """diff 退出码非 0（如基线 commit 不存在）→ full"""
        monkeypatch.setattr(bs, "BUILD_WS", tmp_path)
        _patch_run(monkeypatch, diff_rc=1)

        assert bs.classify_change(_BASELINE, _HEAD) == "full"

    def test_empty_diff_is_frontend(self, tmp_path, monkeypatch):
        """与线上同一 commit 的重复构建：空 diff → frontend（同步一次 dist 即可）"""
        monkeypatch.setattr(bs, "BUILD_WS", tmp_path)
        _patch_run(monkeypatch, diff_out="")

        assert bs.classify_change(_BASELINE, _HEAD) == "frontend"


# ======================================================================
# 部署目标解析的 mode 传递（_resolve_deploy_tag）
# ======================================================================

class TestResolveDeployTagMode:
    # 显式 tag → full 已由 test_build_service.py::TestResolveDeployTag 覆盖，不重复

    def test_build_result_with_frontend_mode(self):
        """构建 job result 带 mode=frontend → 部署解析为 frontend"""
        job = bs.new_job("build", {})
        bs.set_job(job["job_id"], status="success",
                   result={"tag": "frontend-abcdef12", "commit": _HEAD,
                           "mode": "frontend"})

        tag, commit, mode = bs._resolve_deploy_tag(bs.DeployReq(job_id=job["job_id"]))

        assert (tag, commit, mode) == ("frontend-abcdef12", _HEAD, "frontend")

    def test_build_result_without_mode_defaults_full(self):
        """历史构建记录没有 mode 字段 → 缺省 full（不能误走前端-only 部署）"""
        job = bs.new_job("build", {})
        bs.set_job(job["job_id"], status="success",
                   result={"tag": "deadbeef99", "commit": _HEAD})

        _, _, mode = bs._resolve_deploy_tag(bs.DeployReq(job_id=job["job_id"]))

        assert mode == "full"


# ======================================================================
# run_build_thread：frontend / full 两条分支
# ======================================================================

class TestRunBuildThread:
    def _patch_common(self, monkeypatch, tmp_path, *, with_dist=False,
                      with_build_image=False):
        ws = tmp_path / "build-ws"
        ws.mkdir(parents=True)
        if with_dist:
            dist = ws / "frontend" / "dist"
            dist.mkdir(parents=True)
            (dist / "index.html").write_text("<html>new</html>", encoding="utf-8")
        if with_build_image:
            (ws / ".build-image").write_text("ai-evo:deadbeef99\n", encoding="utf-8")
        monkeypatch.setattr(bs, "BUILD_WS", ws)

        releases = tmp_path / "releases"
        releases.mkdir(parents=True)
        monkeypatch.setattr(bs, "RELEASES_DIR", releases)

        monkeypatch.setattr(bs, "checkout_ref", lambda branch, sha: _HEAD)
        monkeypatch.setattr(bs, "check_offline_snapshot", lambda: (True, "v"))
        monkeypatch.setattr(bs, "latest_deployed_commit", lambda: _BASELINE)

        calls = {"claude": [], "git": [], "popen": []}
        monkeypatch.setattr(bs, "ensure_claude_bin", lambda lf: calls["claude"].append(1))
        monkeypatch.setattr(bs, "ensure_git_bin", lambda lf: calls["git"].append(1))

        class FakeProc:
            def poll(self):
                return 0  # 构建脚本立即成功退出

            def kill(self):
                pass

        def fake_popen(cmd, **kwargs):
            calls["popen"].append(list(cmd))
            return FakeProc()

        monkeypatch.setattr(bs.subprocess, "Popen", fake_popen)
        return ws, releases, calls

    def test_frontend_mode_skips_claude_git_and_injects_env(self, tmp_path, monkeypatch):
        """frontend 模式：跳过 claude/git bin、注入 BUILD_FRONTEND_ONLY=1、
        不读 .build-image，release 记录 tag=frontend-<sha前8> 且 image 为空"""
        ws, releases, calls = self._patch_common(monkeypatch, tmp_path, with_dist=True)
        # classify 走真实函数：git diff 返回全 frontend 路径
        _patch_run(monkeypatch, diff_out="frontend/src/App.vue\n")

        job = _run_build_job("main", None, force_mode="auto")

        assert job["status"] == "success"
        # 前端-only 不需要应用镜像构建工具链
        assert calls["claude"] == []
        assert calls["git"] == []
        # docker run 命令注入了 BUILD_FRONTEND_ONLY=1
        assert len(calls["popen"]) == 1
        cmd = calls["popen"][0]
        assert "-e" in cmd
        assert "BUILD_FRONTEND_ONLY=1" in cmd
        # release 记录
        record = json.loads((releases / "frontend-abcdef12.json").read_text())
        assert record["mode"] == "frontend"
        assert record["tag"] == "frontend-abcdef12"
        assert re.fullmatch(r"frontend-[0-9a-f]{8}", record["tag"])
        assert record["image"] == ""
        assert record["commit"] == _HEAD

    def test_force_full_uses_claude_git_and_build_image(self, tmp_path, monkeypatch):
        """force_mode="full"：走全量分支——claude/git bin 就位、读 .build-image、
        docker run 不含 BUILD_FRONTEND_ONLY"""
        ws, releases, calls = self._patch_common(monkeypatch, tmp_path,
                                                 with_build_image=True)
        _patch_run(monkeypatch)  # 全量分支不应发起 diff（force 直接定 mode）

        job = _run_build_job("main", None, force_mode="full")

        assert job["status"] == "success"
        assert len(calls["claude"]) == 1
        assert len(calls["git"]) == 1
        cmd = calls["popen"][0]
        assert "BUILD_FRONTEND_ONLY=1" not in cmd
        record = json.loads((releases / "deadbeef99.json").read_text())
        assert record["mode"] == "full"
        assert record["image"] == "ai-evo:deadbeef99"
        assert record["tag"] == "deadbeef99"


# ======================================================================
# run_deploy_thread：frontend 快速部署
# ======================================================================

class TestRunDeployThreadFrontend:
    def test_frontend_deploy_only_syncs_frontend_and_marks(self, monkeypatch):
        """frontend 部署：只 sync_frontend + mark_deployed，
        compose/迁移/镜像清理/.env 改写/部署标记/worker 换代 全部零调用"""
        monkeypatch.setattr(bs, "validate_env_file", lambda: None)
        sync_calls = []
        monkeypatch.setattr(bs, "sync_frontend",
                            lambda job_id, lf: sync_calls.append(job_id))
        mark_calls = []
        monkeypatch.setattr(bs, "mark_deployed",
                            lambda commit: mark_calls.append(commit))
        compose_calls = _recording_compose(monkeypatch)
        mig_calls = []
        monkeypatch.setattr(bs, "run_db_migrations", lambda lf: mig_calls.append(1))
        prune_calls = []
        monkeypatch.setattr(bs, "prune_images", lambda lf: prune_calls.append(1))
        env_calls = []
        monkeypatch.setattr(bs, "update_env_image_tag", lambda tag: env_calls.append(tag))
        marker_calls = []
        monkeypatch.setattr(bs, "_write_deploy_marker",
                            lambda job_id: marker_calls.append(job_id))
        swap_calls = []
        monkeypatch.setattr(bs, "swap_worker", lambda lf: swap_calls.append(1))

        job = _run_deploy_job("frontend-abcdef12", _HEAD, mode="frontend")

        assert job["status"] == "success"
        assert job["result"]["mode"] == "frontend"
        assert job["result"]["tag"] == "frontend-abcdef12"
        assert job["result"]["commit"] == _HEAD
        assert sync_calls == [job["job_id"]]
        assert mark_calls == [_HEAD]  # 基线要推进到本次部署的 commit
        # 容器侧动作零调用
        assert compose_calls == []
        assert mig_calls == []
        assert prune_calls == []
        assert env_calls == []
        assert marker_calls == []
        assert swap_calls == []


# ======================================================================
# run_deploy_thread：全量编排接线与顺序
# ======================================================================

class TestRunDeployThreadFull:
    def test_full_orchestration_wiring_and_order(self, monkeypatch):
        """全量部署：compose up 不含 worker 且含 api；
        源码顺序 compose up < 迁移 < verify < 前端同步 < swap_worker，
        且 swap_worker 位于 _clear_deploy_marker 所在 finally 块之后"""
        monkeypatch.setattr(bs, "validate_env_file", lambda: None)
        compose_calls = _recording_compose(monkeypatch)
        mig_calls = []
        monkeypatch.setattr(bs, "run_db_migrations", lambda lf: mig_calls.append(1))
        verify_calls = []
        monkeypatch.setattr(bs, "verify_deploy_with_alert",
                            lambda lf, job_id, tag, commit="": verify_calls.append(tag))
        sync_calls = []
        monkeypatch.setattr(bs, "sync_frontend",
                            lambda job_id, lf: sync_calls.append(job_id))
        swap_calls = []

        def fake_swap(logf):
            swap_calls.append(1)
            return "fast"

        monkeypatch.setattr(bs, "swap_worker", fake_swap)
        prune_calls = []
        monkeypatch.setattr(bs, "prune_images", lambda lf: prune_calls.append(1))
        env_calls = []
        monkeypatch.setattr(bs, "update_env_image_tag", lambda tag: env_calls.append(tag))
        marker_calls = []
        monkeypatch.setattr(bs, "_write_deploy_marker",
                            lambda job_id: marker_calls.append(job_id))
        mark_calls = []
        monkeypatch.setattr(bs, "mark_deployed", lambda commit: mark_calls.append(commit))

        job = _run_deploy_job("deadbeef99", _HEAD, mode="full")

        # job 结果
        assert job["status"] == "success"
        assert job["result"]["mode"] == "full"
        assert job["result"]["worker_swap"] == "fast"
        # compose up：worker 不在其中（由 swap_worker 单独编排），api 必须在
        ups = [a for a in compose_calls if a and a[0] == "up"]
        assert len(ups) == 1
        assert "worker" not in ups[0]
        assert "api" in ups[0]
        # 全部依赖各就位一次
        assert len(mig_calls) == 1
        assert verify_calls == ["deadbeef99"]
        assert len(sync_calls) == 1
        assert len(swap_calls) == 1
        assert len(prune_calls) == 1
        assert env_calls == ["deadbeef99"]
        assert marker_calls == [job["job_id"]]
        assert mark_calls == [_HEAD]

        # 源码顺序断言（接线回归护栏）
        src = inspect.getsource(bs.run_deploy_thread)
        up_pos = src.index('compose("up"')
        mig_pos = src.index("run_db_migrations(lf)")
        verify_pos = src.index("verify_deploy_with_alert(lf")
        # sync_frontend 在 frontend 分支与全量分支各出现一次，取 verify 之后的那个
        sync_pos = src.index("sync_frontend(job_id, lf)", verify_pos)
        swap_pos = src.index("swap_worker(lf)")
        assert up_pos < mig_pos < verify_pos < sync_pos < swap_pos, \
            "全量编排顺序必须是 compose up → 迁移 → verify → 前端同步 → worker 换代"
        # swap_worker 在 marker 清理（finally 块）之后：drain 期间 cron 不介入
        finally_pos = src.index("finally:")
        clear_pos = src.index("_clear_deploy_marker()")
        assert swap_pos > clear_pos > finally_pos


# ======================================================================
# 配套静态断言（CI / compose / 构建脚本 / 配置模板）
# ======================================================================

class TestStaticConfigWiring:
    def test_gitlab_ci_deploy_wait_timeout_is_900(self):
        """deploy job 的轮询超时收紧为 900s（worker 换代已非阻塞，无需 3900s）

        仅企业 GitLab CI 线存在 .gitlab-ci.yml；GitHub 线 CI 为 Actions，
        该文件缺席时跳过本断言（compose/构建脚本两断言仍生效）。"""
        import pytest

        ci_file = REPO_ROOT / ".gitlab-ci.yml"
        if not ci_file.exists():
            pytest.skip("本仓库线无 .gitlab-ci.yml（企业 GitLab 线专属配置）")
        text = ci_file.read_text(encoding="utf-8")
        assert 'WAIT_TIMEOUT: "900"' in text

    def test_compose_worker_grace_period_is_7200s(self):
        """worker stop_grace_period 7200s：warm shutdown 窗口覆盖 celery 2h 时限"""
        import yaml

        compose = yaml.safe_load(
            (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        assert compose["services"]["worker"]["stop_grace_period"] == "7200s"

    def test_build_script_frontend_only_short_circuit(self):
        """构建脚本：BUILD_FRONTEND_ONLY=1 分支在校验 dist 后直接 exit 0，
        且位于 docker build 之前（跳过应用镜像构建）"""
        text = (REPO_ROOT / "tools" / "build-in-container.sh").read_text(encoding="utf-8")
        guard = 'if [ "$BUILD_FRONTEND_ONLY" = "1" ]; then'
        assert guard in text
        assert "exit 0" in text
        assert text.index(guard) < text.index("docker build")

    def test_config_example_has_drain_params(self):
        """config.example.toml 带 worker 优雅换代参数且为缺省值"""
        import tomllib

        cfg = tomllib.loads(
            (REPO_ROOT / "tools" / "build-service" / "config.example.toml")
            .read_text(encoding="utf-8"))
        assert cfg["deploy"]["drain_wait_sec"] == 120
        assert cfg["deploy"]["drain_force_kill_sec"] == 7500
