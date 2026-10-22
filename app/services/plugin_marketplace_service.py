"""插件市场服务（插件评测「从市场选择评测对象」来源）

插件市场 = 一个 Claude Code marketplace git 仓库（根目录 .claude-plugin/marketplace.json
列出插件清单，每个 source 指向仓库内相对路径）。服务职责：
- 添加/删除市场：git 浅克隆到 uploads/plugin_marketplaces/{id}/，删除时清理目录
- 同步市场：fetch --depth 1 + reset --hard（浅克隆的更新方式），目录丢失自动重克隆
- 浏览市场：解析 marketplace.json 的 plugins[]，逐项 detect_target 分类（plugin/skill）
- 评测执行期懒加载：每次评测开始前把本地副本更新到远端最新（节流窗口 + 失败降级），
  并记录被评测插件所在 commit（版本锚点），见 prepare_market_plugin_for_eval
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse, urlunparse

from sqlalchemy import func

from app.db.session import SessionLocal
from app.models.plugin_marketplace_models import PluginMarketplace
from app.services.plugin_eval.parser import detect_target

logger = logging.getLogger(__name__)

# 市场克隆根目录（与数据集/插件上传目录同级约定）
MARKETPLACE_BASE_DIR = Path("uploads/plugin_marketplaces")

GIT_TIMEOUT = 300  # 单次 git 命令超时（秒）

# 评测期懒加载节流窗口：同一市场在窗口内已同步过就跳过 fetch（避免连续评测重复拉
# 同一仓库）。置 0 = 每次评测都拉。可经 PLUGIN_MARKET_LAZY_SYNC_INTERVAL 覆盖。
LAZY_SYNC_MIN_INTERVAL = 60


class MarketplaceError(Exception):
    """市场操作业务错误（API 层转 400）"""


def _run_git(args: List[str], cwd: Optional[str] = None) -> str:
    """执行 git 命令，非零退出抛 MarketplaceError

    统一前置 -c credential.helper=（清空 helper 链）：macOS 的 Apple git 默认
    osxkeychain helper 会在认证后尝试存凭据并弹 GUI 授权框（后台进程永久
    挂起）；服务器凭据经 URL 注入、不允许落任何 credential store。
    """
    try:
        proc = subprocess.run(
            ["git", "-c", "credential.helper=", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise MarketplaceError(f"git 命令超时（>{GIT_TIMEOUT}s）: git {' '.join(args)}")
    except FileNotFoundError:
        raise MarketplaceError("未找到 git 命令，请先安装 git")
    if proc.returncode != 0:
        raise MarketplaceError(f"git {' '.join(args)} 失败: {proc.stderr.strip()[:500]}")
    return proc.stdout


def _git_credential_url(url: str) -> str:
    """按环境变量给 http(s) git 地址注入基本认证凭据

    部署环境（无本机 git 凭据）通过 PLUGIN_MARKET_GIT_USERNAME /
    PLUGIN_MARKET_GIT_PASSWORD 提供只读账号；两个变量都非空且地址未带
    凭据时，凭据 URL 编码后注入命令行参数（不写入仓库 remote 配置，
    见 _clone_marketplace 的 set-url 还原）。其余情形原样返回。
    """
    username = os.environ.get("PLUGIN_MARKET_GIT_USERNAME", "").strip()
    password = os.environ.get("PLUGIN_MARKET_GIT_PASSWORD", "")
    if not username or not password:
        return url
    if not url.startswith(("http://", "https://")):
        return url
    parsed = urlparse(url)
    if parsed.username:
        return url
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{parsed.netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def _clone_marketplace(git_url: str, branch: str, dest: Path) -> None:
    """浅克隆市场仓库到 dest（dest 必须不存在，由调用方保证）"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", "--depth", "1"]
    if branch:
        args += ["--branch", branch]
    args += [_git_credential_url(git_url), str(dest)]
    _run_git(args)
    # 凭据只存在于本次命令行参数：clone 后 remote url 还原为干净地址，避免凭据落盘
    if _git_credential_url(git_url) != git_url:
        _run_git(["remote", "set-url", "origin", git_url], cwd=str(dest))


def _get_remote_url(repo_dir: Path) -> str:
    return _run_git(["remote", "get-url", "origin"], cwd=str(repo_dir)).strip()


def _fetch_and_reset(repo_dir: Path, branch: str, git_url: str = "") -> None:
    """浅克隆仓库更新到远端最新：fetch --depth 1 + reset --hard FETCH_HEAD

    fetch 显式指定（注入凭据后的）仓库地址而非 origin 别名，凭据不落盘。
    """
    fetch_url = _git_credential_url(git_url or _get_remote_url(repo_dir))
    args = ["fetch", "--depth", "1", fetch_url]
    if branch:
        args.append(branch)
    _run_git(args, cwd=str(repo_dir))
    _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=str(repo_dir))


