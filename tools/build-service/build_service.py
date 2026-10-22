#!/usr/bin/env python3
"""
ai-evo 构建服务（build-service）

模块名刻意不叫 app.py：仓库根目录已经有一个 app/ 包（主应用），
同名会与之冲突——在全量跑测试时 `import app` 拿到的是主应用包而非本模块，
导致测试以难以察觉的方式失效。

常驻容器运行在部署机上（默认 :8180），职责：
  1. build   ：在 ai-evo-builder 容器内离线构建前端 dist 与应用镜像
     （变更相对线上已部署 commit 全部落在 frontend/ 内时只构建 dist）
  2. deploy  ：同步 docker-compose.yml，更新 .env 的 IMAGE_TAG，compose up
     （worker 除外，由 swap_worker 单独非阻塞换代）并校验；
     前端-only 模式只同步 nginx 静态根，不动容器
  3. rollback：回退到某个历史镜像 tag（不重新构建）
  4. jobs    ：异步任务状态与日志查询（GitLab CI 轮询用）

鉴权：除 /api/v1/health 外全部要求 X-Build-Token 请求头。
并发：同一时刻只允许一个 build 或 deploy 任务（否则 409）。
配置：/app/config.toml（见 config.example.toml）。

为什么需要这个服务：部署机是 CentOS 7 且不能下载任何东西，GitLab runner
只负责调用本服务；真正的构建在一个预烘焙好工具链的容器里完成，依赖全部
来自离线快照。CI 侧只做 curl + 轮询，与本服务的实现细节解耦。
"""
# norm-exempt: attach-truncation —— 下方 [:N] 是部署日志/子进程错误消息限长，不是 allure 证据附件
# norm-exempt: keyword-reuse —— 本模块是部署服务，不涉及用例 UI 操作与知识库关键字

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import tomllib
import urllib.request
from urllib.parse import urlparse

def ensure_http_url(url: str) -> str:
    """校验外呼 URL 仅允许 http/https scheme（阻断 file:/ftp: 等变形）"""
    scheme = urlparse(str(url or "")).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"仅允许 http/https URL，收到: {url!r}")
    return url

import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

# ------------------------------------------------------------------
# 配置加载
# ------------------------------------------------------------------
CONFIG_PATH = Path(os.environ.get("BUILD_SERVICE_CONFIG", "/app/config.toml"))


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"配置文件不存在: {CONFIG_PATH}")
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


CFG = load_config()

TOKEN = CFG["auth"]["token"]

# 生产工作区：仅用于读取 git remote，不作为构建目录
WORKSPACE = Path(CFG["paths"]["workspace"])
# 隔离的构建工作区：checkout / clean 都发生在这里，
# 避免污染生产工作区（其中有运行中的 compose 与 .env）
BUILD_WS = Path(CFG["paths"].get("build_workspace", str(WORKSPACE) + "-build"))
# compose 实际运行目录（部署时把仓库里的 compose 同步过来）
DEPLOY_DIR = Path(CFG["paths"].get("deploy_dir", str(WORKSPACE)))
DATA_DIR = Path(CFG["paths"].get("data_dir", str(WORKSPACE) + "/build-service/data"))

BUILDER_IMAGE = CFG.get("builder", {}).get("image", "ai-evo-builder:latest")
IMAGE_NAME = CFG.get("builder", {}).get("image_name", "ai-evo")
CACHE_DIR = Path(CFG.get("builder", {}).get("cache_dir", str(WORKSPACE) + "-build-cache"))

ENV_FILE = DEPLOY_DIR / CFG.get("deploy", {}).get("env_file", ".env")
COMPOSE_FILE = DEPLOY_DIR / CFG.get("deploy", {}).get("compose_file", "docker-compose.yml")
# 部署期间的存在标记：部署机上有 cron 每 5 分钟跑 ensure_worker.sh，发现
# ai-evo-worker 不在运行就 `docker compose up -d worker`。而部署时 worker 正是
# 要优雅退出（最长 7200s，与 celery task_time_limit 对齐，W10-3），容器处于
# Stopping、不出现在 `docker ps` 里——cron 会误判为"挂了"并介入，与外层的
# compose 撞车。部署期间写这个标记，crontab 侧用 `[ -f <标记> ] ||` 跳过自愈。
DEPLOY_MARKER = DEPLOY_DIR / ".deploy-in-progress"
DEPLOY_SERVICES = CFG.get("deploy", {}).get("services", ["api", "worker"])
# W13：探活 URL 由 /api/v1/auth/status（恒 200、不查依赖，形同虚设）切换为
# /api/v1/health/deep——后者对 DB/Redis/迁移版本/uploads 做真实探测，
# 依赖异常时返回 503，verify 才能真正拦住「部署成功但服务不可用」。
HEALTH_URL = CFG.get("deploy", {}).get("health_url", "http://127.0.0.1:8100/api/v1/health/deep")
# nginx 的前端静态根（宿主机路径）。首次部署实测踩到：用户访问的站点由 nginx
# 直接托管这个目录，而不是容器里 FastAPI 托管的那份 dist——只更新容器内的
# dist，用户看到的仍是旧前端。为空则跳过同步。
FRONTEND_ROOT = CFG.get("deploy", {}).get("frontend_root", "")
FRONTEND_BACKUPS = int(CFG.get("deploy", {}).get("keep_frontend_backups", 3))
KEEP_RELEASES = int(CFG.get("deploy", {}).get("keep_releases", 5))
# worker 优雅换代参数（swap_worker，见对应函数说明）：
#   DRAIN_WAIT_SEC        流水线内等旧 worker 退出的短窗口，超时转入后台 watcher
#   DRAIN_FORCE_KILL_SEC  watcher 强制回收上限（2h celery task_time_limit + 缓冲）
DRAIN_WAIT_SEC = int(CFG.get("deploy", {}).get("drain_wait_sec", 120))
DRAIN_FORCE_KILL_SEC = int(CFG.get("deploy", {}).get("drain_force_kill_sec", 7500))
# worker 容器名（docker-compose.yml 的 container_name，与 ensure_worker.sh 同口径）
WORKER_CONTAINER = "ai-evo-worker"
# 「线上已部署 commit」记录：部署成功（含前端-only 与回滚）后写入，作为下次构建
# 「前端-only 判定」的 diff 基线。不复用 releases 记录——release 记录在 build
# 成功时就写入，若那次部署失败，基线会虚高、后续判定会漏掉未部署的后端变更。
DEPLOYED_REF = DATA_DIR / "deployed_commit"
# 前端-only 判定路径前缀：相对已部署基线的变更全部落在该前缀内时，
# 跳过应用镜像构建与容器重建，仅同步前端静态资源
FRONTEND_PREFIX = "frontend/"

BUILD_TIMEOUT = int(CFG.get("timeouts", {}).get("build_sec", 3600))
DEPLOY_TIMEOUT = int(CFG.get("timeouts", {}).get("deploy_sec", 1800))
# 迁移步骤超时：常规增量迁移秒级，给 5 分钟余量覆盖大表 ALTER 等慢迁移
MIGRATE_TIMEOUT = int(CFG.get("timeouts", {}).get("migrate_sec", 300))

# 构建后镜像漏洞扫描门禁（ARCH-3-3，设计 §3 Phase 3 表 3-3 行：CI 加 trivy
# 镜像扫描——本服务是企业 GitLab 线的等价构建钩子，GitLab runner 只 curl
# 触发，扫描必须发生在构建产物落 daemon 之后、写 release 记录之前）。
#
# 离线约束：部署机不能下载任何东西，扫描器是**预烘焙**镜像（aquasec/trivy
# + 离线漏洞库，构建/预载步骤见 docs/build-pipeline.md 第十一节），扫描时
# 挂宿主 docker.sock 读取刚构建的镜像，--skip-db-update 不联网。
#
# fail-closed（与 AUTH_JWT_SECRET / EXECUTOR_TOKEN 同整改纪律）：门禁默认
# 开启，扫描器镜像缺失/扫描超时/高危 CVE（HIGH 及以上）一律构建失败——
# 有漏洞的镜像不进 releases 记录，也就无法被部署或回滚。上线新版
# build-service 镜像前必须先预载扫描器镜像（或显式 enabled=false 过渡）。
SCAN_ENABLED = bool(CFG.get("scan", {}).get("enabled", True))
SCAN_IMAGE = CFG.get("scan", {}).get("image", "ai-evo-trivy:latest")
SCAN_SEVERITY = CFG.get("scan", {}).get("severity", "HIGH,CRITICAL")
SCAN_TIMEOUT = int(CFG.get("scan", {}).get("timeout_sec", 900))

GIT_REMOTE = CFG.get("gitlab", {}).get("repo_url", "origin")

