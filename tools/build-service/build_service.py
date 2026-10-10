#!/usr/bin/env python3
"""
ai-evo 构建服务（build-service）

模块名刻意不叫 app.py：仓库根目录已经有一个 app/ 包（主应用），
同名会与之冲突——在全量跑测试时 `import app` 拿到的是主应用包而非本模块，
导致测试以难以察觉的方式失效。

常驻容器运行在部署机上（默认 :8180），职责：
  1. build   ：在 ai-evo-builder 容器内离线构建前端 dist 与应用镜像
  2. deploy  ：同步 docker-compose.yml，更新 .env 的 IMAGE_TAG，compose up -d 并校验
  3. rollback：回退到某个历史镜像 tag（不重新构建）
  4. jobs    ：异步任务状态与日志查询（GitLab CI 轮询用）

鉴权：除 /api/v1/health 外全部要求 X-Build-Token 请求头。
并发：同一时刻只允许一个 build 或 deploy 任务（否则 409）。
配置：/app/config.toml（见 config.example.toml）。

为什么需要这个服务：部署机是 CentOS 7 且不能下载任何东西，GitLab runner
只负责调用本服务；真正的构建在一个预烘焙好工具链的容器里完成，依赖全部
来自离线快照。CI 侧只做 curl + 轮询，与本服务的实现细节解耦。
"""

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
# 要优雅退出（最长 900s），容器处于 Stopping、不出现在 `docker ps` 里——cron
# 会误判为"挂了"并介入，与外层的 compose 撞车。部署期间写这个标记，
# crontab 侧用 `[ -f <标记> ] ||` 跳过自愈。
DEPLOY_MARKER = DEPLOY_DIR / ".deploy-in-progress"
DEPLOY_SERVICES = CFG.get("deploy", {}).get("services", ["api", "worker"])
HEALTH_URL = CFG.get("deploy", {}).get("health_url", "http://127.0.0.1:8100/api/v1/auth/status")
# nginx 的前端静态根（宿主机路径）。首次部署实测踩到：用户访问的站点由 nginx
# 直接托管这个目录，而不是容器里 FastAPI 托管的那份 dist——只更新容器内的
# dist，用户看到的仍是旧前端。为空则跳过同步。
FRONTEND_ROOT = CFG.get("deploy", {}).get("frontend_root", "")
FRONTEND_BACKUPS = int(CFG.get("deploy", {}).get("keep_frontend_backups", 3))
KEEP_RELEASES = int(CFG.get("deploy", {}).get("keep_releases", 5))

BUILD_TIMEOUT = int(CFG.get("timeouts", {}).get("build_sec", 3600))
DEPLOY_TIMEOUT = int(CFG.get("timeouts", {}).get("deploy_sec", 1800))

GIT_REMOTE = CFG.get("gitlab", {}).get("repo_url", "origin")

GL_HOST = CFG.get("gitlab", {}).get("host", "https://gitlab.example.com")
GL_PROJECT = str(CFG.get("gitlab", {}).get("project_id", ""))
GL_DT_USER = CFG.get("gitlab", {}).get("deploy_token_user", "")
GL_DT_PASS = CFG.get("gitlab", {}).get("deploy_token_pass", "")
GL_SNAPSHOT_PKG = CFG.get("gitlab", {}).get("snapshot_package", "ai-evo-offline-snapshot")
GL_CLAUDE_PKG = CFG.get("gitlab", {}).get("claude_package", "ai-evo-claude-bin")

LOGS_DIR = DATA_DIR / "logs"
RELEASES_DIR = DATA_DIR / "releases"
for d in (LOGS_DIR, RELEASES_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ai-evo-build-service")

app = FastAPI(title="ai-evo-build-service", docs_url=None, redoc_url=None)

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
        "--exclude=claude-local", "--exclude=.claude-bin-version")
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


