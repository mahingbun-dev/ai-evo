"""插件评测手动指定来源的上传落盘服务

接收 zip 插件包或 SKILL.md 单文件（multipart 上传）：
- .zip：内存解包（复用 pre-judge 脚本包的归一化校验：路径穿越拒绝、文件数/
  单文件/总量限制、GitHub 式唯一顶层目录剥离）后落盘到
  uploads/plugin_eval/{ts}_{uuid}_{name}/，校验根级评测对象（SKILL.md 或
  .claude-plugin/plugin.json），返回可执行的插件目录绝对路径——执行链路与
  「插件路径」手动填写完全一致
- .md：读取文本返回内联内容（前端回填内联 SKILL.md 文本域）

上传目录不做自动清理：评测对象目录在任务执行期间必须稳定存在，与
hub 拉取缓存（任务结束即清理）语义不同；目录积累由运维按需清理。
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path

from app.services.plugin_eval.parser import detect_target
from app.services.pre_judge_script_service import normalize_package

logger = logging.getLogger(__name__)

# zip 上传总量上限（解包前的原始文件大小）
MAX_UPLOAD_SIZE = 20 * 1024 * 1024
# 单个 SKILL.md 文本上限
MAX_SKILL_MD_SIZE = 2 * 1024 * 1024

_PLUGIN_UPLOAD_DIR = os.path.join(os.getcwd(), "uploads", "plugin_eval")


def upload_root() -> str:
    """上传落盘根目录（函数化便于测试 monkeypatch）"""
    return _PLUGIN_UPLOAD_DIR


def save_plugin_upload(filename: str, content: bytes) -> dict:
    """保存上传的插件包/SKILL.md，返回结果 dict（含 error 时由 API 层转 400）"""
    name = str(filename or "").strip()
    lower = name.lower()
    if lower.endswith(".zip"):
        return _save_zip(name, content)
    if lower.endswith(".md") or lower.endswith(".markdown"):
        return _save_md(name, content)
    return {"error": "仅支持 .zip 插件包或 SKILL.md（.md）文件"}


# ---------------------------------------------------------------------------
# zip 插件包
# ---------------------------------------------------------------------------

def _save_zip(name: str, content: bytes) -> dict:
    files_map, source_name, error = normalize_package([(name, content)])
    if error:
        return {"error": error}
    if not files_map:
        return {"error": "zip 内没有文件"}

    root_prefix = _resolve_root_prefix(files_map)
    if root_prefix is None:
        return {
            "error": (
                "zip 根级需包含 SKILL.md 或 .claude-plugin/plugin.json"
                "（请直接压缩插件或 skill 目录本身）"
            )
        }

    # 落盘：{ts}_{uuid8}_{安全名}/
    safe_name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(source_name or "").strip())[:60] or "plugin"
    target = Path(upload_root()) / f"{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe_name}"

    file_count = 0
    try:
        for rel, data in files_map.items():
            rel = rel[len(root_prefix):] if root_prefix else rel
            if not rel:
                continue
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            file_count += 1
    except OSError as e:
        # 半落盘目录清理，避免残留不可用目录
        shutil.rmtree(target, ignore_errors=True)
        logger.warning(f"[插件上传] zip 落盘失败: {e}")
        return {"error": f"插件包落盘失败: {e}"}

    target_type = detect_target(target)
    return {
        "kind": "plugin_dir",
        "path": str(target.resolve()),
        "name": safe_name,
        "target_type": target_type,
        "file_count": file_count,
    }


def _resolve_root_prefix(files_map: dict) -> str | None:
    """定位评测对象根：根级直接命中；否则唯一的一级子目录命中时下钻一层。

    返回需剥离的路径前缀（"" 表示根级即对象根），None 表示找不到评测对象。
    """
    def _is_object_root(keys) -> bool:
        return "SKILL.md" in keys or ".claude-plugin/plugin.json" in keys

    if _is_object_root(files_map.keys()):
        return ""

    # 根级未命中：按一级子目录分组，仅当唯一子目录是对象根时下钻
    # （覆盖「压缩了包含单个 skill/plugin 的外层文件夹」的常见形态）
    subdirs = {}
    for key in files_map:
        parts = key.split("/", 1)
        if len(parts) == 2:
            subdirs.setdefault(parts[0], []).append(parts[1])
    for subdir, rest_keys in subdirs.items():
        if _is_object_root(rest_keys):
            return f"{subdir}/"
    return None


# ---------------------------------------------------------------------------
# 内联 SKILL.md
# ---------------------------------------------------------------------------

def _save_md(name: str, content: bytes) -> dict:
    if len(content) > MAX_SKILL_MD_SIZE:
        return {"error": f"SKILL.md 超限（>{MAX_SKILL_MD_SIZE // (1024 * 1024)}MB）"}
    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        return {"error": "SKILL.md 内容为空"}
    stem = Path(name).stem or "SKILL"
    return {
        "kind": "skill_md",
        "name": stem,
        "content": text,
    }