GL_HOST = CFG.get("gitlab", {}).get("host", "https://gitlab.example.com")
GL_PROJECT = str(CFG.get("gitlab", {}).get("project_id", ""))
GL_DT_USER = CFG.get("gitlab", {}).get("deploy_token_user", "")
GL_DT_PASS = CFG.get("gitlab", {}).get("deploy_token_pass", "")
GL_SNAPSHOT_PKG = CFG.get("gitlab", {}).get("snapshot_package", "ai-evo-offline-snapshot")
GL_CLAUDE_PKG = CFG.get("gitlab", {}).get("claude_package", "ai-evo-claude-bin")
GL_GIT_PKG = CFG.get("gitlab", {}).get("git_package", "ai-evo-git-bin")

LOGS_DIR = DATA_DIR / "logs"
RELEASES_DIR = DATA_DIR / "releases"
for d in (LOGS_DIR, RELEASES_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ai-evo-build-service")

app = FastAPI(title="ai-evo-build-service", docs_url=None, redoc_url=None)


# ------------------------------------------------------------------
# 告警（W13）
#
# 与主应用 app/services/alert_service.py 行为对齐（同一 JSON 契约、
# 同一 ALERT_WEBHOOK_URL 环境变量、落 alerts.log 兜底）；但 build-service
# 是独立容器（Dockerfile 只 COPY 本文件，不装主应用包），不能 import
# 主应用代码，故用标准库在此实现同一契约——两边字段保持一致：
#   {"level", "title", "details", "source", "timestamp"}
#
# 离线环境暂无 IM 出口时，告警先落 LOGS_DIR/alerts.log；接入 IM 机器人后
# 只需给容器加 -e ALERT_WEBHOOK_URL=... 即生效，无需改代码（升级路径见
# docs/alerts-and-health.md）。
# ------------------------------------------------------------------
ALERT_LOG_PATH = LOGS_DIR / "alerts.log"


def _alert_webhook_url() -> str:
    """每次调用时读环境变量，便于运维改动与测试注入"""
    return os.environ.get("ALERT_WEBHOOK_URL", "").strip()


def send_alert(level: str, title: str, details: dict, source: str = "build-service") -> bool:
    """发一条告警：webhook（若配置）+ alerts.log 兜底；永不抛异常"""
    payload = {
        "level": level,
        "title": title,
        "details": details,
        "source": source,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    delivered = False
    url = _alert_webhook_url()
    if url:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                delivered = 200 <= resp.status < 300
                if not delivered:
                    log.warning("告警 webhook 返回非 2xx: %s %s", resp.status, url)
        except Exception as e:  # noqa: BLE001 - 告警失败绝不影响部署主流程
            log.warning("告警 webhook 发送失败 (%s): %s", url, e)
    try:
        ALERT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = dict(payload)
        record["webhook_delivered"] = delivered
        with open(ALERT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        log.error("告警落盘 %s 失败: %s", ALERT_LOG_PATH, e)
    return delivered

# ------------------------------------------------------------------
# 任务状态管理
# ------------------------------------------------------------------
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
BUSY_LOCK = threading.Lock()  # 全局互斥：同时只允许一个 build 或 deploy


def new_job(kind: str, params: dict) -> dict:
    job_id = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    job = {
        "job_id": job_id, "kind": kind, "status": "pending", "progress": 0,
        "stage": "queued", "message": "排队中", "params": params,
        "created_at": time.time(), "finished_at": None, "result": None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    return job


def set_job(job_id: str, **kw):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(kw)


def job_log_path(job_id: str) -> Path:
    return LOGS_DIR / f"{job_id}.log"


def log_tail(job_id: str, lines: int = 80) -> str:
    p = job_log_path(job_id)
    if not p.exists():
        return ""
    return "".join(p.read_text(errors="replace").splitlines(keepends=True)[-lines:])


def check_token(x_build_token: Optional[str]):
    if not TOKEN:
        raise HTTPException(500, "服务端未配置 token，拒绝访问")
    if x_build_token != TOKEN:
        raise HTTPException(401, "无效的 X-Build-Token")


# ------------------------------------------------------------------
# git 操作（隔离构建工作区）
# ------------------------------------------------------------------
def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(BUILD_WS), *args],
                       capture_output=True, text=True, timeout=600)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.strip()[:500]}")
    return r.stdout.strip()


def ensure_build_ws() -> str:
    """确保隔离构建工作区存在，返回用于 fetch 的 remote 名

    统一返回 "origin" 这个名字，**不要**把 repo_url 直接当 remote 用：
    `git fetch <url> <branch>` 不会更新任何 remote-tracking 引用（只写 FETCH_HEAD），
    于是下面按 `<remote>/<branch>` 判断「提交是否在该分支历史里」必然失败——
    表现为第二次及之后的构建都报「commit xxx 不在 ... 历史中」。
    首次构建能过，是因为它走全新 clone 分支时恰好返回的是 "origin"。
    """
    if not (BUILD_WS / ".git").exists():
        BUILD_WS.parent.mkdir(parents=True, exist_ok=True)
        url = GIT_REMOTE
        if url == "origin":
            url = subprocess.run(
                ["git", "-C", str(WORKSPACE), "remote", "get-url", "origin"],
                capture_output=True, text=True,
            ).stdout.strip()
        if not url:
            raise RuntimeError("无法确定 git remote，请在 config.toml 配置 [gitlab].repo_url")
        log.info("首次初始化构建工作区: clone -> %s", BUILD_WS)
        r = subprocess.run(["git", "clone", "--no-checkout", url, str(BUILD_WS)],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f"clone 构建工作区失败: {r.stderr.strip()[:300]}")
        return "origin"

    # 已存在：确保 origin 指向配置里的仓库（改了 repo_url 也能生效）
    if GIT_REMOTE != "origin":
        cur = subprocess.run(["git", "-C", str(BUILD_WS), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        if cur != GIT_REMOTE:
            log.info("构建工作区 origin 地址与配置不符，更新为配置值")
            git("remote", "set-url", "origin", GIT_REMOTE)
    return "origin"


def checkout_ref(branch: str, sha: Optional[str]) -> str:
    """在隔离构建工作区 fetch 并 checkout 到目标提交"""
    remote = ensure_build_ws()

    # 必须用 `git fetch <remote>`，**不能**写 `git fetch <remote> <branch>`：
    # 后者是"没有目标端的 refspec"，只会写 FETCH_HEAD，不会更新
    # refs/remotes/<remote>/<branch>。随后按 <remote>/<branch> 做
    # `merge-base --is-ancestor` 时读到的是过期的引用，就会误报
    # 「commit xxx 不在 ... 历史中」——表现为第二次及之后的构建必失败。
    # 这一点在服务器上实测确认过（带分支名的 fetch 前后 origin/main 没变）。
    log.info("git fetch --prune %s", remote)
    git("fetch", "--prune", remote)

    if sha:
        rc = subprocess.run(
            ["git", "-C", str(BUILD_WS), "merge-base", "--is-ancestor",
             sha, f"{remote}/{branch}"]
        ).returncode
        if rc != 0:
            raise RuntimeError(
                f"commit {sha} 不在 {remote}/{branch} 历史中"
                f"（当前 {remote}/{branch} = {git('rev-parse', '--short', f'{remote}/{branch}')}）"
            )
        target = sha
    else:
        target = f"{remote}/{branch}"

    git("checkout", "--detach", "--force", target)
    # 清理未跟踪文件，但保留离线快照目录（否则每次构建都要重新同步数百 MB）。
    # .snapshot-version 记录工作区当前快照的版本号，同样要保留——否则标记被
    # 清掉后 check 每次都误判未同步，退化为每次构建重新下载数百 MB。
    # claude-local/ 与 .claude-bin-version 同理（离线 claude CLI，见 ensure_claude_bin）。
    git("clean", "-fdx",
        "--exclude=.wheels", "--exclude=.pnpm-store", "--exclude=.build-progress.json",
        "--exclude=.snapshot-version",
        "--exclude=claude-local", "--exclude=.claude-bin-version",
        "--exclude=git-local", "--exclude=.git-bin-version")
    head = git("rev-parse", "HEAD")
    log.info("构建工作区已检出: %s", head)
    return head


# ------------------------------------------------------------------
# 离线快照：校验与同步
# ------------------------------------------------------------------
def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def check_offline_snapshot() -> tuple[bool, str]:
    """返回 (快照是否就绪, 期望的快照版本号)

    版本号 = .offline-snapshot.json 内容哈希前 12 位（与 upload 脚本一致）。
    同时校验锁文件哈希是否与快照记录的一致——不一致说明改了依赖但没重跑
    prepare-offline，必须拒绝构建而不是静默用旧依赖。

    还要校验工作区快照版本标记（.snapshot-version，由 sync_offline_snapshot
    写入）：clean 保留了 .wheels/.pnpm-store 目录，锁文件哈希换成新值后目录
    依然"存在"，但内容可能是上一次同步的旧快照——只看目录存在会误判就绪、
    跳过同步，builder 里 offline 安装缺包（2026-10-09 main 构建实测踩坑）。
    """
    meta_file = BUILD_WS / ".offline-snapshot.json"
    if not meta_file.exists():
        return False, ""
    meta_bytes = meta_file.read_bytes()
    version = hashlib.sha256(meta_bytes).hexdigest()[:12]

    meta = json.loads(meta_bytes)
    pairs = [
        ("requirements_lock_hash", BUILD_WS / "requirements.lock"),
        ("pnpm_lock_hash", BUILD_WS / "frontend" / "pnpm-lock.yaml"),
    ]
    for key, path in pairs:
        if key in meta and path.exists() and meta[key] != _sha256_file(path):
            return False, version

    stamp = BUILD_WS / ".snapshot-version"
    if not (stamp.exists() and stamp.read_text().strip() == version):
        return False, version

    if (BUILD_WS / ".wheels").is_dir() and (BUILD_WS / "frontend" / ".pnpm-store").is_dir():
        return True, version
    return False, version


def sync_offline_snapshot(version: str, logf) -> None:
    """从 GitLab Packages 下载并解压快照到构建工作区

    分片 100M，下载后拼接 → sha256 校验 → 解包（覆盖 .wheels 与 .pnpm-store）
    """
    base = (f"{GL_HOST}/api/v4/projects/{GL_PROJECT}"
            f"/packages/generic/{GL_SNAPSHOT_PKG}/{version}")
    auth = base64.b64encode(f"{GL_DT_USER}:{GL_DT_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}

    def fetch(name: str, dest: Path):
        ensure_http_url(f"{base}/{name}")
        req = urllib.request.Request(f"{base}/{name}", headers=headers)
        with urllib.request.urlopen(req, timeout=1800) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 22)
                if not chunk:
                    break
                f.write(chunk)

    work = Path("/tmp/snapshot-sync-" + version)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    logf.write(f"[snapshot] 下载 manifest (version={version})\n")
    fetch("manifest.json", work / "manifest.json")
    manifest = json.loads((work / "manifest.json").read_text())

    for part in manifest["parts"]:
        logf.write(f"[snapshot] 下载 {part}\n")
        fetch(part, work / part)

    tarball = work / "snapshot.tar.gz"
    with open(tarball, "wb") as out:
        for part in manifest["parts"]:
            with open(work / part, "rb") as f:
                shutil.copyfileobj(f, out)

    digest = _sha256_file(tarball)
    if digest != manifest["sha256"]:
        raise RuntimeError(f"快照校验失败 sha256 {digest} != {manifest['sha256']}")
    logf.write("[snapshot] sha256 校验通过，解压中...\n")

    with tarfile.open(tarball, "r:gz") as tar:
        for m in tar:
            tar.extract(m, BUILD_WS, filter="data")
    shutil.rmtree(work, ignore_errors=True)
    # 写快照版本标记：下次构建 check_offline_snapshot 靠它判断工作区里的
    # store/wheels 是不是本次期望的版本（目录被 clean exclude 保留，内容会残留）
    (BUILD_WS / ".snapshot-version").write_text(version + "\n", encoding="utf-8")
    logf.write("[snapshot] 同步完成\n")


def ensure_claude_bin(logf) -> None:
    """确保工作区里有离线 claude CLI（构建应用镜像的 COPY 输入）

    部署机无外网，Dockerfile 里旧的「apt install curl + 在线跑
    claude.ai/install.sh」必然失败（2026-10-09 main 构建实测踩坑）。
    改为：开发机在 linux/amd64 容器内预装 claude 打包上传 GitLab Packages，
    版本与 sha256 记录在仓库 tools/claude-bin.json；本函数在构建前把
    claude-local/ 就位（工作区标记 .claude-bin-version 判断是否需要下载）。

    升级 claude：开发机重跑 tools/build-claude-bin.sh → 上传 → 更新
    claude-bin.json 提交，服务器构建时自动下载新版本。
    """
    spec_file = BUILD_WS / "tools" / "claude-bin.json"
    if not spec_file.exists():
        raise RuntimeError(f"缺少 {spec_file}（claude 离线安装清单），无法构建应用镜像")
    spec = json.loads(spec_file.read_text())
    version = str(spec.get("version", "")).strip()
    sha256_expected = str(spec.get("sha256", "")).strip()
    if not version or not sha256_expected:
        raise RuntimeError("tools/claude-bin.json 缺少 version 或 sha256 字段")

    stamp = BUILD_WS / ".claude-bin-version"
    bin_path = BUILD_WS / "claude-local" / "bin" / "claude"
    if stamp.exists() and stamp.read_text().strip() == version and bin_path.exists():
        logf.write(f"[claude] 已就位（{spec.get('claude_version', version)}），跳过下载\n")
        return

    base = (f"{GL_HOST}/api/v4/projects/{GL_PROJECT}"
            f"/packages/generic/{GL_CLAUDE_PKG}/{version}")
    auth = base64.b64encode(f"{GL_DT_USER}:{GL_DT_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}

    work = Path("/tmp/claude-bin-" + version)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    tarball = work / "claude-bin.tar.gz"

    logf.write(f"[claude] 下载 claude-bin.tar.gz (version={version})\n")
    ensure_http_url(f"{base}/claude-bin.tar.gz")
    req = urllib.request.Request(f"{base}/claude-bin.tar.gz", headers=headers)
    with urllib.request.urlopen(req, timeout=1800) as r, open(tarball, "wb") as f:
        while True:
            chunk = r.read(1 << 22)
            if not chunk:
                break
            f.write(chunk)

    digest = _sha256_file(tarball)
    if digest != sha256_expected:
        raise RuntimeError(f"claude-bin 校验失败 sha256 {digest} != {sha256_expected}")
    logf.write("[claude] sha256 校验通过，解压中...\n")
    with tarfile.open(tarball, "r:gz") as tar:
        for m in tar:
            tar.extract(m, BUILD_WS, filter="data")
    shutil.rmtree(work, ignore_errors=True)

    if not bin_path.exists():
        raise RuntimeError("claude-bin.tar.gz 解压后缺少 claude-local/bin/claude")
    stamp.write_text(version + "\n", encoding="utf-8")
    logf.write(f"[claude] 就位完成（claude {spec.get('claude_version', '')}）\n")


def ensure_git_bin(logf) -> None:
    """确保工作区里有离线 git 运行时（插件市场 git 克隆的 COPY 输入）

    与 ensure_claude_bin 同款机制：运行镜像基础 python:3.12-slim 不含 git，
    构建机（部署机）无外网 apt 源。开发机跑 tools/prepare-git-bin.sh 生成
    git-bin.tar.gz（git-local/ 树）上传 GitLab Packages，版本与 sha256 记录
    在仓库 tools/git-bin.json；本函数在构建前把 git-local/ 就位（工作区标记
    .git-bin-version 判断是否需要下载）。

    升级 git：开发机重跑 tools/prepare-git-bin.sh → 上传 → 更新 git-bin.json
    提交，服务器构建时自动下载新版本。
    """
    spec_file = BUILD_WS / "tools" / "git-bin.json"
    if not spec_file.exists():
        raise RuntimeError(f"缺少 {spec_file}（git 离线安装清单），无法构建应用镜像")
    spec = json.loads(spec_file.read_text())
    version = str(spec.get("version", "")).strip()
    sha256_expected = str(spec.get("sha256", "")).strip()
    if not version or not sha256_expected:
        raise RuntimeError("tools/git-bin.json 缺少 version 或 sha256 字段")

    stamp = BUILD_WS / ".git-bin-version"
    bin_path = BUILD_WS / "git-local" / "bin" / "git"
    if stamp.exists() and stamp.read_text().strip() == version and bin_path.exists():
        logf.write(f"[git] 已就位（git {spec.get('git_version', version)}），跳过下载\n")
        return

    base = (f"{GL_HOST}/api/v4/projects/{GL_PROJECT}"
            f"/packages/generic/{GL_GIT_PKG}/{version}")
    auth = base64.b64encode(f"{GL_DT_USER}:{GL_DT_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}

    work = Path("/tmp/git-bin-" + version)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    tarball = work / "git-bin.tar.gz"

    logf.write(f"[git] 下载 git-bin.tar.gz (version={version})\n")
    req = urllib.request.Request(f"{base}/git-bin.tar.gz", headers=headers)
    with urllib.request.urlopen(req, timeout=1800) as r, open(tarball, "wb") as f:
        while True:
            chunk = r.read(1 << 22)
            if not chunk:
                break
            f.write(chunk)

    digest = _sha256_file(tarball)
    if digest != sha256_expected:
        raise RuntimeError(f"git-bin 校验失败 sha256 {digest} != {sha256_expected}")
    logf.write("[git] sha256 校验通过，解压中...\n")
    with tarfile.open(tarball, "r:gz") as tar:
        for m in tar:
            tar.extract(m, BUILD_WS, filter="data")
    shutil.rmtree(work, ignore_errors=True)

    if not bin_path.exists():
        raise RuntimeError("git-bin.tar.gz 解压后缺少 git-local/bin/git")
    stamp.write_text(version + "\n", encoding="utf-8")
    logf.write(f"[git] 就位完成（git {spec.get('git_version', '')}）\n")


# ------------------------------------------------------------------
# build
# ------------------------------------------------------------------
def _publish_progress(job_id: str, board_file: Path):
    """读取 builder 容器写的进度 sidecar 并同步到 job 状态"""
    if not board_file.exists():
        return
    try:
        sidecar = json.loads(board_file.read_text())
    except (OSError, json.JSONDecodeError):
        return
    set_job(job_id,
            stage=str(sidecar.get("stage") or "build"),
            progress=float(sidecar.get("percent") or 0),
            message=str(sidecar.get("detail") or ""))


def scan_image(image_ref: str, logf) -> None:
    """构建后镜像漏洞扫描门禁（ARCH-3-3）：HIGH 及以上 CVE 卡构建

    在预烘焙扫描器容器（SCAN_IMAGE，trivy + 离线漏洞库）里扫描刚构建出的
    镜像：挂宿主 docker.sock 让 trivy 直接读 daemon 里的本地镜像（trivy
    官方离线扫描形态）；--skip-db-update + --offline-scan 保证不外呼。

    结果语义（fail-closed，验收口径「高危 CVE 卡门禁演练触发一次」）：
    - 退出码 0            ：无门槛级漏洞，放行；
    - 退出码 1            ：存在 HIGH,CRITICAL 级（且有修复版本的）漏洞，
                            抛 RuntimeError → 构建任务 failed，不写 release
                            记录（有漏洞的镜像不可部署、不可回滚）；
    - 扫描器镜像缺失/崩溃 ：docker run 非零退出，同样失败——门禁不在扫描器
                            缺位时静默放行；
    - 超时                ：转明确报错（subprocess.TimeoutExpired 不裸抛，
                            任务 message 里给出处理指引）。
    ignore-unfixed：无修复版本的基座 CVE（Debian slim 常态）由镜像基座升级
    解决，不永久卡门禁；有修复的高危 CVE 仍卡——GitHub CI 的 trivy job
    （.github/workflows/ci.yml image-scan）同一口径，两线阈值一致。
    """
    if not SCAN_ENABLED:
        logf.write("[scan] [scan].enabled=false，镜像扫描门禁已关闭（高危 CVE 不再卡构建）\n")
        return
    cmd = [
        "docker", "run", "--rm",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        SCAN_IMAGE, "image",
        "--exit-code", "1",
        "--severity", SCAN_SEVERITY,
        "--ignore-unfixed",
        "--skip-db-update",
        "--offline-scan",
        image_ref,
    ]
    logf.write(f"[scan] $ {' '.join(cmd)}\n")
    logf.flush()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SCAN_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"镜像扫描超时（>{SCAN_TIMEOUT}s）：{image_ref}。检查扫描器镜像 "
            f"{SCAN_IMAGE} 是否正常预载（docs/build-pipeline.md 第十一节），"
            "或按需调大 [scan].timeout_sec"
        ) from e
    if proc.stdout:
        logf.write(proc.stdout)
    if proc.stderr:
        logf.write(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(
            f"镜像扫描门禁未通过（退出码 {proc.returncode}）：{image_ref} 存在 "
            f"{SCAN_SEVERITY} 级（有修复版本的）漏洞，或扫描器镜像 "
            f"{SCAN_IMAGE} 不可用。修复依赖/基座后重新构建；扫描器问题见 "
            "docs/build-pipeline.md 第十一节的预载步骤"
        )
    logf.write(f"[scan] 扫描通过，无 {SCAN_SEVERITY} 级（有修复版本的）漏洞: {image_ref}\n")


def run_build_thread(job_id: str, branch: str, sha: Optional[str], force_mode: str = "auto"):
    try:
        set_job(job_id, status="running", stage="checkout", progress=2, message="拉取源码...")
        head = checkout_ref(branch, sha)

        # 变更分类：相对线上已部署 commit 判定是否前端-only（force_mode=full 可绕过）
        if force_mode == "full":
            mode = "full"
        else:
            baseline = latest_deployed_commit()
            mode = classify_change(baseline, head)
        log.info("build %s: mode=%s（基线 %s）",
                 job_id, mode, latest_deployed_commit()[:8] or "无")

        set_job(job_id, stage="snapshot", progress=5, message="校验离线快照...")
        ready, snap_version = check_offline_snapshot()
        if not ready:
            if not snap_version:
                raise RuntimeError(
                    "构建工作区缺少 .offline-snapshot.json，无法确定依赖快照版本"
                )
            if not GL_DT_USER:
                raise RuntimeError(
                    "离线快照缺失或与锁文件不一致，且服务未配置 GitLab 下载凭据。"
                    "请在开发机执行 tools/prepare-offline.sh + tools/upload-offline-snapshot.sh"
                )
            set_job(job_id, message="离线快照缺失/不一致，从 GitLab Packages 同步...")
            with open(job_log_path(job_id), "a") as lf:
                sync_offline_snapshot(snap_version, lf)

        with open(job_log_path(job_id), "a") as lf:
            if mode == "full":
                ensure_claude_bin(lf)
                ensure_git_bin(lf)
            else:
                lf.write("[frontend-only] 跳过 claude/git bin 就位（仅应用镜像构建需要）\n")

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        board_file = BUILD_WS / ".build-progress.json"
        board_file.unlink(missing_ok=True)

        build_hint = "在 builder 容器内构建..." if mode == "full" else "在 builder 容器内构建前端..."
        set_job(job_id, stage="build", progress=8, message=build_hint)
        with open(job_log_path(job_id), "a") as lf:
            lf.write(f"$ docker run {BUILDER_IMAGE} bash /workspace/tools/build-in-container.sh\n")
            lf.flush()
            run_cmd = ["docker", "run", "--rm", "--name", f"ai-evo-build-{job_id}",
                       "-v", f"{BUILD_WS}:/workspace",
                       "-v", "/var/run/docker.sock:/var/run/docker.sock",
                       "-v", f"{CACHE_DIR}:/cache",
                       "-e", f"BUILD_SHA={head}",
                       "-e", f"IMAGE_NAME={IMAGE_NAME}"]
            if mode == "frontend":
                run_cmd += ["-e", "BUILD_FRONTEND_ONLY=1"]
            run_cmd += [BUILDER_IMAGE, "bash", "/workspace/tools/build-in-container.sh"]
            proc = subprocess.Popen(
                run_cmd,
                stdout=lf, stderr=subprocess.STDOUT,
            )
            deadline = time.time() + BUILD_TIMEOUT
            while True:
                rc = proc.poll()
                _publish_progress(job_id, board_file)
                if rc is not None:
                    break
                if time.time() > deadline:
                    proc.kill()
                    raise RuntimeError(f"构建超时（>{BUILD_TIMEOUT}s）")
                time.sleep(3)

        if rc != 0:
            raise RuntimeError(f"构建脚本退出码 {rc}，详见日志")

        if mode == "frontend":
            dist_index = BUILD_WS / "frontend" / "dist" / "index.html"
            if not dist_index.exists():
                raise RuntimeError("前端构建结束但未找到 dist/index.html")
            image = ""
            image_tag = f"frontend-{head[:8]}"
        else:
            image_file = BUILD_WS / ".build-image"
            if not image_file.exists():
                raise RuntimeError("构建结束但未找到 .build-image（镜像 tag）")
            image = image_file.read_text().strip()
            image_tag = image.split(":")[-1]

        # ARCH-3-3：漏洞门禁在写 release 记录**之前**——扫描不过的镜像
        # 不进可部署/可回滚列表（job 亦 failed，_resolve_deploy_tag 拒绝）
        set_job(job_id, stage="scan", progress=92, message="镜像漏洞扫描（trivy）...")
        with open(job_log_path(job_id), "a") as lf:
            scan_image(image, lf)

        record = {
            "image": image, "tag": image_tag, "commit": head,
            "branch": branch, "mode": mode,
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (RELEASES_DIR / f"{image_tag}.json").write_text(json.dumps(record, indent=2))
        prune_releases()

        set_job(job_id, status="success", stage="done", progress=100,
                finished_at=time.time(), message="构建完成",
                result=record)
        log.info("build %s 成功: %s (mode=%s)", job_id, image or image_tag, mode)
    except Exception as e:
        log.exception("build %s 失败", job_id)
        set_job(job_id, status="failed", stage="error", finished_at=time.time(),
                message=str(e)[:800])
    finally:
        BUSY_LOCK.release()


def prune_releases():
    """按构建时间保留最近 N 个版本记录（供回滚列表使用）"""
    records = sorted(RELEASES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for old in records[:-KEEP_RELEASES]:
        old.unlink(missing_ok=True)


def mark_deployed(commit: str) -> None:
    """部署成功后记录「线上已部署 commit」（前端-only 判定的 diff 基线）

    写失败不影响部署本身——最坏情况是下次构建退化为全量。
    """
    if not commit:
        return
    try:
        tmp = DEPLOYED_REF.with_suffix(".tmp")
        tmp.write_text(commit + "\n", encoding="utf-8")
        tmp.replace(DEPLOYED_REF)
    except OSError as e:
        log.warning("写已部署 commit 记录失败（不影响部署）: %s", e)


def latest_deployed_commit() -> str:
    """读「线上已部署 commit」；文件缺失/损坏时返回空串（安全默认全量构建）"""
    try:
        return DEPLOYED_REF.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError 是 ValueError 不是 OSError：基线文件被二进制
        # 内容污染时若不捕获，后续每次构建都会永久失败而不是退化为全量
        return ""


def classify_change(baseline: str, head: str) -> str:
    """判定本次变更是否前端-only，返回 "frontend" 或 "full"

    判定基准是**线上已部署 commit**（而不是 MR 相对目标分支的 diff）：
    语义是「相对线上正在运行的版本改了什么」，能兜住「前序后端 MR 部署
    失败后、紧跟的前端 MR 把后端变更漏掉不部署」的场景。

    任何不确定情况（无基线、diff 失败）一律按 full 处理——误判为 full
    只是多做一次镜像构建，误判为 frontend 会漏部署后端变更。
    """
    if not baseline:
        return "full"
    r = subprocess.run(
        ["git", "-C", str(BUILD_WS), "diff", "--name-only", baseline, head],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        log.info("diff %s..%s 失败（%s），按全量构建处理",
                 baseline[:8], head[:8], r.stderr.strip()[:200])
        return "full"
    changed = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if not changed:
        # 与线上同一 commit 的重复构建：无变更，按前端-only 同步一次 dist 即可
        return "frontend"
    return "frontend" if all(p.startswith(FRONTEND_PREFIX) for p in changed) else "full"


def current_image_tag() -> str:
    """读取部署目录 .env 里当前生效的 IMAGE_TAG"""
    if not ENV_FILE.exists():
        return ""
    m = re.search(r"^\s*IMAGE_TAG\s*=\s*(\S+)", ENV_FILE.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else ""


def prune_images(logf) -> None:
    """清理超期镜像，避免磁盘被逐次构建的镜像占满

    每个应用镜像约 500MB，而每次构建都会产生一个新 tag，不清理的话几十次部署
    后磁盘就会满。保留策略（取并集，保证回滚永远有目标）：

      - 最近 KEEP_RELEASES 个构建的 tag
      - 当前 .env 里正在使用的 tag
      - releases 记录里出现过的 tag

    另外 docker 自身会拒绝删除正在被容器使用的镜像，这是最后一道保险。
    KEEP_RELEASES 设为 0 可关闭本功能。
    """
    if KEEP_RELEASES <= 0:
        return

    try:
        r = subprocess.run(
            ["docker", "images", IMAGE_NAME, "--format", "{{.Tag}}\t{{.CreatedAt}}"],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        # 清理是收尾动作：docker daemon 繁忙时超时不应让已成功的部署被判失败
        logf.write(f"[prune] docker images 超时/失败，跳过本轮清理: {e}\n")
        return
    if r.returncode != 0:
        logf.write(f"[prune] docker images 失败: {r.stderr[:200]}\n")
        return

    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or parts[0] in ("<none>", "latest"):
            continue
        rows.append((parts[0], parts[1]))
    if len(rows) <= KEEP_RELEASES:
        return

    # 按创建时间倒序，取最近 KEEP_RELEASES 个
    rows.sort(key=lambda x: x[1], reverse=True)
    protected = {tag for tag, _ in rows[:KEEP_RELEASES]}
    protected.add(current_image_tag())
    for p in RELEASES_DIR.glob("*.json"):
        try:
            protected.add(json.loads(p.read_text()).get("tag", ""))
        except (OSError, json.JSONDecodeError):
            continue

    for tag, _ in rows:
        if tag in protected or not tag:
            continue
        try:
            rm = subprocess.run(["docker", "rmi", f"{IMAGE_NAME}:{tag}"],
                                capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            # 部署机高负载时 docker rmi 可能长时间不返回（实测 pipeline #513833
            # 120s 超时直接判 deploy 失败，而部署本体早已成功）。清理失败只影响
            # 磁盘占用，保留镜像继续下一个
            logf.write(f"[prune] 删除 {IMAGE_NAME}:{tag} 超时（>120s），保留该镜像\n")
            continue
        except OSError as e:
            logf.write(f"[prune] 删除 {IMAGE_NAME}:{tag} 失败，保留该镜像（{e}）\n")
            continue
        if rm.returncode == 0:
            logf.write(f"[prune] 已删除旧镜像 {IMAGE_NAME}:{tag}\n")
        else:
            logf.write(f"[prune] 保留 {IMAGE_NAME}:{tag}（{rm.stderr.strip()[:120]}）\n")


# ------------------------------------------------------------------
# deploy
# ------------------------------------------------------------------
def update_env_image_tag(tag: str) -> None:
    """把 IMAGE_TAG 写进部署目录的 .env（其它行原样保留，原子替换）"""
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    pattern = re.compile(r"^\s*IMAGE_TAG\s*=")
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"IMAGE_TAG={tag}"
            replaced = True
            break
    if not replaced:
        lines.append(f"IMAGE_TAG={tag}")

    tmp = ENV_FILE.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(ENV_FILE)
    log.info("已更新 %s 的 IMAGE_TAG=%s", ENV_FILE, tag)


# 部署前必须已存在于 .env 的关键变量（不含 IMAGE_TAG，那个由本服务写入）
#
# 这两个都在 docker-compose.yml 里改为从 env_file 读取了，compose 的 environment
# 段不再提供它们，所以 .env 里缺任何一个都会让容器起来后连不上依赖。
REQUIRED_ENV_KEYS = ("DATABASE_URL", "REDIS_URL")


def validate_env_file() -> None:
    """部署前校验 .env 齐备，缺了就提前失败

    这一步很关键：部署流程是先同步 compose、再往 .env 写 IMAGE_TAG（必要时
    会新建文件）、最后 compose up。如果 .env 里没有 DATABASE_URL，
    update_env_image_tag 会造出一个只有 IMAGE_TAG 的 .env，compose up 随即失败，
    而此时 compose 已经被仓库版本覆盖——等于把正在运行的部署搞坏。
    宁可在这里直接失败，也不要动线上。
    """
    if not ENV_FILE.exists():
        raise RuntimeError(
            f"部署目录缺少 {ENV_FILE}。请先从 .env.example 复制并填入 "
            f"{'、'.join(REQUIRED_ENV_KEYS)}；否则本次部署会覆盖 compose 后启动失败，"
            f"影响正在运行的服务"
        )

    text = ENV_FILE.read_text(encoding="utf-8")
    missing = [
        key for key in REQUIRED_ENV_KEYS
        if not re.search(rf"^\s*{re.escape(key)}\s*=", text, re.M)
    ]
    if missing:
        raise RuntimeError(
            f"{ENV_FILE} 缺少必需变量: {', '.join(missing)}。"
            f"请补齐后再部署（见 docs/build-pipeline.md 第 4.4 节）"
        )


def compose(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "--project-directory", str(DEPLOY_DIR), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def run_db_migrations(logf) -> None:
    """部署内自动数据库迁移：api 容器内 alembic upgrade head

    health/deep 的 migrations 探针会比对 alembic_version 与代码 heads，
    新增迁移后不升级必然 503（2026-10-14 W19 f1a9c5e3b7d2、2026-10-15
    b3c9d7e2f1a4 两次同型事故，都要部署完手动补迁移再重跑 job）。因此把
    升级放进部署流程，在 compose up 之后、verify 之前执行。失败重试 3 次
    （exec 偶发撞上容器启动窗口；迁移本身幂等，重试安全），仍失败则终止
    部署——不让 verify 阶段用 2 分钟探活重试慢速暴露同一个问题。
    """
    last_err = ""
    for attempt in range(1, 4):
        logf.write(f"[migrate] $ docker compose exec -T api alembic upgrade head（第 {attempt} 次）\n")
        logf.flush()
        try:
            mig = compose("exec", "-T", "api", "alembic", "upgrade", "head",
                          timeout=MIGRATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            last_err = f"迁移超时（>{MIGRATE_TIMEOUT}s）"
            logf.write(f"[migrate] {last_err}\n")
            logf.flush()
            time.sleep(5)
            continue
        logf.write(mig.stdout or "")
        if mig.stderr:
            logf.write(mig.stderr)
        logf.flush()
        if mig.returncode == 0:
            logf.write("[migrate] 数据库迁移完成\n")
            return
        last_err = (mig.stderr or mig.stdout or "").strip()[-500:]
        logf.write(f"[migrate] 退出码 {mig.returncode}：{last_err}\n")
        logf.flush()
        time.sleep(5)
    raise RuntimeError(f"alembic upgrade head 连续 3 次失败：{last_err}")


# ------------------------------------------------------------------
# worker 非阻塞优雅换代
#
# 旧编排 `docker compose up -d`（全家）重建 worker 时先 stop 旧容器，
# celery warm shutdown 要等当前任务跑完（stop_grace_period 7200s），整个
# compose up 与部署流水线被阻塞最长 2 小时。新编排把 worker 从 compose up
# 里拆出来单独处理（swap_worker）：
#   1. compose up 只重建除 worker 外的受管服务（秒级，旧 worker 不动）
#   2. 对旧 worker 发 SIGTERM（docker kill --signal=TERM 立即返回）——
#      celery warm shutdown：停止接新任务、跑完当前任务后自行退出
#   3. 流水线内短等 DRAIN_WAIT_SEC，退出则立即拉起新 worker（fast path）
#   4. 未退出（有长任务）转后台 watcher：旧容器退出后拉起新 worker，
#      超过 DRAIN_FORCE_KILL_SEC 强杀+告警；流水线立即成功结束，
#      排队任务由 Redis 暂存、新 worker 就绪后接手
# cron 的 ensure_worker.sh（每 5 分钟）是第二层兜底：drain 期间旧容器仍在
# docker ps 里它不会介入；旧容器退出后它同样能把新 worker 拉起来。
# ------------------------------------------------------------------

# watcher 内联脚本（build-service 容器内脱离部署线程运行）。
# 用法: python3 -c <script> <old_container_id> <deploy_dir> <force_kill_sec>
#                     <worker_name> <alerts_log> <drain_log>
# 按容器 ID 盯旧容器而不是按名字：compose 换代会删旧起新、名字不变，
# 若 cron 在 watcher 轮询间隙抢先换代，按名字盯会误判「仍在运行」而死循环。
WORKER_WATCHER_SCRIPT = r'''
import json, os, subprocess, sys, time, urllib.request

old_id, deploy_dir, force_kill_sec, worker_name, alerts_log, drain_log = sys.argv[1:7]
force_kill_sec = float(force_kill_sec)

def w(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)

def alert(level, title, details):
    payload = {"level": level, "title": title, "details": details,
               "source": "build-service-worker-watcher",
               "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    delivered = False
    if url:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                delivered = 200 <= resp.status < 300
        except Exception as e:  # noqa: BLE001 - 告警失败不影响换代流程
            w(f"告警 webhook 发送失败: {e}")
    try:
        with open(alerts_log, "a", encoding="utf-8") as f:
            record = dict(payload)
            record["webhook_delivered"] = delivered
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        w(f"告警落盘失败: {e}")

def status_of(ident):
    r = subprocess.run(["docker", "inspect", "--format", "{{.State.Status}}", ident],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else "missing"

w(f"watcher 启动：盯旧容器 {old_id[:12]}，强杀上限 {int(force_kill_sec)}s")
deadline = time.time() + force_kill_sec
while True:
    st = status_of(old_id)
    if st != "running":
        w(f"旧容器已不在运行（status={st}）")
        break
    if time.time() > deadline:
        w(f"超过 {int(force_kill_sec)}s 仍在运行（任务挂死？），强制回收")
        subprocess.run(["docker", "rm", "-f", old_id],
                       capture_output=True, text=True, timeout=60)
        alert("warning", "worker 长任务超限被强制回收",
              {"container": worker_name, "force_kill_sec": int(force_kill_sec),
               "hint": "celery task_time_limit（2h）本应让长任务到点自行结束，请核查超长任务"})
        break
    time.sleep(10)

up = subprocess.run(
    ["docker", "compose", "--project-directory", deploy_dir, "up", "-d", worker_name],
    capture_output=True, text=True, timeout=1800,
)
w((up.stdout or "") + (up.stderr or ""))
ok = False
for _ in range(12):
    if status_of(worker_name) == "running":
        ok = True
        break
    time.sleep(5)
if ok:
    w("新 worker 已运行，换代完成")
else:
    alert("error", "worker 换代后未运行",
          {"container": worker_name,
           "hint": "后台 watcher 拉起失败；cron 的 ensure_worker.sh 每 5 分钟会重试，请关注自愈日志"})
w("watcher 退出")
'''


def _up_services() -> list[str]:
    """compose up 的目标服务：除 worker 外的全部受管服务（config [deploy].services）

    worker 由 swap_worker 单独编排——compose up 重建 worker 会先 stop 旧容器，
    被 celery warm shutdown 阻塞最长 stop_grace_period（7200s），这正是要消除的。
    """
    return [s for s in DEPLOY_SERVICES if s != "worker"]


def worker_container_state(container: str = WORKER_CONTAINER) -> tuple[str, str]:
    """返回 (状态, 容器 ID)：状态为 docker inspect 的 State.Status，
    容器不存在时 ("missing", "")。"""
    r = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}} {{.Id}}", container],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return "missing", ""
    parts = r.stdout.split()
    if not parts:
        return "unknown", ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


def spawn_drain_watcher(old_container_id: str, logf) -> None:
    """启动后台 watcher：旧 worker 退出后拉起新 worker（脱离部署线程存活）

    build-service 容器重启会带走 watcher，此时由 cron 的 ensure_worker.sh
    兜底（每 5 分钟检查 worker 不在运行就拉起），任务队列在 Redis 不丢。
    watcher 启动失败同样只记日志、不阻断部署（cron 兜底）。
    """
    drain_log = LOGS_DIR / f"worker-drain-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.log"
    cmd = ["python3", "-c", WORKER_WATCHER_SCRIPT,
           old_container_id, str(DEPLOY_DIR), str(DRAIN_FORCE_KILL_SEC),
           WORKER_CONTAINER, str(ALERT_LOG_PATH), str(drain_log)]
    try:
        lf = open(drain_log, "a", encoding="utf-8")
    except OSError as e:
        logf.write(f"[worker] watcher 日志文件打开失败（{e}），仍尝试启动 watcher\n")
        lf = None
    try:
        subprocess.Popen(cmd, stdout=lf or subprocess.DEVNULL,
                         stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as e:  # noqa: BLE001 - watcher 失败不阻断部署，cron 兜底
        logf.write(f"[worker] watcher 启动失败（{e}），依赖 cron 自愈兜底\n")
        return
    finally:
        if lf:
            lf.close()
    logf.write(f"[worker] worker 换代转后台：旧容器 {old_container_id[:12]} 退出后自动拉起新 worker"
               f"（watcher 日志 {drain_log.name}，强杀上限 {DRAIN_FORCE_KILL_SEC}s）\n")


def swap_worker(logf) -> str:
    """把 worker 换到新镜像：非阻塞优雅换代，返回 "fast"|"deferred"|"noop"

    旧 worker 在运行且需要换代时：
      1. docker update --restart=no —— 不许 restart policy 在 warm shutdown
         退出后把旧镜像容器复活抢任务；
      2. docker kill --signal=TERM —— 立即返回；celery warm shutdown 停止
         接新任务、跑完当前任务后自行退出（挂死由 watcher 超时强杀）；
      3. 短等 DRAIN_WAIT_SEC，退出则 compose up worker（fast path）；
      4. 未退出转后台 watcher（deferred）：流水线不阻塞，排队任务由
         Redis 暂存，新 worker 就绪后接手。
    两条 docker 命令失败都不阻断（记日志继续）：kill 失败最坏退化为旧容器
    被 compose 后续动作处理，cron 亦可兜底。
    """
    state, cid = worker_container_state()
    if state != "running":
        logf.write(f"[worker] 容器状态 {state}，直接 compose up worker\n")
        logf.flush()
        up = compose("up", "-d", "worker", timeout=DEPLOY_TIMEOUT)
        logf.write(up.stdout or "")
        logf.write(up.stderr or "")
        if up.returncode != 0:
            raise RuntimeError(f"docker compose up -d worker 失败，退出码 {up.returncode}")
        return "noop"

    logf.write(f"[worker] 旧 worker 运行中（{cid[:12]}）：update --restart=no 后发 SIGTERM，"
               "进入非阻塞 drain（celery warm shutdown 跑完当前任务自行退出）\n")
    logf.flush()
    for cmd in (["docker", "update", "--restart=no", WORKER_CONTAINER],
                ["docker", "kill", "--signal=TERM", WORKER_CONTAINER]):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            logf.write(f"[worker] $ {' '.join(cmd)} 失败: {r.stderr.strip()[:200]}\n")

    deadline = time.time() + DRAIN_WAIT_SEC
    while time.time() < deadline:
        time.sleep(5)
        state, _ = worker_container_state()
        if state != "running":
            break
    if state == "running":
        logf.write(f"[worker] {DRAIN_WAIT_SEC}s 内旧任务未跑完，转入后台 watcher（不阻塞部署）\n")
        spawn_drain_watcher(cid, logf)
        return "deferred"

    logf.write("[worker] 旧 worker 已退出，拉起新 worker\n")
    logf.flush()
    up = compose("up", "-d", "worker", timeout=DEPLOY_TIMEOUT)
    logf.write(up.stdout or "")
    logf.write(up.stderr or "")
    if up.returncode != 0:
        raise RuntimeError(f"docker compose up -d worker 失败，退出码 {up.returncode}")
    return "fast"


# ------------------------------------------------------------------
# W10-3 发版前队列 drain 提示
# ------------------------------------------------------------------

def _env_value(env_text: str, key: str) -> Optional[str]:
    """从 .env 文本取变量值（去引号；不存在返回 None）"""
    m = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", env_text, re.M)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def collect_running_tasks(database_url: str) -> list[dict]:
    """查询 running 状态评测任务（id/name，最多列出前 20 条）

    直接 psycopg2 查询（build-service 不依赖 app.* 包）；失败抛异常由调用方兜底。
    """
    import psycopg2

    dsn = database_url.strip()
    if dsn.startswith("postgresql+"):
        dsn = "postgresql://" + dsn.split("://", 1)[1]
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name FROM evaluation_tasks "
                "WHERE status = 'running' ORDER BY created_at LIMIT 21"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "name": r[1]} for r in rows]


def collect_queue_depth(redis_url: str) -> int:
    """Redis celery 默认队列积压深度（排队待执行的消息数）"""
    import redis as redis_lib

    client = redis_lib.from_url(redis_url, decode_responses=True)
    try:
        return int(client.llen("celery") or 0)
    finally:
        client.close()


def queue_drain_hint(logf, env_file: Optional[Path] = None) -> None:
    """发版前队列 drain 提示（W10-3）：列出 running 任务数与排队深度

    只提示不拦截——stop_grace_period 已放宽到 7200s，worker 停止时会跑完
    当前任务再退出；排队中的任务保留在 Redis 队列由新 worker 接手。这里
    让发版者在日志里看到「此刻有多少任务受本次发版影响」，决定是否等
    任务跑完再发。查询失败只告警，绝不阻断部署流程。
    """
    env_path = env_file or ENV_FILE
    try:
        env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        database_url = _env_value(env_text, "DATABASE_URL")
        redis_url = _env_value(env_text, "REDIS_URL")

        running = []
        if database_url:
            running = collect_running_tasks(database_url)
        queued = collect_queue_depth(redis_url) if redis_url else None

        logf.write("[drain] 发版前队列 drain 提示：\n")
        logf.write(f"[drain]   running 任务数: {len(running)}\n")
        for t in running[:20]:
            logf.write(f"[drain]     - {t.get('id')} {t.get('name', '')}\n")
        if len(running) > 20:
            logf.write(f"[drain]     ...（其余 {len(running) - 20} 条略）\n")
        if queued is not None:
            logf.write(f"[drain]   Redis 队列积压消息数: {queued}\n")
        if running or (queued or 0) > 0:
            logf.write(
                "[drain]   提示: 当前有任务在执行/排队，worker 停止会等待当前任务跑完"
                "（最长 7200s）；如不希望发版打断，可等任务完成后再发版。\n"
            )
    except Exception as e:  # noqa: BLE001 - 提示失败不阻断部署
        logf.write(f"[drain] 队列状态查询失败（不影响部署）: {e}\n")


def verify_deploy(logf) -> None:
    """容器状态 + worker 存活 + HTTP 探活（吸收原 do_deploy.sh 的第 6/7 步）"""
    deadlines = time.time() + 180

    while time.time() < deadlines:
        ps = compose("ps", "--format", "json")
        if ps.returncode != 0:
            logf.write(f"[verify] docker compose ps 失败: {ps.stderr[:300]}\n")
            time.sleep(5)
            continue

        # docker compose ps --format json 逐行输出一个 JSON 对象（v2）
        rows = []
        for line in ps.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        states = {r.get("Service") or r.get("Name"): r.get("State", "") for r in rows}
        logf.write(f"[verify] 容器状态: {states}\n")

        expected = set(DEPLOY_SERVICES)
        missing = expected - set(states)
        not_running = {s for s in expected if states.get(s) != "running"}

        if not missing and not not_running:
            break
        logf.write(f"[verify] 等待容器就绪（缺少={missing or '无'}, 未运行={not_running or '无'}）\n")
        time.sleep(5)
    else:
        raise RuntimeError(f"容器在 180s 内未全部 running: {states}")

    # HTTP 探活：/api/v1/auth/status 在鉴权白名单内，不需要 cookie
    for _ in range(30):
        try:
            ensure_http_url(HEALTH_URL)
            with urllib.request.urlopen(HEALTH_URL, timeout=5) as r:
                if r.status == 200:
                    logf.write(f"[verify] HTTP 探活通过: {HEALTH_URL}\n")
                    break
        except Exception as e:
            logf.write(f"[verify] 探活重试中: {e}\n")
        time.sleep(4)
    else:
        raise RuntimeError(f"HTTP 探活失败: {HEALTH_URL}")

    logf.write("[verify] 校验通过\n")


def verify_deploy_with_alert(logf, job_id: str, tag: str, commit: str = "") -> None:
    """verify_deploy 的告警包装（W13）：校验失败 → 发告警 → 原样抛出。

    单独拆出来是为了可测：verify 本体依赖 docker/网络，告警逻辑靠
    monkeypatch verify_deploy 即可验证。
    """
    try:
        verify_deploy(logf)
    except Exception as e:
        send_alert(
            level="error",
            title="部署 verify 校验失败",
            details={
                "job_id": job_id,
                "tag": tag,
                "commit": commit,
                "error": str(e)[:800],
            },
        )
        raise


def _write_deploy_marker(job_id: str) -> None:
    """部署期间留下标记，供 crontab 里的 worker 自愈脚本跳过

    写失败不影响部署本身——最坏情况是回到"cron 可能介入"的旧行为。
    """
    try:
        DEPLOY_MARKER.write_text(
            f"job_id={job_id}\nstarted_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n",
            encoding="utf-8",
        )
    except OSError as e:
        log.warning("写部署标记失败（不影响部署）: %s", e)


def _clear_deploy_marker() -> None:
    try:
        DEPLOY_MARKER.unlink(missing_ok=True)
    except OSError as e:
        log.warning("清理部署标记失败: %s", e)


def sync_frontend(job_id: str, logf) -> None:
    """把构建出的 dist 同步到 nginx 的静态根

    用户访问的 eval.example.com 由 nginx 直接托管宿主机上的 frontend_root
    （见 /etc/nginx/conf.d/eval-frontend.conf），**不是**容器里 FastAPI 托管的那份。
    只更新容器内的 dist，用户看到的仍是旧前端——首次部署实际踩到了这个坑。

    换目录用「先拷到临时目录，再两次 rename」的方式，尽量缩短静态目录不可用的窗口。
    """
    if not FRONTEND_ROOT:
        logf.write("[frontend] 未配置 frontend_root，跳过前端同步\n")
        return

    src = BUILD_WS / "frontend" / "dist"
    if not (src / "index.html").exists():
        raise RuntimeError(f"构建产物缺少 {src}/index.html，拒绝同步前端（避免把站点清空）")

    dest = Path(FRONTEND_ROOT)
    dest.parent.mkdir(parents=True, exist_ok=True)

    tmp = dest.with_name(dest.name + f".new-{job_id}")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, symlinks=True)

    backup = None
    if dest.exists():
        backup = dest.with_name(dest.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        dest.rename(backup)
    tmp.rename(dest)
    logf.write(f"[frontend] 已同步前端到 {dest}\n")
    if backup:
        logf.write(f"[frontend] 旧前端已备份为 {backup.name}\n")

    prune_frontend_backups(dest, logf)


def prune_frontend_backups(dest: Path, logf) -> None:
    """只保留最近 N 份前端备份，避免磁盘被历史 dist 堆满"""
    if FRONTEND_BACKUPS <= 0:
        return
    backups = sorted(dest.parent.glob(dest.name + ".bak-*"),
                     key=lambda p: p.stat().st_mtime)
    for old in backups[:-FRONTEND_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)
        logf.write(f"[frontend] 清理旧备份 {old.name}\n")


def run_deploy_thread(job_id: str, tag: str, commit: str = "", mode: str = "full"):
    try:
        set_job(job_id, status="running", stage="precheck", progress=5,
                message="校验部署前置条件...")

        # 先校验再动任何文件：见 validate_env_file 的说明
        validate_env_file()

        if mode == "frontend":
            # 前端-only 部署：镜像与容器都没变，跳过 compose/迁移/verify，
            # 只把构建出的 dist 同步到 nginx 静态根。nginx 托管的是宿主机
            # 目录、每次请求都读磁盘，同步即生效，无需 reload。
            set_job(job_id, stage="frontend", progress=50,
                    message="前端-only 部署：同步静态资源...")
            with open(job_log_path(job_id), "a") as lf:
                lf.write("[deploy] 前端-only 部署：跳过镜像切换/容器重建/数据库迁移，仅同步前端\n")
                sync_frontend(job_id, lf)
            mark_deployed(commit)
            set_job(job_id, status="success", stage="done", progress=100,
                    finished_at=time.time(),
                    message="前端-only 部署完成（容器未重启）",
                    result={"tag": tag, "commit": commit, "mode": mode})
            log.info("deploy %s 成功（frontend-only）: %s", job_id, commit[:8])
            return

        set_job(job_id, stage="sync-config", progress=10,
                message="同步 compose 配置...")

        with open(job_log_path(job_id), "a") as lf:
            # 0. 发版前队列 drain 提示（W10-3）：列出 running 任务数与排队深度，
            #    仅提示不拦截，让发版者决定是否等任务跑完再发
            queue_drain_hint(lf)

            # 1. 从构建工作区同步 compose（保证仓库里的配置变更也能随代码生效）
            src = BUILD_WS / COMPOSE_FILE.name
            if src.exists():
                if COMPOSE_FILE.exists() and COMPOSE_FILE.read_bytes() == src.read_bytes():
                    lf.write("[deploy] docker-compose.yml 无变更\n")
                else:
                    shutil.copy2(src, COMPOSE_FILE)
                    lf.write("[deploy] docker-compose.yml 已同步到部署目录\n")
            else:
                lf.write("[deploy] 警告: 构建工作区未找到 docker-compose.yml，沿用部署目录现有配置\n")

            # 2. 切换镜像 tag
            set_job(job_id, stage="switch-tag", progress=25, message=f"切换到镜像 {tag}...")
            update_env_image_tag(tag)
            lf.write(f"[deploy] IMAGE_TAG={tag}\n")

            # 3. 拉起容器（worker 除外——它由 swap_worker 单独非阻塞换代：
            #    compose up 重建 worker 会被 celery warm shutdown 阻塞最长
            #    stop_grace_period 7200s，这正是本次改造消除的部署阻塞）
            #    这期间写部署标记，让 cron 的 worker 自愈跳过，避免它插入
            #    一次 compose up 与本次部署撞车
            _write_deploy_marker(job_id)
            try:
                up_services = _up_services()
                set_job(job_id, stage="compose-up", progress=45,
                        message=f"docker compose up -d {' '.join(up_services)} ...")
                lf.write(f"[deploy] $ docker compose up -d {' '.join(up_services)}"
                         "（worker 由 swap_worker 单独编排）\n")
                lf.flush()
                up = compose("up", "-d", *up_services, timeout=DEPLOY_TIMEOUT)
                lf.write(up.stdout or "")
                lf.write(up.stderr or "")
                if up.returncode != 0:
                    raise RuntimeError(f"docker compose up -d 失败，退出码 {up.returncode}")

                # 3.5 数据库迁移（verify 之前：health/deep 的 migrations 探针
                #     比对 alembic_version 与代码 heads，不升级必 503）
                set_job(job_id, stage="migrate", progress=60,
                        message="数据库迁移 alembic upgrade head ...")
                run_db_migrations(lf)

                # 4. 校验（W13：verify 失败 → 告警 webhook / alerts.log）
                #    此时旧 worker 仍未被触碰、处于 running，校验语义不变
                set_job(job_id, stage="verify", progress=75, message="校验容器与接口...")
                verify_deploy_with_alert(lf, job_id, tag, commit)

                # 5. 同步前端到 nginx 静态根
                #    放在容器起来之后：新前端可见时 API 已经就绪，避免出现
                #    "新版页面调用还没上线的接口" 的短暂窗口
                set_job(job_id, stage="frontend", progress=88, message="同步前端到 nginx 根目录...")
                sync_frontend(job_id, lf)
            finally:
                _clear_deploy_marker()

            # 6. worker 非阻塞优雅换代。放在 marker 清除之后：drain 期间旧
            #    容器仍在 docker ps，cron 不会介入；旧容器退出后 cron 同样
            #    是拉起新 worker 的兜底
            set_job(job_id, stage="worker-swap", progress=92, message="worker 优雅换代...")
            swap_mode = swap_worker(lf)

            # 7. 清理旧镜像（部署成功后再清，此时新镜像正在使用不会被误删）
            set_job(job_id, stage="prune", progress=95, message="清理历史镜像...")
            prune_images(lf)

        mark_deployed(commit)

        done_msg = ("部署完成（worker 换代转后台：旧任务跑完后自动拉起新 worker）"
                    if swap_mode == "deferred" else "部署完成")
        set_job(job_id, status="success", stage="done", progress=100,
                finished_at=time.time(), message=done_msg,
                result={"tag": tag, "commit": commit, "mode": mode,
                        "worker_swap": swap_mode})
        log.info("deploy %s 成功: %s (worker_swap=%s)", job_id, tag, swap_mode)
    except Exception as e:
        log.exception("deploy %s 失败", job_id)
        set_job(job_id, status="failed", stage="error", finished_at=time.time(),
                message=str(e)[:800])
    finally:
        BUSY_LOCK.release()


# ------------------------------------------------------------------
# 请求模型
# ------------------------------------------------------------------
class BuildReq(BaseModel):
    branch: str = "main"
    sha: Optional[str] = Field(default=None, description="构建的 commit SHA，缺省取分支最新")
    force_mode: str = Field(
        default="auto",
        description='"auto" 按相对线上已部署 commit 的变更判定构建模式；"full" 强制全量构建（Web 手动触发调试用）',
    )


class DeployReq(BaseModel):
    job_id: Optional[str] = Field(default=None, description="对应的构建 job_id")
    tag: Optional[str] = Field(default=None, description="直接指定镜像 tag（回滚用）")


# ------------------------------------------------------------------
# 路由
# ------------------------------------------------------------------
@app.get("/api/v1/health")
def health():
    return {"status": "ok", "time": time.time()}


@app.post("/api/v1/build")
def start_build(req: BuildReq, x_build_token: Optional[str] = Header(default=None)):
    check_token(x_build_token)
    if not BUSY_LOCK.acquire(blocking=False):
        raise HTTPException(409, "已有构建或部署任务在执行，请稍后重试")
    try:
        job = new_job("build", req.model_dump())
        job_log_path(job["job_id"]).touch()
        threading.Thread(target=run_build_thread,
                         args=(job["job_id"], req.branch, req.sha, req.force_mode),
                         daemon=True).start()
        return job
    except Exception:
        BUSY_LOCK.release()
        raise


def _resolve_deploy_tag(req: DeployReq) -> tuple[str, str, str]:
    """确定要部署的镜像 tag；(tag, commit, mode)

    mode="frontend" 表示该构建产物仅含前端 dist，部署只同步静态资源；
    mode="full" 为常规全量部署（显式指定 tag 的回滚一律走全量路径）。
    历史构建记录没有 mode 字段，缺省按 full 处理。
    """
    if req.tag:
        commit = ""
        rec_file = RELEASES_DIR / f"{req.tag}.json"
        if rec_file.exists():
            try:
                commit = json.loads(rec_file.read_text()).get("commit", "")
            except (OSError, json.JSONDecodeError):
                commit = ""
        return req.tag, commit, "full"
    if not req.job_id:
        raise HTTPException(400, "需要提供 job_id 或 tag")
    job = JOBS.get(req.job_id)
    if not job or job["status"] != "success" or job["kind"] != "build":
        raise HTTPException(400, f"构建任务 {req.job_id} 不存在或未成功")
    return (job["result"]["tag"], job["result"].get("commit", ""),
            job["result"].get("mode", "full"))


@app.post("/api/v1/deploy")
def start_deploy(req: DeployReq, x_build_token: Optional[str] = Header(default=None)):
    check_token(x_build_token)
    tag, commit, mode = _resolve_deploy_tag(req)
    if not BUSY_LOCK.acquire(blocking=False):
        raise HTTPException(409, "已有构建或部署任务在执行，请稍后重试")
    try:
        job = new_job("deploy", {"tag": tag, "commit": commit, "mode": mode})
        job_log_path(job["job_id"]).touch()
        threading.Thread(target=run_deploy_thread,
                         args=(job["job_id"], tag, commit, mode), daemon=True).start()
        return job
    except Exception:
        BUSY_LOCK.release()
        raise


@app.post("/api/v1/rollback")
def start_rollback(req: DeployReq, x_build_token: Optional[str] = Header(default=None)):
    """回退到历史镜像 tag（不重新构建）"""
    check_token(x_build_token)
    if not req.tag:
        raise HTTPException(400, "回滚必须显式提供 tag")
    return start_deploy(DeployReq(tag=req.tag), x_build_token)


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, x_build_token: Optional[str] = Header(default=None)):
    check_token(x_build_token)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"任务 {job_id} 不存在")
    return job


@app.get("/api/v1/jobs/{job_id}/log")
def get_job_log(job_id: str, lines: int = 100,
                x_build_token: Optional[str] = Header(default=None)):
    check_token(x_build_token)
    if job_id not in JOBS:
        raise HTTPException(404, f"任务 {job_id} 不存在")
    return {"job_id": job_id, "log": log_tail(job_id, lines)}


@app.get("/api/v1/releases")
def list_releases(x_build_token: Optional[str] = Header(default=None)):
    """可回滚的版本列表（按构建时间倒序）"""
    check_token(x_build_token)
    items = []
    for p in sorted(RELEASES_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            items.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return {"total": len(items), "items": items}


@app.get("/api/v1/images")
def list_images(x_build_token: Optional[str] = Header(default=None)):
    """宿主机上现存的 ai-evo 镜像（回滚时确认目标 tag 是否还在）"""
    check_token(x_build_token)
    r = subprocess.run(
        ["docker", "images", IMAGE_NAME, "--format", "{{.Tag}}\t{{.CreatedAt}}\t{{.Size}}"],
        capture_output=True, text=True, timeout=60,
    )
    tags = [line.split("\t") for line in r.stdout.splitlines() if line.strip()]
    return {"images": [{"tag": t[0], "created_at": t[1], "size": t[2]} for t in tags if len(t) == 3]}
