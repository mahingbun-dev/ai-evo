"""ARCH-3-3 CI 镜像扫描与 digest 钉扎——静态门禁（L1，无 docker/网络/DB）

设计依据：plans/2026-10-15-企业级评测系统架构设计.md §3 Phase 3 表 3-3 行——
「CI 加 trivy 镜像扫描；script-runner 镜像 digest 钉扎进配置」。

覆盖三块：
  1. GitHub CI 线：.github/workflows/ci.yml 可被 yaml 解析（语法有效性），
     image-scan job 对主镜像与 script-runner 镜像各跑一遍 trivy，
     HIGH 及以上（HIGH,CRITICAL）exit-code 1 卡门禁，且复用 docker-image
     job 的构建产物（构建一次、扫描一次，不重复 build 主镜像）；
  2. 企业 GitLab 线：build-service 的 scan_image 门禁（[scan] 配置段）——
     高危 CVE/扫描器缺位/超时一律构建失败（fail-closed），并接入
     run_build_thread（扫描不过的镜像不写 release 记录，不可部署/回滚）；
     两条产线 severity 阈值一致（单一口径，防止一松一紧）；
  3. 钉扎配置对齐（ARCH-1-5 语法闸 + ARCH-3-3 口径）：docs/build-pipeline.md
     第十一节的 SCRIPT_RUNNER_IMAGE 钉扎示例过 Settings 层文法校验
     （文档不漂移），config.example.toml 的 [scan] 段与代码默认一致。

Settings 层钉扎文法/默认值/env 覆盖的单测由 ARCH-1-5 落在
tests/unit/test_w15_container_isolation.py::TestRunnerImageDigestPinning，
此处不重复断言。
"""
import io
import os
import subprocess as sp
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB，纯静态/纯逻辑）

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BUILD_SERVICE_CFG_EXAMPLE = REPO_ROOT / "tools" / "build-service" / "config.example.toml"
BUILD_DOC_PATH = REPO_ROOT / "docs" / "build-pipeline.md"

# 两线共同口径：「HIGH 及以上」= HIGH,CRITICAL
EXPECTED_SEVERITY = {"HIGH", "CRITICAL"}

