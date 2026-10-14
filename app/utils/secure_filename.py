"""上传文件名安全清洗（W1-1：RCE 链整改——路径穿越写防护）

上传接口落盘时一律用本模块清洗后的文件名拼路径，消除 `../../evil.py`
一类文件名把写入位置越出目标目录的风险。
"""
import os
import re

# 控制字符（含 \x00）：清洗后不允许出现在落盘文件名里
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# 清洗后为空（或仅剩 ./..）时的回退名
_FALLBACK_NAME = "upload.bin"

__all__ = ["secure_filename"]


def secure_filename(name: str) -> str:
    """清洗上传文件名，保证可安全用于 os.path.join 落盘

    规则（按序）：
    1. 剥离控制字符；
    2. 反斜杠归一为 `/`（兼容 Windows 路径形态）后取 basename——
       剥掉一切目录成分（`../../evil.py` → `evil.py`，`/tmp/x.py` → `x.py`）；
    3. 去除残余路径分隔符；
    4. 结果为空 / `.` / `..` 时回退 `upload.bin`。

    中文等正常字符原样保留；仅目录语义字符被清洗。
    """
    cleaned = _CONTROL_CHARS_RE.sub("", str(name or ""))
    base = os.path.basename(cleaned.replace("\\", "/"))
    base = base.replace("/", "").replace("\\", "")
    if base in ("", ".", ".."):
        return _FALLBACK_NAME
    return base
