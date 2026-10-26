"""会话 Trace 解析与插件识别（plugin-eval 历史评测的输入准备）

为什么需要本模块：plugin-eval 的历史（真实数据）评测要求输入**Claude 原始 JSONL**
（``--thread``），它按消息 uuid/parentUuid 与 tool_use/tool_result id 关联调用与返回、
划分任务轮次。ai-evo 数据集里的 ``session_messages`` 是尺寸控制后的会话记录
（丢 message uuid，且超预算时丢弃首尾之外的消息），实测用它重建线程会**系统性虚高**
（同一线程：原始 jsonl 离线 83.33 分 / 重建 100.0 分，失败证据被一并丢掉），
因此线程只能取自数据行 hub 通道里保留的原始包。

职责边界：
- 找线程：hub 镜像目录 → 嵌套 ``claude[-_]thread*.zip`` 或裸 ``.jsonl``
- 定插件：市场 plugin-eval 的 ``rules/history/*.yaml`` 决定可评插件集合；从线程里的
  ``/anc-xxx:skill`` 与 ``<command-name>/anc-xxx:skill</command-name>`` 调用识别本次
  执行属于哪个插件（与 plugin-eval ``invoked_skill`` 同形判定）
- 解析被测插件目录：复用市场清单（含懒加载后的本地克隆与 commit 锚点）

本模块只做输入准备与识别，不发起评测；识别结果不构成判定——线程是否真的属于该插件
由 plugin-eval 自己的 task_selectors 决定。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional

logger = logging.getLogger(__name__)

# 嵌套会话包命名兼容连字符/下划线（实测 claude-thread.zip，业务口径 claude_thread.zip），
# 与 dataset_api 的会话导入选取规则同口径
THREAD_ZIP_PATTERN = re.compile(r"claude[-_]thread.*\.zip$", re.IGNORECASE)
JSONL_SUFFIX = ".jsonl"

# hub 镜像目录通常很浅（包内一级），给足深度以容忍执行包的多层结构
MAX_SCAN_DEPTH = 6

# plugin-eval 历史规则目录：源码运行读插件根 rules/history，wheel 安装读包内资源
RULE_DIR_RELATIVE = ("rules/history", "src/plugin_eval/history_rulefiles")

# 调用识别正则：与 plugin-eval.history.phases.invoked_skill 同形
# （行首斜杠命令，或 Claude Code 会话里的 <command-name> 包装）
_SKILL_NAME = r"[a-z0-9][a-z0-9-]*"
_COMMAND_LINE = re.compile(rf"^\s*/({_SKILL_NAME}):({_SKILL_NAME})(?=\s|$)", re.MULTILINE)
_COMMAND_TAG = re.compile(rf"<command-name>/?({_SKILL_NAME}):({_SKILL_NAME})</command-name>")


class ThreadSource(NamedTuple):
    """线程文件来源（jsonl_path 可能位于嵌套 zip 的解压临时目录内）"""

    jsonl_path: str
    kind: str  # nested_zip | jsonl
    origin: str  # 相对 hub 镜像目录的来源路径


def _iter_files(root: Path, max_depth: int = MAX_SCAN_DEPTH) -> Iterable[Path]:
    """按深度受限递归列出文件（跳过隐藏目录与 __MACOSX 之类干扰项）"""
    base_depth = len(root.parts)
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__MACOSX"]
        if len(Path(current).parts) - base_depth >= max_depth:
            dirs[:] = []
        for name in files:
            if name.startswith("._"):
                continue
            yield Path(current) / name


def find_thread_file(package_dir: str, work_dir: str) -> Optional[ThreadSource]:
    """在 hub 镜像目录里定位会话线程 jsonl

    优先嵌套会话包（``claude[-_]thread*.zip``，包内 jsonl 即会话转录），
    其次裸 ``.jsonl``；同类多个候选取体积最大者。找不到返回 None。

    Args:
        package_dir: 行内 hub_file 字段被 resolve 后的镜像目录
        work_dir: 解压嵌套 zip 的工作目录（调用方创建与清理）
    """
    from app.services.hub_file_service import HubFileError, unpack_zip

    root = Path(package_dir)
    if not root.is_dir():
        return None

    candidates = [p for p in _iter_files(root) if THREAD_ZIP_PATTERN.search(p.name)]
    if candidates:
        chosen = max(candidates, key=lambda p: p.stat().st_size)
        nested_dir = os.path.join(work_dir, "thread_unpacked")
        try:
            files = unpack_zip(str(chosen), nested_dir)
        except (HubFileError, OSError) as e:
            logger.warning(f"[插件评测] 嵌套会话包解压失败（{chosen.name}）: {e}")
            return None
        jsonls = [Path(f["path"]) for f in files if str(f.get("rel_path", "")).lower().endswith(JSONL_SUFFIX)]
        if not jsonls:
            return None
        best = max(jsonls, key=lambda p: p.stat().st_size)
        return ThreadSource(
            jsonl_path=str(best),
            kind="nested_zip",
            origin=str(chosen.relative_to(root)),
        )

    loose = [p for p in _iter_files(root) if p.name.lower().endswith(JSONL_SUFFIX)]
    if not loose:
        return None
    best = max(loose, key=lambda p: p.stat().st_size)
    return ThreadSource(
        jsonl_path=str(best),
        kind="jsonl",
        origin=str(best.relative_to(root)),
    )


def build_artifact_dir(
    package_dir: str, work_dir: str, thread: Optional[ThreadSource] = None
) -> Optional[str]:
    """把执行包里**除会话线程之外**的文件镜像成产物目录，返回目录路径（无产物返回 None）

    为什么不直接传执行包镜像目录：线程已通过 ``--thread`` 传入，plugin-eval 的产物扫描
    会对目录递归解析，``.jsonl`` 命中多条 extractor 规则——整包当产物会让线程转录被当作
    「本轮产物」反复解析（拖慢，且让证据角色混淆：转录自身不是产物）。嵌套线程包同理。

    实现用硬链接（同卷零拷贝），跨卷/不支持时降级复制；镜像随 work_dir 清理。
    """
    import shutil

    base = Path(package_dir)
    skip_real = {os.path.realpath(thread.jsonl_path)} if thread else set()
    targets = [
        path
        for path in _iter_files(base)
        if os.path.realpath(path) not in skip_real
        and not THREAD_ZIP_PATTERN.search(path.name)
    ]
    if not targets:
        return None

    dest_root = Path(work_dir) / "artifacts"
    for path in targets:
        dest = dest_root / path.relative_to(base)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, dest)
        except OSError:
            shutil.copy2(path, dest)
    return str(dest_root)


def discover_rule_plugins(evaluator_package_root: str) -> List[str]:
    """候选插件集合：市场 plugin-eval 的历史规则文件名（不含扩展名）

    不硬编码业务线清单——plugin-eval 新增规则后自动纳入。目录不存在返回空表。
    """
    root = Path(evaluator_package_root)
    for relative in RULE_DIR_RELATIVE:
        directory = root / relative
        if not directory.is_dir():
            continue
        return sorted(p.stem for p in directory.glob("*.yaml") if p.is_file())
    return []


def _user_text_blocks(record: Any) -> Iterable[str]:
    """产出该 JSONL 记录里 user 侧的文本（口径同 plugin-eval read_history）"""
    if not isinstance(record, dict):
        return
    message = record.get("message")
    if not isinstance(message, dict):
        return
    role = record.get("type", message.get("role"))
    if role != "user":
        return
    content = message.get("content")
    if isinstance(content, str):
        yield content
        return
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            yield block["text"]


def detect_invoked_plugin(thread_path: str, candidates: Iterable[str]) -> Dict[str, Any]:
    """从线程识别本次执行属于哪个候选插件

    返回 {"plugin", "counts", "first_line", "session_id", "candidates"}。
    多候选取调用次数最多者，并列取最早出现者；无命中时 plugin 为空串。

    只做候选识别，不做「是否真的属于该插件」的判定：plugin-eval 的 task_selectors
    会自行决定能否定位任务。
    """
    allowed = {name for name in candidates if name}
    counts: Dict[str, int] = {}
    first_line: Dict[str, int] = {}
    session_id = ""

    with open(thread_path, "r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            record = _safe_json(stripped)
            if record is None:
                continue
            if not session_id:
                raw_session = record.get("sessionId")
                if isinstance(raw_session, str):
                    session_id = raw_session
            for text in _user_text_blocks(record):
                for pattern in (_COMMAND_TAG, _COMMAND_LINE):
                    for match in pattern.finditer(text):
                        plugin = match.group(1)
                        if plugin not in allowed:
                            continue
                        counts[plugin] = counts.get(plugin, 0) + 1
                        first_line.setdefault(plugin, number)

    plugin = ""
    if counts:
        plugin = min(counts, key=lambda name: (-counts[name], first_line[name]))
    return {
        "plugin": plugin,
        "counts": dict(sorted(counts.items())),
        "first_line": first_line.get(plugin),
        "session_id": session_id,
        "candidates": sorted(allowed),
    }


def _safe_json(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _manifest_plugin_name(directory: str) -> str:
    """读插件目录 manifest 的 name（读不到返回空串）"""
    manifest = Path(directory) / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return ""
    record = _safe_json(manifest.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(record, dict):
        return ""
    return str(record.get("name") or "")


def resolve_plugin_dir(
    market_id: int, plugin_name: str, branch: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """在市场（已懒加载到最新）里定位被测插件目录

    先用市场清单的条目名匹配，再按各插件目录 manifest 的 name 兜底核对
    （条目名与 manifest name 不一致时仍能命中）。未收录返回 None。
    ``branch`` 非空时从该分支的独立副本读取（首次会拉取该分支）。
    """
    from app.db.session import SessionLocal
    from app.services.plugin_marketplace_service import list_marketplace_plugins

    db = SessionLocal()
    try:
        items = list_marketplace_plugins(db, market_id, branch=branch)
    finally:
        db.close()

    plugins = [i for i in items if i.get("target_type") == "plugin"]
    for item in plugins:
        if item.get("name") == plugin_name:
            return dict(item)
    for item in plugins:
        if _manifest_plugin_name(str(item.get("path") or "")) == plugin_name:
            return dict(item)
    return None