def _to_record(market: PluginMarketplace) -> Dict[str, Any]:
    return {
        "id": market.id,
        "name": market.name,
        "git_url": market.git_url,
        "branch": market.branch or "",
        "local_path": market.local_path or "",
        "sync_status": market.sync_status,
        "sync_error": market.sync_error or "",
        "last_synced_at": market.last_synced_at.isoformat() if market.last_synced_at else None,
        "created_at": market.created_at.isoformat() if market.created_at else None,
    }


def _get_market(db, market_id: int) -> PluginMarketplace:
    market = db.query(PluginMarketplace).filter(PluginMarketplace.id == market_id).first()
    if not market:
        raise MarketplaceError(f"插件市场不存在: {market_id}")
    return market


# ---------------------------------------------------------------------------
# 增删查 / 同步
# ---------------------------------------------------------------------------


def list_marketplaces(db) -> List[Dict[str, Any]]:
    return [_to_record(m) for m in db.query(PluginMarketplace).order_by(PluginMarketplace.id).all()]


def create_marketplace(db, name: str, git_url: str, branch: str = "") -> Dict[str, Any]:
    """新增市场并立即克隆；克隆失败则不保留记录（错误信息随异常抛出）"""
    name = (name or "").strip()
    git_url = (git_url or "").strip()
    branch = (branch or "").strip()
    if not name:
        raise MarketplaceError("市场名称不能为空")
    if not git_url.startswith(("http://", "https://", "git@", "ssh://", "file://")):
        raise MarketplaceError(f"git 地址不合法: {git_url}")

    exists = db.query(PluginMarketplace).filter(PluginMarketplace.git_url == git_url).first()
    if exists:
        raise MarketplaceError(f"该 git 地址已添加为市场「{exists.name}」")

    market = PluginMarketplace(name=name, git_url=git_url, branch=branch or None, sync_status="syncing")
    db.add(market)
    db.commit()
    db.refresh(market)

    dest = MARKETPLACE_BASE_DIR / str(market.id)
    try:
        if dest.exists():
            shutil.rmtree(dest)
        _clone_marketplace(git_url, branch, dest)
    except MarketplaceError as e:
        db.delete(market)
        db.commit()
        raise MarketplaceError(f"克隆市场仓库失败: {e}")

    market.local_path = str(dest.resolve())
    market.sync_status = "ok"
    market.sync_error = None
    market.last_synced_at = func.now()
    db.commit()
    db.refresh(market)
    logger.info(f"[插件市场] 已添加市场 {market.id}({name}) 并克隆到 {market.local_path}")
    return _to_record(market)


def _sync_local_copy(db, market: PluginMarketplace) -> None:
    """把市场本地副本更新到远端最新；本地目录丢失时重新浅克隆

    成功时只更新 market 的内存属性（local_path），落库由调用方决定——显式同步
    记 ok，评测期懒加载失败不覆盖 sync_status（见 refresh_marketplace_if_stale）。
    """
    dest = Path(market.local_path) if market.local_path else None
    if not dest or not (dest / ".git").exists():
        dest = MARKETPLACE_BASE_DIR / str(market.id)
        if dest.exists():
            shutil.rmtree(dest)
        _clone_marketplace(market.git_url, market.branch or "", dest)
        market.local_path = str(dest.resolve())
    else:
        _fetch_and_reset(dest, market.branch or "", market.git_url)


def _head_commit(repo_dir: Path) -> str:
    """本地副本当前 HEAD 短 SHA（版本锚点，回答「这个分是哪个版本的插件」）"""
    try:
        return _run_git(["rev-parse", "--short=12", "HEAD"], cwd=str(repo_dir)).strip()
    except MarketplaceError as e:
        logger.warning(f"[插件市场] 读取本地副本版本失败: {e}")
        return ""


def sync_marketplace(db, market_id: int) -> Dict[str, Any]:
    """同步市场到远端最新；本地目录丢失时自动重新克隆"""
    market = _get_market(db, market_id)
    try:
        _sync_local_copy(db, market)
    except MarketplaceError as e:
        market.sync_status = "failed"
        market.sync_error = str(e)
        db.commit()
        raise MarketplaceError(f"同步市场失败: {e}")

    market.sync_status = "ok"
    market.sync_error = None
    market.last_synced_at = func.now()
    db.commit()
    db.refresh(market)
    logger.info(f"[插件市场] 市场 {market_id}({market.name}) 已同步")
    return _to_record(market)


