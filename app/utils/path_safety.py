"""路径与 URL 安全工具（供上传落盘、zip 解压、外呼请求等场景复用）"""
import os
import re
from urllib.parse import urlparse


class PathEscapeError(ValueError):
    """目标路径逃出允许的根目录"""


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


def ensure_http_url(url: str) -> str:
    """校验外呼 URL 仅允许 http/https scheme（阻断 file:/ftp:/gopher: 等变形）"""
    scheme = urlparse(str(url or "")).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"仅允许 http/https URL，收到: {url!r}")
    return url
