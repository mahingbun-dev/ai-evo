"""read_limited 单元测试（整改开发计划 W1-1 测试设计）

计划用例清单（§2 W1「测试设计」）：
- 超限抛 413、恰好等于上限通过
补充边界：低于上限通过、上限为 1 字节的粒度、非正上限 ValueError、
分块读语义（多次 read，不整读）、starlette 真实 UploadFile 集成。

独立测试角色编写；不修改实现代码。
"""
import asyncio
import io

import pytest
from fastapi import HTTPException, UploadFile

from app.utils.read_limited import read_limited

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class _SpyUploadFile:
    """最小上传文件替身：记录每次 read 的 size，验证分块读语义"""

    def __init__(self, data: bytes, filename: str = "upload.bin"):
        self.filename = filename
        self._buf = io.BytesIO(data)
        self.read_sizes = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._buf.read(size)


def _run(coro):
    return asyncio.run(coro)


class TestReadLimitedPlanCases:
    """计划点名的用例：超限 413 / 恰好等于上限通过"""

    def test_over_limit_raises_413(self):
        file = _SpyUploadFile(b"x" * 11)
        with pytest.raises(HTTPException) as exc:
            _run(read_limited(file, 10))
        assert exc.value.status_code == 413
        assert "10B" in exc.value.detail  # 上限随错误透出（可读）

    def test_exactly_at_limit_passes(self):
        """恰好等于上限：放行（边界含端点）"""
        file = _SpyUploadFile(b"x" * 10)
        content = _run(read_limited(file, 10))
        assert content == b"x" * 10

    def test_below_limit_passes(self):
        file = _SpyUploadFile(b"hello")
        content = _run(read_limited(file, 1024))
        assert content == b"hello"

    def test_empty_file_passes(self):
        file = _SpyUploadFile(b"")
        assert _run(read_limited(file, 10)) == b""


class TestReadLimitedBoundaries:
    def test_one_byte_granularity(self):
        """上限 1 字节：1 字节通过、2 字节 413"""
        assert _run(read_limited(_SpyUploadFile(b"a"), 1)) == b"a"
        with pytest.raises(HTTPException) as exc:
            _run(read_limited(_SpyUploadFile(b"ab"), 1))
        assert exc.value.status_code == 413

    def test_limit_crossing_chunk_boundary(self):
        """超限发生在分块边界上（内容跨多个 1MB 块）同样 413"""
        two_mb_plus = b"x" * (2 * 1024 * 1024 + 1)
        with pytest.raises(HTTPException) as exc:
            _run(read_limited(_SpyUploadFile(two_mb_plus), 2 * 1024 * 1024))
        assert exc.value.status_code == 413

    def test_non_positive_limit_rejected(self):
        """max_bytes 非正为编程错误：ValueError（不是 413）"""
        for bad in (0, -1):
            with pytest.raises(ValueError):
                _run(read_limited(_SpyUploadFile(b"x"), bad))

    def test_reads_in_chunks_not_all_at_once(self):
        """分块读语义：read 以块大小（1MB）多次调用，而非一次全读"""
        size = int(2.5 * 1024 * 1024)
        file = _SpyUploadFile(b"x" * size)
        content = _run(read_limited(file, 4 * 1024 * 1024))
        assert len(content) == size
        assert len(file.read_sizes) >= 3, "应分多次（1MB 块）读取，而非整读"

    def test_human_readable_limit_in_detail(self):
        file = _SpyUploadFile(b"x" * (1024 * 1024 + 1))
        with pytest.raises(HTTPException) as exc:
            _run(read_limited(file, 1024 * 1024))
        assert "1MB" in exc.value.detail


class TestReadLimitedWithRealUploadFile:
    def test_starlette_upload_file_integration(self):
        """与 starlette 真实 UploadFile 协作（读通道与类型一致）"""
        file = UploadFile(file=io.BytesIO(b"x" * 2048), filename="a.bin")
        content = _run(read_limited(file, 4096))
        assert content == b"x" * 2048

        file2 = UploadFile(file=io.BytesIO(b"x" * 2049), filename="b.bin")
        with pytest.raises(HTTPException) as exc:
            _run(read_limited(file2, 2048))
        assert exc.value.status_code == 413