def delete_marketplace(db, market_id: int) -> Dict[str, Any]:
    """删除市场记录并清理本地克隆目录"""
    market = _get_market(db, market_id)
    record = _to_record(market)
    if market.local_path:
        shutil.rmtree(market.local_path, ignore_errors=True)
    db.delete(market)
    db.commit()
    logger.info(f"[插件市场] 已删除市场 {market_id}({record['name']})")
    return record


# ---------------------------------------------------------------------------
# 浏览市场内容
# ---------------------------------------------------------------------------


def _safe_resolve(local_path: Path, relative: str) -> Optional[Path]:
    """把 marketplace.json 的 source 相对路径解析为 local_path 内的绝对路径

    防目录穿越：resolve 后必须仍在 local_path 内；非法或越界返回 None。
    """
    if not relative:
        return None
    cleaned = relative.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.strip("/")
    if not cleaned or cleaned.startswith("."):
        return None
    resolved = (local_path / cleaned).resolve()
    try:
        resolved.relative_to(local_path.resolve())
    except ValueError:
        return None
    return resolved if resolved.is_dir() else None


def _skill_description(skill_dir: Path) -> str:
    """读取 SKILL.md frontmatter 的 description（供列表展示，失败返回空串）"""
    try:
        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    from app.services.plugin_eval.parser import parse_skill_content

    try:
        return parse_skill_content(content).description or ""
    except Exception:  # noqa: BLE001 — 描述仅用于展示，解析失败不影响可选性
        return ""


def _expand_plugin_skills(
    plugin_source: str, plugin_dir: Path, plugin_name: str
) -> List[Dict[str, Any]]:
    """展开插件内的 skills/*/ 为可评测的 skill 级条目

    Claude Code 插件约定 `<plugin>/skills/<name>/SKILL.md`。插件级对象只跑
    静态层，深度评审（Claude Code judge 层）需要按单个 skill 评测，因此市场
    列表必须同时给出 skill 级选项（plugin-eval 的同一口径：插件级仅静态层、
    skill 级可深度评）。
    """
    skills_root = plugin_dir / "skills"
    if not skills_root.is_dir():
        return []
    items = []
    base = plugin_source.rstrip("/")
    for child in sorted(skills_root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not (child / "SKILL.md").is_file():
            continue
        items.append({
            "name": f"{plugin_name} / {child.name}",
            "description": _skill_description(child),
            "version": "",
            "source": f"{base}/skills/{child.name}",
            "path": str(child),
            "target_type": "skill",
        })
    return items


def _list_from_marketplace_json(local_path: Path) -> List[Dict[str, Any]]:
    """解析 .claude-plugin/marketplace.json 的 plugins[]（Claude Code 市场标准格式）

    每个插件条目后紧跟其展开的 skill 级条目（插件级只跑静态层，skill 级可
    深度评），保持插入顺序便于前端按插件分组浏览。
    """
    manifest = local_path / ".claude-plugin" / "marketplace.json"
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[插件市场] marketplace.json 解析失败: {e}")
        return []
    items = []
    for entry in data.get("plugins") or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "")
        target_dir = _safe_resolve(local_path, source)
        if target_dir is None:
            logger.warning(f"[插件市场] 跳过非法或缺失的插件目录: {source}")
            continue
        plugin_name = str(entry.get("name") or target_dir.name)
        items.append({
            "name": plugin_name,
            "description": str(entry.get("description") or ""),
            "version": str(entry.get("version") or ""),
            "source": source,
            "path": str(target_dir),
            "target_type": detect_target(target_dir),
        })
        items.extend(_expand_plugin_skills(source, target_dir, plugin_name))
    return items