# ------------------------------------------------------------------
# build_service 导入前准备（模块级会执行 load_config()，与
# tests/unit/test_build_service.py 同一手法；本文件按字母序先导入，
# 无论谁先导入，两份测试配置都不含 [scan]，SCAN_* 均取代码默认值）
# ------------------------------------------------------------------
_TMP = Path(tempfile.mkdtemp(prefix="arch-3-3-buildsvc-"))
_WS = _TMP / "ws"
_WS.mkdir(parents=True, exist_ok=True)
(_TMP / "config.toml").write_text(
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
os.environ.setdefault("BUILD_SERVICE_CONFIG", str(_TMP / "config.toml"))

_SERVICE_DIR = REPO_ROOT / "tools" / "build-service"
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

import build_service as bs  # noqa: E402


# ======================================================================
# GitHub CI：ci.yml 语法有效 + trivy 扫描 job 结构
# ======================================================================

@pytest.fixture(scope="module")
def ci():
    return yaml.safe_load(CI_YML_PATH.read_text(encoding="utf-8"))


def _steps(jobs, job_id):
    assert job_id in jobs, f"ci.yml 缺 {job_id} job"
    return jobs[job_id].get("steps", [])


def _trivy_steps(steps):
    return [s for s in steps
            if str(s.get("uses", "")).startswith("aquasecurity/trivy-action")]


class TestCiWorkflowImageScan:
    """ci.yml 的 yaml 有效性 + ARCH-3-3 扫描门禁结构断言"""

    def test_ci_yml_is_valid_yaml(self, ci):
        """语法有效性：safe_load 成功且结构完整（job → steps → 步骤键合法）"""
        assert isinstance(ci, dict) and isinstance(ci.get("jobs"), dict) and ci["jobs"]
        for job_id, job in ci["jobs"].items():
            assert "runs-on" in job, f"{job_id} 缺 runs-on"
            for step in job.get("steps", []):
                assert any(k in step for k in ("uses", "run")), \
                    f"{job_id} 存在既无 uses 也无 run 的步骤: {step}"
        # 既有 job 不因本次改动丢失
        assert {"backend-tests", "frontend-build", "docker-image"} <= set(ci["jobs"])

    def test_image_scan_job_gates_after_docker_image(self, ci):
        """image-scan job 存在，且依赖 docker-image（构建失败就不必扫）"""
        assert "image-scan" in ci["jobs"]
        needs = ci["jobs"]["image-scan"].get("needs")
        assert "docker-image" in (needs if isinstance(needs, list) else [needs])

    def test_main_image_built_once_and_reused(self, ci):
        """主镜像构建一次：docker-image 上传 artifact，image-scan 下载 load 回来"""
        uploads = [s for s in _steps(ci["jobs"], "docker-image")
                   if str(s.get("uses", "")).startswith("actions/upload-artifact")]
        assert uploads, "docker-image job 应上传镜像 artifact 供扫描复用"
        assert uploads[0].get("with", {}).get("name") == "main-image"

        downloads = [s for s in _steps(ci["jobs"], "image-scan")
                     if str(s.get("uses", "")).startswith("actions/download-artifact")]
        assert downloads and downloads[0].get("with", {}).get("name") == "main-image"
        loads = [s for s in _steps(ci["jobs"], "image-scan")
                 if "docker load" in str(s.get("run", ""))]
        assert loads, "image-scan job 应 docker load 下载的主镜像"

    def test_script_runner_image_built_in_scan_job(self, ci):
        """script-runner 镜像（用户代码执行面）在扫描 job 内现场构建"""
        builds = [s for s in _steps(ci["jobs"], "image-scan")
                  if "docker/script-runner/Dockerfile" in str(s.get("run", ""))]
        assert builds, "image-scan job 应从 docker/script-runner/Dockerfile 构建镜像"

    def test_trivy_scans_both_images_high_severity_fails(self, ci):
        """两镜像各扫一遍：HIGH 及以上（HIGH,CRITICAL）exit-code 1 卡门禁"""
        steps = _trivy_steps(_steps(ci["jobs"], "image-scan"))
        assert len(steps) == 2, "应恰好扫描主镜像与 script-runner 两个镜像"

        refs = {s["with"]["image-ref"] for s in steps}
        assert refs == {"ai-evo:ci", "ai-evo-script-runner:ci"}, \
            f"扫描对象应为主镜像与 script-runner，实际 {refs}"

        for s in steps:
            with_ = s["with"]
            sev = set(str(with_["severity"]).replace(" ", "").split(","))
            assert sev == EXPECTED_SEVERITY, \
                f"severity 应为 HIGH 及以上（{EXPECTED_SEVERITY}），实际 {with_['severity']}"
            assert str(with_["exit-code"]) == "1", "高危漏洞必须以非零退出卡门禁"
            assert with_["ignore-unfixed"] is True, \
                "ignore-unfixed 与 build-service [scan] 同参（无修复版本由基座升级解决）"

    def test_scan_threshold_matches_gitlab_line(self, ci):
        """两线口径一致：ci.yml 的 severity 档 == build-service 代码默认档"""
        steps = _trivy_steps(_steps(ci["jobs"], "image-scan"))
        for s in steps:
            sev = set(str(s["with"]["severity"]).replace(" ", "").split(","))
            assert sev == set(bs.SCAN_SEVERITY.replace(" ", "").split(",")), \
                "GitHub CI 与企业 GitLab 线（build_service.SCAN_SEVERITY）阈值必须一致"


# ======================================================================
# build-service：scan_image 门禁（fail-closed）+ 构建线程接入
# ======================================================================

def _fake_docker(monkeypatch, *, rc=0, stdout="ai-evo:abc1234 (debian 12)\n",
                 stderr=""):
    """替身 docker run：记录命令并返回给定退出码"""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return sp.CompletedProcess(cmd, rc, stdout, stderr)

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    return calls


class TestScanImage:
    """scan_image：HIGH 及以上卡构建；扫描器缺位/超时同样失败（不放行）"""

    def test_defaults_are_fail_closed_high_and_above(self):
        """config 不含 [scan] 时的代码默认：门禁开、HIGH 及以上、预烘焙扫描器"""
        assert bs.SCAN_ENABLED is True
        assert set(bs.SCAN_SEVERITY.replace(" ", "").split(",")) == EXPECTED_SEVERITY
        assert bs.SCAN_IMAGE  # 扫描器镜像名非空

    def test_scan_pass_invokes_scanner_with_offline_gate_flags(self, monkeypatch):
        """通过路径：命令带离线扫描三件套与 docker.sock，目标镜像在末位"""
        calls = _fake_docker(monkeypatch, rc=0)
        monkeypatch.setattr(bs, "SCAN_IMAGE", "ai-evo-trivy:test")
        monkeypatch.setattr(bs, "SCAN_SEVERITY", "HIGH,CRITICAL")

        logf = io.StringIO()
        bs.scan_image("ai-evo:abc1234", logf)

        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[:2] == ["docker", "run"]
        assert "/var/run/docker.sock:/var/run/docker.sock" in cmd
        assert "ai-evo-trivy:test" in cmd
        assert "--exit-code" in cmd and cmd[cmd.index("--exit-code") + 1] == "1"
        assert "--severity" in cmd and cmd[cmd.index("--severity") + 1] == "HIGH,CRITICAL"
        # 离线三件套：不更新库、离线模式、无修复版本不卡（与 CI 同参）
        for flag in ("--ignore-unfixed", "--skip-db-update", "--offline-scan"):
            assert flag in cmd, f"缺 {flag}"
        assert cmd[-1] == "ai-evo:abc1234"
        assert "扫描通过" in logf.getvalue()

    def test_high_cve_fails_the_gate(self, monkeypatch):
        """trivy 退出码 1（存在 HIGH,CRITICAL 级有修复版本漏洞）→ 构建失败"""
        _fake_docker(monkeypatch, rc=1, stdout="TOTAL: 3 (HIGH: 2, CRITICAL: 1)\n")

        logf = io.StringIO()
        with pytest.raises(RuntimeError, match="镜像扫描门禁未通过"):
            bs.scan_image("ai-evo:abc1234", logf)
        # 扫描器表格式输出必须进任务日志（定位是哪个 CVE 卡的）
        assert "HIGH: 2" in logf.getvalue()

    def test_missing_scanner_image_also_fails(self, monkeypatch):
        """扫描器镜像未预载（docker run 报 127/125）→ 同样失败，不放行"""
        _fake_docker(monkeypatch, rc=127, stderr="docker: Error: no such image")

        with pytest.raises(RuntimeError, match="镜像扫描门禁未通过"):
            bs.scan_image("ai-evo:abc1234", io.StringIO())

    def test_timeout_fails_with_actionable_message(self, monkeypatch):
        """扫描超时 → 明确报错（带预载指引），不裸抛 TimeoutExpired"""
        def slow_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, kwargs.get("timeout", 900))

        monkeypatch.setattr(bs.subprocess, "run", slow_run)
        monkeypatch.setattr(bs, "SCAN_TIMEOUT", 900)
        monkeypatch.setattr(bs, "SCAN_IMAGE", "ai-evo-trivy:latest")

        with pytest.raises(RuntimeError, match="镜像扫描超时"):
            bs.scan_image("ai-evo:abc1234", io.StringIO())

    def test_explicitly_disabled_skips_without_docker_call(self, monkeypatch):
        """enabled=false 仅记录跳过（过渡期开关），不产生 docker 调用"""
        calls = _fake_docker(monkeypatch)
        monkeypatch.setattr(bs, "SCAN_ENABLED", False)

        logf = io.StringIO()
        bs.scan_image("ai-evo:abc1234", logf)  # 不抛异常即通过

        assert calls == []
        assert "enabled=false" in logf.getvalue()


