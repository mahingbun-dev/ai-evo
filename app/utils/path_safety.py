"""路径与 URL 安全工具（供上传落盘、zip 解压、外呼请求等场景复用）"""
import errno
import os
import re
from urllib.parse import urlparse


class PathEscapeError(ValueError):
    """目标路径逃出允许的根目录"""


# ARCH-0-3：运行时目录统一显式权限（属主 rwx、组 rx、其他无）——uploads 下
# script_runs / hub_files 缓存 / hub_datasets 解包区 / hub_supplements 补传区
# 等目录创建一律用该权限，与容器内非 root 运行用户（10002 属主自用）口径一致
RUNTIME_DIR_MODE = 0o750


def sanitize_upload_filename(name: str, fallback: str = "upload.bin") -> str:
    """把客户端提供的文件名压成纯 basename（反斜杠也按分隔符处理），拒绝穿越"""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    return base or fallback


def sanitize_upload_ext(name: str, default: str = "") -> str:
    """从客户端文件名里提取安全的扩展名（.字母数字，≤10 位），不安全返回 default"""
    ext = os.path.splitext(str(name or ""))[1].lower()
    return ext if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext) else default


def ensure_within(root: str, *parts) -> str:
    """safe join：拼接后校验 realpath 仍在 root 内，逃出即抛 PathEscapeError

    用于 zip 解压、file_id/relative_path 拼路径等「根目录 + 不可信子路径」场景，
    与上游的成员名校验互为冗余防线（symlink、编码变形等由 realpath 兜底）。
    """
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, *(str(p) for p in parts)))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise PathEscapeError(f"路径越界（逃出 {root_real}）: {target}")
    return target


def secure_mkdir(path, mode: int = RUNTIME_DIR_MODE) -> str:
    """按显式 mode 幂等创建目录（含全部缺失的中间层级），返回路径字符串

    ARCH-0-3 目录权限收紧：uploads 下 script_runs / hub_files / hub_datasets /
    hub_supplements 等运行时目录的创建统一走本函数——
    - os.makedirs 的 mode 只作用于叶子目录，中间层级走进程 umask 缺省权限；
      这里自最深已存在的父级向下逐级 mkdir，本次新建的每一级都 chmod 到
      精确显式位（不受进程 umask 收窄影响）
    - 幂等：已存在的目录层级不重设权限（避免对他人属主目录越权 chmod）；
      目标已存在但不是目录时抛 FileExistsError（与 os.mkdir exist_ok 同语义）
    """
    target = os.fspath(path)
    # 自目标向上收集缺失层级（含目标自身），到文件系统根为止
    missing = []
    probe = target
    while probe and not os.path.exists(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    # 自最深已存在层级向下逐级创建（并发竞态：他人已建好则跳过该层）
    for level in reversed(missing):
        try:
            os.mkdir(level, mode)
        except FileExistsError:
            if not os.path.isdir(level):
                raise
            continue
        # mkdir 的 mode 会被进程 umask 收窄（如 027/077 umask 下得到 0750/0700），
        # chmod 强制精确位——显式权限不靠 umask 运气
        os.chmod(level, mode)
    if not os.path.isdir(target):
        raise FileExistsError(errno.EEXIST, "目标已存在但不是目录", target)
    return target


def ensure_http_url(url: str) -> str:
    """校验外呼 URL 仅允许 http/https scheme（阻断 file:/ftp:/gopher: 等变形）"""
    scheme = urlparse(str(url or "")).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"仅允许 http/https URL，收到: {url!r}")
    return url
