"""受限读取上传文件内容（W1-1：RCE 链整改——上传大小上限）

替代无界的 `await file.read()`：分块读取，累计超过上限立即中止并抛 413，
避免超大请求把进程内存打爆。
"""
from typing import Final

from fastapi import HTTPException, UploadFile

# 分块大小（1MB）：边读边累计，超限即刻失败
_CHUNK_SIZE: Final[int] = 1024 * 1024

__all__ = ["read_limited"]


def _human_size(max_bytes: int) -> str:
    mb = max_bytes // (1024 * 1024)
    return f"{mb}MB" if mb else f"{max_bytes}B"


async def read_limited(file: UploadFile, max_bytes: int) -> bytes:
    """分块读取上传文件内容，总大小超过 max_bytes 即抛 413

    读满上限即刻中止（不把整个文件读进内存才发现超限）；
    恰好等于上限的字节数放行。
    """
    if max_bytes <= 0:
        raise ValueError("read_limited 的 max_bytes 必须为正整数")
    chunks = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"文件过大（超过上限 {_human_size(max_bytes)}）",
            )
        chunks.append(chunk)
    return b"".join(chunks)