def _list_by_scan(local_path: Path) -> List[Dict[str, Any]]:
    """无 marketplace.json 时兜底：扫描一级子目录中的插件/skill"""
    items = []
    for child in sorted(local_path.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name == "node_modules":
            continue
        target_type = detect_target(child)
        if target_type == "unknown":
            continue
        items.append({
            "name": child.name,
            "description": "",
            "version": "",
            "source": child.name,
            "path": str(child),
            "target_type": target_type,
        })
        if target_type == "plugin":
            items.extend(_expand_plugin_skills(child.name, child, child.name))
    return items


def list_marketplace_plugins(db, market_id: int) -> List[Dict[str, Any]]:
    """列出市场中可评测的插件/skill（评测对象候选）

    顺序：marketplace.json 声明顺序，插件条目后紧跟其 skill 级条目
    （插件级仅静态层、skill 级可深度评，两类都要能选）。
    """
    market = _get_market(db, market_id)
    if market.sync_status != "ok" or not market.local_path:
        raise MarketplaceError(f"市场尚未同步成功，请先同步: {market.name}")
    local_path = Path(market.local_path)
    if not local_path.is_dir():
        raise MarketplaceError(f"市场本地目录缺失，请重新同步: {market.local_path}")

    items = _list_from_marketplace_json(local_path)
    if not items:
        items = _list_by_scan(local_path)
    # 剔除不可评测对象（保留声明顺序，不打乱插件与其 skill 的分组）
    return [i for i in items if i["target_type"] != "unknown"]


def _lazy_min_interval() -> float:
    """节流窗口（秒）：PLUGIN_MARKET_LAZY_SYNC_INTERVAL 覆盖，非法值回落默认"""
    raw = os.environ.get("PLUGIN_MARKET_LAZY_SYNC_INTERVAL")
    if raw is None or not str(raw).strip():
        return float(LAZY_SYNC_MIN_INTERVAL)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(LAZY_SYNC_MIN_INTERVAL)


def _seconds_since(ts: Optional[datetime]) -> Optional[float]:
    """距 ts 的秒数；ts 为 None 返回 None

    naive 时间按 UTC 解释（sqlite 测试库无时区信息；PG 的 DateTime(timezone=True)
    读回的是带 tz 的 UTC）。
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def refresh_marketplace_if_stale(
    db, market: PluginMarketplace, min_interval: Optional[float] = None
) -> Dict[str, Any]:
    """评测期懒加载：距上次成功同步超过节流窗口才 fetch 到远端最新

    失败降级：拉取失败不抛错、不改动 sync_status/sync_error（显式同步的失败态由
    sync_marketplace 负责），返回 stale=True 让调用方带着本地副本继续评测并在结果
    里留痕；只有本地副本本身缺失且克隆失败时才由调用方的目录校验拒绝。
    返回 {"refreshed", "stale", "age_seconds", "error"}。
    """
    interval = _lazy_min_interval() if min_interval is None else max(0.0, float(min_interval))
    age = _seconds_since(market.last_synced_at)
    local_ready = bool(market.local_path) and (Path(market.local_path) / ".git").exists()

    if local_ready and age is not None and age < interval:
        return {"refreshed": False, "stale": False, "age_seconds": age, "error": ""}

    try:
        _sync_local_copy(db, market)
    except MarketplaceError as e:
        logger.warning(f"[插件市场] 评测前同步失败，降级用本地副本（市场 {market.id}）: {e}")
        return {"refreshed": False, "stale": True, "age_seconds": age, "error": str(e)}

    market.sync_status = "ok"
    market.sync_error = None
    market.last_synced_at = func.now()
    db.commit()
    db.refresh(market)
    logger.info(f"[插件市场] 评测前懒加载同步完成：市场 {market.id}({market.name})")
    return {"refreshed": True, "stale": False, "age_seconds": 0.0, "error": ""}


def prepare_market_plugin_for_eval(
    market_id: int, source: str, min_interval: Optional[float] = None
) -> Dict[str, Any]:
    """评测执行期入口：懒加载同步 → 路径解析 → 版本锚点

    market_id + source 相对路径 → 本地插件目录绝对路径（防目录穿越），并在评测
    开始前把本地副本更新到远端最新（节流 + 失败降级，见 refresh_marketplace_if_stale）。
    市场不存在/未克隆/目录缺失/路径非法均抛 MarketplaceError。
    """
    db = SessionLocal()
    try:
        market = _get_market(db, market_id)
        refresh = refresh_marketplace_if_stale(db, market, min_interval=min_interval)

        if not market.local_path:
            raise MarketplaceError(f"市场尚未克隆: {market.name}")
        local_path = Path(market.local_path)
        if not local_path.is_dir():
            raise MarketplaceError(f"市场本地目录缺失，请先同步市场: {market.name}")
        target = _safe_resolve(local_path, source)
        if target is None:
            raise MarketplaceError(f"市场内未找到插件目录「{source}」，请重新同步市场后重试")

        return {
            "path": str(target),
            "market_id": market.id,
            "market_name": market.name,
            "branch": market.branch or "",
            "source": source,
            "commit": _head_commit(local_path),
            "stale": refresh["stale"],
            "refreshed": refresh["refreshed"],
            "refresh_error": refresh["error"],
            "last_synced_at": market.last_synced_at.isoformat() if market.last_synced_at else None,
        }
    finally:
        db.close()