def run_build_thread(job_id: str, branch: str, sha: Optional[str]):
    try:
        set_job(job_id, status="running", stage="checkout", progress=2, message="拉取源码...")
        head = checkout_ref(branch, sha)

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
            ensure_claude_bin(lf)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        board_file = BUILD_WS / ".build-progress.json"
        board_file.unlink(missing_ok=True)

        set_job(job_id, stage="build", progress=8, message="在 builder 容器内构建...")
        with open(job_log_path(job_id), "a") as lf:
            lf.write(f"$ docker run {BUILDER_IMAGE} bash /workspace/tools/build-in-container.sh\n")
            lf.flush()
            proc = subprocess.Popen(
                ["docker", "run", "--rm", "--name", f"ai-evo-build-{job_id}",
                 "-v", f"{BUILD_WS}:/workspace",
                 "-v", "/var/run/docker.sock:/var/run/docker.sock",
                 "-v", f"{CACHE_DIR}:/cache",
                 "-e", f"BUILD_SHA={head}",
                 "-e", f"IMAGE_NAME={IMAGE_NAME}",
                 BUILDER_IMAGE, "bash", "/workspace/tools/build-in-container.sh"],
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

        image_file = BUILD_WS / ".build-image"
        if not image_file.exists():
            raise RuntimeError("构建结束但未找到 .build-image（镜像 tag）")
        image = image_file.read_text().strip()
        image_tag = image.split(":")[-1]

        record = {
            "image": image, "tag": image_tag, "commit": head,
            "branch": branch, "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (RELEASES_DIR / f"{image_tag}.json").write_text(json.dumps(record, indent=2))
        prune_releases()

        set_job(job_id, status="success", stage="done", progress=100,
                finished_at=time.time(), message="构建完成",
                result=record)
        log.info("build %s 成功: %s", job_id, image)
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

    r = subprocess.run(
        ["docker", "images", IMAGE_NAME, "--format", "{{.Tag}}\t{{.CreatedAt}}"],
        capture_output=True, text=True, timeout=120,
    )
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
        rm = subprocess.run(["docker", "rmi", f"{IMAGE_NAME}:{tag}"],
                            capture_output=True, text=True, timeout=120)
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


def run_deploy_thread(job_id: str, tag: str, commit: str = ""):
    try:
        set_job(job_id, status="running", stage="precheck", progress=5,
                message="校验部署前置条件...")

        # 先校验再动任何文件：见 validate_env_file 的说明
        validate_env_file()

        set_job(job_id, stage="sync-config", progress=10,
                message="同步 compose 配置...")

        with open(job_log_path(job_id), "a") as lf:
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

            # 3. 拉起容器
            #    这期间 worker 会优雅退出（容器 Stopping、不在 docker ps 里），
            #    所以先写部署标记，让 cron 的 worker 自愈跳过，避免它插入一次
            #    compose up 与本次部署撞车
            _write_deploy_marker(job_id)
            try:
                set_job(job_id, stage="compose-up", progress=45, message="docker compose up -d ...")
                lf.write("[deploy] $ docker compose up -d\n")
                lf.flush()
                up = compose("up", "-d", timeout=DEPLOY_TIMEOUT)
                lf.write(up.stdout or "")
                lf.write(up.stderr or "")
                if up.returncode != 0:
                    raise RuntimeError(f"docker compose up -d 失败，退出码 {up.returncode}")

                # 4. 校验
                set_job(job_id, stage="verify", progress=75, message="校验容器与接口...")
                verify_deploy(lf)

                # 5. 同步前端到 nginx 静态根
                #    放在容器起来之后：新前端可见时 API 已经就绪，避免出现
                #    "新版页面调用还没上线的接口" 的短暂窗口
                set_job(job_id, stage="frontend", progress=88, message="同步前端到 nginx 根目录...")
                sync_frontend(job_id, lf)
            finally:
                _clear_deploy_marker()

            # 6. 清理旧镜像（部署成功后再清，此时新镜像正在使用不会被误删）
            set_job(job_id, stage="prune", progress=95, message="清理历史镜像...")
            prune_images(lf)

        set_job(job_id, status="success", stage="done", progress=100,
                finished_at=time.time(), message="部署完成",
                result={"tag": tag, "commit": commit})
        log.info("deploy %s 成功: %s", job_id, tag)
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
                         args=(job["job_id"], req.branch, req.sha), daemon=True).start()
        return job
    except Exception:
        BUSY_LOCK.release()
        raise


def _resolve_deploy_tag(req: DeployReq) -> tuple[str, str]:
    """确定要部署的镜像 tag；(tag, commit)"""
    if req.tag:
        return req.tag, ""
    if not req.job_id:
        raise HTTPException(400, "需要提供 job_id 或 tag")
    job = JOBS.get(req.job_id)
    if not job or job["status"] != "success" or job["kind"] != "build":
        raise HTTPException(400, f"构建任务 {req.job_id} 不存在或未成功")
    return job["result"]["tag"], job["result"].get("commit", "")


@app.post("/api/v1/deploy")
def start_deploy(req: DeployReq, x_build_token: Optional[str] = Header(default=None)):
    check_token(x_build_token)
    tag, commit = _resolve_deploy_tag(req)
    if not BUSY_LOCK.acquire(blocking=False):
        raise HTTPException(409, "已有构建或部署任务在执行，请稍后重试")
    try:
        job = new_job("deploy", {"tag": tag, "commit": commit})
        job_log_path(job["job_id"]).touch()
        threading.Thread(target=run_deploy_thread,
                         args=(job["job_id"], tag, commit), daemon=True).start()
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