class TestScanGateInBuildThread:
    """run_build_thread 接线：扫描不过 → 任务 failed 且不写 release 记录"""

    def _wire(self, tmp_path, monkeypatch, scan_impl):
        ws = tmp_path / "build-ws"
        ws.mkdir()
        (ws / ".build-image").write_text("ai-evo:abc1234", encoding="utf-8")
        releases = tmp_path / "releases"
        releases.mkdir()

        monkeypatch.setattr(bs, "BUILD_WS", ws)
        monkeypatch.setattr(bs, "RELEASES_DIR", releases)
        monkeypatch.setattr(bs, "CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(bs, "checkout_ref", lambda branch, sha: "full-sha-abc")
        monkeypatch.setattr(bs, "check_offline_snapshot", lambda: (True, "v1"))
        monkeypatch.setattr(bs, "ensure_claude_bin", lambda lf: None)
        monkeypatch.setattr(bs, "ensure_git_bin", lambda lf: None)
        monkeypatch.setattr(bs, "prune_releases", lambda: None)
        monkeypatch.setattr(bs, "scan_image", scan_impl)

        class _Done:
            def poll(self):
                return 0

        monkeypatch.setattr(bs.subprocess, "Popen", lambda *a, **k: _Done())
        return releases

    def test_build_succeeds_after_scan_passes(self, tmp_path, monkeypatch):
        """扫描通过 → 任务 success，扫描拿到的是刚构建出的镜像引用"""
        seen = []
        self._wire(tmp_path, monkeypatch,
                   lambda image_ref, logf: seen.append(image_ref))

        job = bs.new_job("build", {})
        assert bs.BUSY_LOCK.acquire(blocking=False)
        bs.run_build_thread(job["job_id"], "main", None)

        assert seen == ["ai-evo:abc1234"]
        assert bs.JOBS[job["job_id"]]["status"] == "success"
        assert (tmp_path / "releases" / "abc1234.json").exists()

    def test_scan_failure_blocks_release_record(self, tmp_path, monkeypatch):
        """高危 CVE 卡门禁：任务 failed，且不写 release 记录（不可部署/回滚）"""
        def gate(image_ref, logf):
            raise RuntimeError("镜像扫描门禁未通过（退出码 1）: ai-evo:abc1234")

        releases = self._wire(tmp_path, monkeypatch, gate)

        job = bs.new_job("build", {})
        assert bs.BUSY_LOCK.acquire(blocking=False)
        bs.run_build_thread(job["job_id"], "main", None)

        record = bs.JOBS[job["job_id"]]
        assert record["status"] == "failed"
        assert "镜像扫描门禁未通过" in record["message"]
        assert list(releases.glob("*.json")) == [], "有漏洞的镜像不得进入可回滚列表"


# ======================================================================
# 配置模板 / 文档接线（ARCH-3-3 接入点文档化 + 钉扎口径不漂移）
# ======================================================================

class TestScanAndPinningDocs:
    def test_config_example_scan_section_matches_code_defaults(self):
        """config.example.toml 的 [scan] 与代码默认一致（运维照抄即得门禁口径）"""
        cfg = tomllib.loads(BUILD_SERVICE_CFG_EXAMPLE.read_text(encoding="utf-8"))
        scan = cfg.get("scan", {})
        assert scan.get("enabled") is True
        assert set(str(scan.get("severity", "")).replace(" ", "").split(",")) \
            == EXPECTED_SEVERITY
        assert scan.get("image"), "应给出预烘焙扫描器镜像名"
        # 模板缺省 == 代码缺省（两处口径漂移会在这里红）
        assert scan["severity"] == bs.SCAN_SEVERITY

    def test_docs_document_both_scan_entrypoints_and_drill(self):
        """docs/build-pipeline.md 第十一节：两线接入点 + 扫描器预载 + 演练"""
        text = BUILD_DOC_PATH.read_text(encoding="utf-8")
        for needle in (
            "image-scan",            # GitHub CI 线接入点
            "scan_image",            # GitLab 线接入点（build-service 构建钩子）
            "ai-evo-trivy",          # 离线扫描器镜像预载
            "[scan]",                # 配置段
            "门禁演练",               # 验收口径：高危 CVE 卡门禁演练
            "--ignore-unfixed",      # 两线同参的取舍说明
        ):
            assert needle in text, f"docs/build-pipeline.md 缺 {needle}"

    def test_docs_pinning_example_matches_settings_grammar(self):
        """11.4 钉扎示例（占位符换成真实 64hex 后）必须过 Settings 层文法闸

        文法单一事实源是 app/core/config.validate_runner_image_ref
        （ARCH-1-5 落地）；文档示例若与文法脱节（如 digest 写成短哈希），
        运维照抄会直接被启动校验拒绝——在这里提前红。
        """
        from app.core.config import validate_runner_image_ref

        text = BUILD_DOC_PATH.read_text(encoding="utf-8")
        # 文档里的钉扎示例形态（registry 前缀 + @sha256 占位）
        prefix = "harbor.internal:5000/ai-evo/script-runner@sha256:"
        assert f"SCRIPT_RUNNER_IMAGE={prefix}" in text, \
            "docs 11.4 应给出 registry 前缀的 digest 钉扎示例"

        pinned = prefix + "0123456789abcdef" * 4  # 占位符 → 真实 64 位十六进制
        assert validate_runner_image_ref(pinned) == pinned
        # 缺省浮动引用（文档同段给出的另一形态）同样合法
        assert validate_runner_image_ref("ai-evo-script-runner:latest") \
            == "ai-evo-script-runner:latest"
