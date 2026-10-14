"""
anc-hub file-store 文件服务（MCP streamable-HTTP 通道）

通过 anc-hub 的 MCP 端点访问 file-store：
- file-store__file_download 分块下载（服务端单块上限 1MB），落盘后 sha256 + size 双校验
- file_query 远端文件检索（名称子串/上传时间/上传者过滤，分页），驱动「从 anc-hub 导入」页签

协议顺序（参考 anc-gateway/scripts/hub-file-download.py 的 McpClient 成熟实现）：
    initialize → notifications/initialized → tools/call（携带 mcp-session-id header）→ DELETE 收尾
响应为 SSE 时取最后一个非空 data: 行；偶发空响应自动重试一次。

端点从环境变量 ANC_HUB_MCP_ENDPOINT 读取，缺省时回退 Settings（.env），均缺失时抛出中文错误。
TLS 证书默认严格校验；自签名内网端点需显式设 AIEVO_HUB_TLS_INSECURE=1 才跳过。
缓存目录 uploads/hub_files/{file_id}/：已存在且 sha256 校验通过则跳过下载。

数据集迭代新增（anc-hub 执行包导入）：
- unpack_zip_safely 安全解包（拒绝穿越/绝对路径/超限，嵌套 zip 原样保留）
- build_file_index / extract_hub_summary 解包产物索引与 attempt 摘要
- ensure_unpacked 幂等物化（登记表 hub_files 命中零下载）
- acquire_ref / release_ref 引用计数（归零清理解包目录与登记记录）
- hardlink_tree 同卷硬链接复制（失败降级 copy2）
- match_files 共享匹配器（阶段B执行 / 阶段D预览复用同一语义）
- match_files_detail 匹配明细（阶段B脚本输入映射 {path,rel_path,size,type}）
- resolve_hub_file_fields 阶段B重写：复用持久解包区 hardlink 镜像到任务目录，
  缺持久区抛 HubFileNotAvailable（数据项级失败，可定位 file_id）
- resolve_path_in_root 防穿越统一校验（realpath 后必须以解包根为前缀，
  覆盖 .. / 绝对路径 / 编码逃逸 / symlink 逃逸）
- match_files_in_index 基于 file_index 的纯函数匹配器（阶段D预览用，
  与执行时 match_files 的文件系统 glob 同语义）
- hub_match_preview 匹配预览：取数据集首条含 hub_file 字段样本的登记索引匹配
"""
import base64
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy import delete as sa_delete
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import DBAPIError as sa_DBAPIError
import urllib3

from app.core.config import get_settings
from app.utils.path_safety import ensure_within, secure_mkdir

# 内网自签证书跳过校验后的告警噪音只保留在 debug 场景，不刷业务日志
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# 环境变量名与参考缺省端点（仅用于错误提示，缺失时必须显式配置）
HUB_ENDPOINT_ENV = "ANC_HUB_MCP_ENDPOINT"
# 缺省端点为 plugin-hub（github.com/mahingbun-dev/plugin-hub）MCP 直连面的本地形态
DEFAULT_HUB_ENDPOINT = "http://127.0.0.1:8096/mcp"

# file-store 服务端单块上限 1MB
HUB_DOWNLOAD_CHUNK = 1024 * 1024
# 下载分块数保险上限（1MB/块 × 10240 = 10GB），防止服务端 eof 异常导致死循环
MAX_DOWNLOAD_CHUNKS = 10240

# zip 解压防护上限：anc-hub 服务端会把文件打包成 zip（如 原名__哈希.zip）再返回，
# 实测 anc-results 类打包可达千余条目；超限视为异常包（zip bomb），中止解压
MAX_UNPACK_ENTRIES = 20000
MAX_UNPACK_TOTAL_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

# zip 解压产物子目录名（落在 target_dir 下，与 zip 同缓存目录）
UNPACK_DIRNAME = "unpacked"

DOWNLOAD_TOOL = "file-store__file_download"
QUERY_TOOL = "file-store__file_query"

def _hub_sso_cookie_name() -> str:
    """承载平台登录态的 Cookie 名（HUB_SSO_COOKIE_NAME 配置；留空表示禁用 Cookie 通道）"""
    from app.core.config import get_settings
    return (get_settings().HUB_SSO_COOKIE_NAME or "").strip()


def extract_bearer_token(cookie_header: Optional[str]) -> Optional[str]:
    """从浏览器 Cookie 头提取平台登录态 Cookie 值，作为 anc-hub Bearer 凭证

    适用于 ai-evo 与平台同域部署的场景：用户登录平台后，浏览器对 ai-evo
    后端的请求自动携带平台 Cookie；其值即 anc-hub authz 中间件 kind=token
    直验通道所要求的登录态 token。Cookie 名经 HUB_SSO_COOKIE_NAME 配置。
    """
    cookie_name = _hub_sso_cookie_name()
    if not cookie_name or not cookie_header or not isinstance(cookie_header, str):
        return None
    for pair in cookie_header.split(";"):
        if "=" not in pair:
            continue
        key, _, value = pair.strip().partition("=")
        if key.strip() == cookie_name and value.strip():
            return value.strip()
    return None


def resolve_hub_bearer_token(request) -> Optional[str]:
    """
    解析本次请求可用的 anc-hub Bearer 凭证：
    1. 静态服务凭证 ANC_HUB_BEARER_TOKEN（推荐，非同域部署也可用）
    2. 浏览器 Cookie 中的平台登录态（同域部署兜底通道，HUB_SSO_COOKIE_NAME）
    """
    from app.core.config import get_settings
    static_token = (get_settings().ANC_HUB_BEARER_TOKEN or "").strip()
    if static_token:
        return static_token
    cookie_header = request.headers.get("cookie") if request is not None else None
    return extract_bearer_token(cookie_header)

# 数据集字段元数据中表示 hub_file 通道的 value_type（field_type 自由字符串列）
HUB_FIELD_TYPE = "hub_file"


def _hub_tls_insecure() -> bool:
    """是否跳过 anc-hub 端点的 TLS 证书校验（仅显式环境变量 opt-in，默认校验）"""
    return os.environ.get("AIEVO_HUB_TLS_INSECURE", "").strip().lower() in ("1", "true", "yes")

# MCP initialize 参数（与参考实现保持一致）
_MCP_INITIALIZE_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "ai-evo-hub-file", "version": "1.0.0"},
}


class HubFileError(Exception):
    """anc-hub 文件服务异常（消息均为中文，可直接透出给调用方）"""


class HubEndpointNotConfiguredError(HubFileError):
    """ANC_HUB_MCP_ENDPOINT 未配置"""


class HubFileNotAvailable(HubFileError):
    """执行包无持久解包区（未导入 / 已被清理），需重新导入后再评测

    数据集迭代阶段B：resolve_hub_file_fields 只复用登记表 hub_files 的
    持久解包区、不再发起在线下载，缺持久区属于确定的业务失败事实，
    调用方（评测任务）应把对应数据项标记失败（可定位到 file_id）。
    """


class HubRecordMissingError(HubFileError):
    """执行包未登记（或登记已被清理），阶段D预览端点映射 404"""


def get_hub_endpoint() -> str:
    """读取 anc-hub MCP 端点：os.environ 优先，缺省回退 Settings（.env），均缺失时抛出中文错误"""
    endpoint = (os.environ.get(HUB_ENDPOINT_ENV) or "").strip()
    if not endpoint:
        endpoint = (get_settings().ANC_HUB_MCP_ENDPOINT or "").strip()
    if not endpoint:
        raise HubEndpointNotConfiguredError(
            f"环境变量 {HUB_ENDPOINT_ENV} 未配置，无法访问 anc-hub 文件服务"
            f"（缺省端点形如 {DEFAULT_HUB_ENDPOINT}，请在部署环境配置后重试）"
        )
    return endpoint


def sanitize_hub_name(name: str, fallback: str = "file") -> str:
    """清理文件/目录名：仅保留 basename，去除路径分隔符与不可见字符"""
    base = os.path.basename(str(name or "").strip())
    # 去掉路径分隔符、控制字符等不安全字符
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip(". ")
    return cleaned or fallback


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class HubMcpClient:
    """最小化的 streamable-HTTP MCP 客户端：initialize → initialized → tools/call → DELETE

    响应体按 MCP/SSE 规范以 UTF-8 显式解码（requests 的 .text 在
    Content-Type 无 charset 时会退回 latin-1，中文 mojibake 中的 C1
    控制字节——如「先」UTF-8 的 0x85——会被 str.splitlines() 当行界，
    把 data 行在 JSON 字符串中途截断）；SSE 行界也只用规范定义的
    \\n / \\r\\n，不用 splitlines()（它额外认 U+0085 / U+2028 等 Unicode
    行界，同样的截断风险）。

    bearer_token：调用凭证（静态服务 token，或同域部署下用户浏览器的平台登录态 Cookie 值），
    以 Authorization: Bearer 头随全部请求发送，经 anc-hub authz 中间件
    验证后以该用户身份调用插件。
    """

    def __init__(self, endpoint: str, timeout: int = 120, bearer_token: Optional[str] = None):
        self.endpoint = endpoint
        self.timeout = timeout
        self.bearer_token = (bearer_token or "").strip() or None
        self.session_id: Optional[str] = None
        self._next_id = 0

    def _headers(self) -> Dict[str, str]:
        """统一请求头（含会话与 Bearer 凭证）"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def _post(self, msg: Dict[str, Any]):
        # TLS 证书校验默认开启；自签名内网端点可显式设 AIEVO_HUB_TLS_INSECURE=1 跳过
        # （原实现无条件 verify=False，存在中间人风险）
        resp = requests.post(
            self.endpoint,
            data=json.dumps(msg).encode("utf-8"),
            headers=self._headers(),
            timeout=self.timeout,
            verify=not _hub_tls_insecure(),
        )
        if resp.status_code >= 400:
            raise HubFileError(
                f"MCP 请求失败: HTTP {resp.status_code}: "
                f"{self._decode_body(resp)[:200]}"
            )
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        return resp

    @staticmethod
    def _decode_body(resp) -> str:
        """按规范以 UTF-8 解码响应体（见类 docstring：不能用 resp.text）"""
        return (resp.content or b"").decode("utf-8", errors="replace")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, notify: bool = False):
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if notify:
            self._post(msg)
            return None

        self._next_id += 1
        msg["id"] = self._next_id
        # 响应偶发为空，重试一次
        body = ""
        for attempt in (1, 2):
            body = self._decode_body(self._post(msg))
            if body.strip():
                break
            if attempt == 2:
                raise HubFileError(f"{method}: 服务器返回空响应（已重试一次）")

        # 响应为 SSE；流里有空的 data: 心跳行，取最后一个非空 data: 行。
        # 行拆分只用 \n（再 rstrip \r 兼容 \r\n）——splitlines() 会把
        # U+0085 等 Unicode 行界当换行，截断 data 行（见类 docstring）
        payload = None
        for line in body.split("\n"):
            line = line.rstrip("\r")
            if line.startswith("data:"):
                s = line[len("data:"):].strip()
                if s:
                    payload = s
        if payload is None:
            payload = body  # 兜底：纯 JSON 响应

        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as e:
            raise HubFileError(f"{method}: 响应解析失败: {e}（原始内容前 200 字符: {payload[:200]}）")
        if isinstance(obj, dict) and "error" in obj and obj["error"] is not None:
            raise HubFileError(f"{method}: JSON-RPC error: {obj['error']}")
        return obj.get("result", obj) if isinstance(obj, dict) else obj

    def close(self):
        """DELETE 会话收尾（静默失败）"""
        if not self.session_id:
            return
        try:
            requests.delete(
                self.endpoint,
                headers=self._headers(),
                timeout=30,
                verify=not _hub_tls_insecure(),
            )
        except Exception:
            pass

    def __enter__(self) -> "HubMcpClient":
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def _extract_tool_payload(result: Any) -> Dict[str, Any]:
    """从 CallToolResult 里取出 file-store 的 payload（并校验 isError / ok 标志）"""
    if not isinstance(result, dict):
        raise HubFileError("工具结果格式异常: " + json.dumps(result, ensure_ascii=False, default=str)[:500])
    if result.get("isError"):
        texts = [
            c.get("text", "")
            for c in result.get("content", []) if isinstance(c, dict) and c.get("type") == "text"
        ]
        raise HubFileError("anc-hub 工具返回错误: " + "\n".join(t for t in texts if t)[:2000])

    obj = result.get("structuredContent")
    if not isinstance(obj, dict):
        obj = None
        for c in result.get("content", []):
            if isinstance(c, dict) and c.get("type") == "text":
                try:
                    obj = json.loads(c["text"])
                    break
                except (json.JSONDecodeError, TypeError):
                    continue
    if not isinstance(obj, dict):
        raise HubFileError(
            "无法从工具结果解析 payload: " + json.dumps(result, ensure_ascii=False, default=str)[:500]
        )
    # anc-hub 登录闸门的拒绝卡（decision=reject）：message 是面向用户的中文
    # 提示（如「需要先建立登录身份……」），直接透出，别包一层 JSON
    if obj.get("decision") == "reject" or obj.get("status") == "rejected":
        raise HubFileError(
            "anc-hub 拒绝了本次调用: " + str(obj.get("message") or json.dumps(obj, ensure_ascii=False))[:500]
        )
    if isinstance(obj.get("payload"), dict):
        obj = obj["payload"]
    if not obj.get("ok"):
        raise HubFileError("anc-hub 工具返回失败: " + json.dumps(obj, ensure_ascii=False, default=str)[:500])
    return obj


def _safe_zip_rel_path(name: str) -> Optional[str]:
    """清洗 zip entry 名为安全的 "/" 分隔相对路径

    返回 None 表示条目不安全（绝对路径 / .. 穿越 / 纯目录壳），调用方应拒绝或跳过。
    """
    normalized = (name or "").replace("\\", "/")
    if not normalized:
        return None
    parts = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts) if parts else None


def _unpack_zip(zip_path: str, unpack_root: str) -> List[Dict[str, Any]]:
    """解压 zip 到 unpack_root（保留目录结构，不递归展开嵌套 zip）

    防护：zip entry 路径穿越（zip slip）与超限解压（zip bomb，按实际写出
    字节累计，不信任 entry 头里的 file_size）。畸形包中重复的同名 entry
    按 zip 顺序后者覆盖前者，清单按 rel_path 去重。返回解压出的文件清单。
    """
    files: Dict[str, Dict[str, Any]] = {}
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_UNPACK_ENTRIES:
            raise HubFileError(
                f"zip 条目数超过上限 {MAX_UNPACK_ENTRIES}（实际 {len(infos)}），中止解压"
            )
        for zi in infos:
            if zi.is_dir():
                continue
            rel = _safe_zip_rel_path(zi.filename)
            if rel is None:
                raise HubFileError(f"zip 条目路径不安全: {zi.filename!r}，中止解压")
            # 成员名校验之外再做 realpath 越界兜底（symlink/编码变形由 realpath 兜底）
            dest = ensure_within(unpack_root, *rel.split("/"))
            # ARCH-0-3：解包中间目录显式 0o750 权限创建
            secure_mkdir(os.path.dirname(dest))
            written = 0
            with zf.open(zi) as src, open(dest, "wb") as out:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    written += len(block)
                    total += len(block)
                    if total > MAX_UNPACK_TOTAL_SIZE:
                        raise HubFileError(
                            f"zip 解压总大小超过上限 {MAX_UNPACK_TOTAL_SIZE} 字节，中止解压"
                        )
                    out.write(block)
            files[rel] = {"path": dest, "rel_path": rel, "size": written}
    return list(files.values())


class HubFileService:
    """anc-hub file-store 文件下载与检索服务"""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        cache_root: Optional[str] = None,
        chunk_size: int = HUB_DOWNLOAD_CHUNK,
        timeout: int = 120,
        bearer_token: Optional[str] = None,
    ):
        self._endpoint = endpoint
        self.timeout = timeout
        # 调用凭证随全部 MCP 请求
        # 以 Authorization: Bearer 头发送，经 anc-hub authz 验证后以该用户
        # 身份调用 file-store；缺省匿名（登录闸门开启时会被拒并返回登录提示）
        self.bearer_token = (bearer_token or "").strip() or None
        # 单块上限以服务端为准（1MB），超出时收敛
        self.chunk_size = max(1, min(int(chunk_size), HUB_DOWNLOAD_CHUNK))
        self.cache_root = cache_root or os.path.join(os.getcwd(), "uploads", "hub_files")

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    def _cache_dir(self, file_id: str) -> str:
        return os.path.join(self.cache_root, sanitize_hub_name(file_id, fallback="file"))

    @staticmethod
    def _meta_path(target_dir: str) -> str:
        return os.path.join(target_dir, "meta.json")

    def _load_cache_hit(self, target_dir: str) -> Optional[Dict[str, Any]]:
        """缓存命中校验：meta.json 存在且文件 sha256/size 校验通过"""
        try:
            with open(self._meta_path(target_dir), "r", encoding="utf-8") as f:
                meta = json.load(f)
            filename = meta.get("filename")
            sha256 = meta.get("sha256")
            if not filename or not sha256:
                return None
            path = os.path.join(target_dir, filename)
            if not os.path.isfile(path):
                return None
            if meta.get("size") is not None and os.path.getsize(path) != meta["size"]:
                return None
            if _sha256_file(path) != sha256:
                return None
            return {
                "path": path,
                "filename": filename,
                "size": meta.get("size", os.path.getsize(path)),
                "sha256": sha256,
                "cached": True,
            }
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def download_to_file(self, file_id: str, dest_dir: Optional[str] = None) -> Dict[str, Any]:
        """分块下载 anc-hub 文件并落盘，sha256 + size 双校验

        Args:
            file_id: anc-hub file-store 文件 ID
            dest_dir: 落盘目录，缺省使用缓存目录 uploads/hub_files/{file_id}/
                      （缓存已存在且 sha256 校验通过时跳过下载）

        Returns:
            {path, filename, size, sha256, cached}

        Raises:
            HubFileError: 端点未配置 / 下载失败 / 校验不符
        """
        file_id = str(file_id or "").strip()
        if not file_id:
            raise HubFileError("file_id 不能为空")

        target_dir = dest_dir or self._cache_dir(file_id)
        hit = self._load_cache_hit(target_dir)
        if hit:
            logger.info(f"[hub_file] 缓存命中，跳过下载: file_id={file_id} path={hit['path']}")
            return hit

        # ARCH-0-3：hub_files 缓存目录显式 0o750 权限创建
        secure_mkdir(target_dir)
        # dest_dir 允许调用方指定，但落盘路径必须仍在该目录内（realpath 兜底）
        tmp_path = ensure_within(target_dir, f".download_{os.getpid()}.part")

        endpoint = self._endpoint or get_hub_endpoint()
        filename: Optional[str] = None
        try:
            with HubMcpClient(endpoint, timeout=self.timeout, bearer_token=self.bearer_token) as cli:
                cli.call("initialize", _MCP_INITIALIZE_PARAMS)
                cli.call("notifications/initialized", notify=True)

                offset = 0
                chunks = 0
                digest = hashlib.sha256()
                expected_sha: Optional[str] = None
                expected_size: Optional[int] = None
                with open(tmp_path, "wb") as f:
                    while True:
                        chunks += 1
                        if chunks > MAX_DOWNLOAD_CHUNKS:
                            raise HubFileError(
                                f"文件 {file_id} 分块数超过上限 {MAX_DOWNLOAD_CHUNKS}，中止下载"
                            )
                        res = cli.call("tools/call", {
                            "name": DOWNLOAD_TOOL,
                            "arguments": {
                                "file_id": file_id,
                                "offset": offset,
                                "max_bytes": self.chunk_size,
                            },
                        })
                        pl = _extract_tool_payload(res)
                        data = base64.b64decode(pl.get("data_base64") or "")
                        if pl.get("bytes_read") != len(data):
                            raise HubFileError(
                                f"文件 {file_id} 第 {chunks} 块长度不符: "
                                f"bytes_read={pl.get('bytes_read')}，解码后 {len(data)}"
                            )
                        f.write(data)
                        digest.update(data)
                        offset += len(data)
                        if pl.get("sha256"):
                            expected_sha = pl["sha256"]
                        if pl.get("size") is not None:
                            expected_size = pl["size"]
                        if pl.get("filename"):
                            filename = pl["filename"]
                        if pl.get("eof"):
                            break

                # 双校验：size + sha256（不符即失败，清理半成品）
                if expected_size is not None and expected_size != offset:
                    raise HubFileError(
                        f"文件 {file_id} 大小校验失败: 服务端 {expected_size}，落盘 {offset}"
                    )
                actual_sha = digest.hexdigest()
                if expected_sha and actual_sha != expected_sha:
                    raise HubFileError(
                        f"文件 {file_id} sha256 校验失败: 服务端 {expected_sha}，落盘 {actual_sha}"
                    )
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        safe_name = sanitize_hub_name(filename, fallback=file_id)
        final_path = ensure_within(target_dir, safe_name)
        os.replace(tmp_path, final_path)

        meta = {
            "file_id": file_id,
            "filename": safe_name,
            "size": offset,
            "sha256": digest.hexdigest(),
        }
        with open(self._meta_path(target_dir), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        logger.info(
            f"[hub_file] 下载完成: file_id={file_id} size={offset} sha256={meta['sha256']} path={final_path}"
        )
        return {
            "path": final_path,
            "filename": safe_name,
            "size": offset,
            "sha256": meta["sha256"],
            "cached": False,
        }

    # ------------------------------------------------------------------
    # 下载 + zip 解压（anc-hub 服务端打包兼容）
    # ------------------------------------------------------------------
    def _read_unpacked_manifest(self, target_dir: str, unpack_root: str) -> Optional[List[Dict[str, Any]]]:
        """读取 meta.json 中的解压清单；任一产物缺失/大小不符时返回 None（触发重新解压）"""
        try:
            with open(self._meta_path(target_dir), "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        entries = meta.get("unpacked_files")
        if not isinstance(entries, list) or not entries:
            return None
        files: List[Dict[str, Any]] = []
        for e in entries:
            if not isinstance(e, dict):
                return None
            rel, size = e.get("rel_path"), e.get("size")
            if not rel or not isinstance(rel, str) or size is None:
                return None
            safe_rel = _safe_zip_rel_path(rel)
            if safe_rel != rel:
                return None
            path = os.path.join(unpack_root, *rel.split("/"))
            if not os.path.isfile(path) or os.path.getsize(path) != size:
                return None
            files.append({"path": path, "rel_path": rel, "size": size})
        return files

    def _write_unpacked_manifest(self, target_dir: str, files: List[Dict[str, Any]]) -> None:
        """把解压清单写回 meta.json（失败仅告警，下次调用会重新解压）"""
        try:
            with open(self._meta_path(target_dir), "r", encoding="utf-8") as f:
                meta = json.load(f)
            meta["unpacked_files"] = [{"rel_path": f["rel_path"], "size": f["size"]} for f in files]
            with open(self._meta_path(target_dir), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[hub_file] 写入解压清单失败（下次将重新解压）: {e}")

    def download_and_extract(self, file_id: str, dest_dir: Optional[str] = None) -> Dict[str, Any]:
        """下载并落地最终可用文件：非 zip 原样返回；zip 自动解压（含 zip slip/bomb 防护）

        anc-hub file-store 下载得到的是服务端打包的 zip（如 原名__哈希.zip），
        内部可能是单个 jsonl，也可能是带目录结构的多文件（如 anc-results 打包）。

        Args:
            file_id: anc-hub file-store 文件 ID
            dest_dir: 落盘目录，缺省使用缓存目录 uploads/hub_files/{file_id}/

        Returns:
            {path, filename, files: [{path, rel_path, size}], size, sha256, cached}
            - path: 非 zip / 单条目 zip → 解出的文件路径；多条目 zip → 解压根目录
            - filename: 与 path 对应的展示名（多条目时为原始 zip 名）
            - files: 全部落地文件清单（非 zip 时为单元素）
        """
        info = self.download_to_file(file_id, dest_dir=dest_dir)
        base = {
            "size": info["size"],
            "sha256": info["sha256"],
            "cached": info["cached"],
        }
        path = info["path"]
        if not zipfile.is_zipfile(path):
            return {
                **base,
                "path": path,
                "filename": info["filename"],
                "files": [{"path": path, "rel_path": info["filename"], "size": info["size"]}],
            }

        target_dir = os.path.dirname(path)
        unpack_root = os.path.join(target_dir, UNPACK_DIRNAME)
        files = self._read_unpacked_manifest(target_dir, unpack_root)
        if files is None:
            # ARCH-0-3：解包根目录显式 0o750 权限创建
            secure_mkdir(unpack_root)
            try:
                files = _unpack_zip(path, unpack_root)
            except Exception:
                # 解压失败清理半成品：缓存场景下恶意/损坏包反复调用时不残留、
                # 不重复占用磁盘（下次调用重新解压或重新下载）
                shutil.rmtree(unpack_root, ignore_errors=True)
                raise
            if not files:
                shutil.rmtree(unpack_root, ignore_errors=True)
                raise HubFileError(f"文件 {info['filename']} 解压后没有任何文件")
            self._write_unpacked_manifest(target_dir, files)
            logger.info(
                f"[hub_file] zip 已解压: file_id={file_id} 条目={len(files)} 目录={unpack_root}"
            )

        if len(files) == 1:
            return {
                **base,
                "path": files[0]["path"],
                "filename": os.path.basename(files[0]["rel_path"]),
                "files": files,
            }
        return {
            **base,
            "path": unpack_root,
            "filename": info["filename"],
            "files": files,
        }

    # ------------------------------------------------------------------
    # 检索（file_query 透传）
    # ------------------------------------------------------------------
    def query_files(
        self,
        name_like: Optional[str] = None,
        uploaded_after: Optional[str] = None,
        uploaded_before: Optional[str] = None,
        uploader: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """调用 anc-hub file_query 检索文件，结果透传给调用方"""
        arguments: Dict[str, Any] = {}
        if name_like:
            arguments["name_like"] = name_like
        if uploaded_after:
            arguments["uploaded_after"] = uploaded_after
        if uploaded_before:
            arguments["uploaded_before"] = uploaded_before
        if uploader:
            arguments["uploader"] = uploader
        if page is not None:
            arguments["page"] = int(page)
        if page_size is not None:
            arguments["page_size"] = int(page_size)

        endpoint = self._endpoint or get_hub_endpoint()
        with HubMcpClient(endpoint, timeout=self.timeout, bearer_token=self.bearer_token) as cli:
            cli.call("initialize", _MCP_INITIALIZE_PARAMS)
            cli.call("notifications/initialized", notify=True)
            result = cli.call("tools/call", {"name": QUERY_TOOL, "arguments": arguments})
        return _extract_tool_payload(result)


# ---------------------------------------------------------------------------
# 评测取数路径：value_type=hub_file 的数据集字段自动落盘
# ---------------------------------------------------------------------------

def unpack_zip(zip_path: str, dest_dir: str) -> List[Dict[str, Any]]:
    """解压任意 zip 到 dest_dir（公开入口，含 zip slip / zip bomb 防护）

    供调用方解压嵌套 zip（如 anc-hub 会话打包内的 claude_thread.zip）复用，
    防护与清单语义同 download_and_extract 的内层解压。dest_dir 需不存在或为空，
    由调用方管理生命周期（如随临时 workdir 一并清理）。
    """
    # ARCH-0-3：解包目录显式 0o750 权限创建
    secure_mkdir(dest_dir)
    try:
        files = _unpack_zip(zip_path, dest_dir)
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    if not files:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HubFileError(f"zip {os.path.basename(zip_path)} 解压后没有任何文件")
    return files


def get_hub_file_fields(db, dataset_id: Optional[str]) -> List[str]:
    """读取数据集中 value_type=hub_file 的字段名列表（DatasetFieldMetadata.field_type == "hub_file"）"""
    if db is None or not dataset_id:
        return []
    from app.models.dataset_models import DatasetFieldMetadata

    rows = (
        db.query(DatasetFieldMetadata)
        .filter(
            DatasetFieldMetadata.dataset_id == dataset_id,
            DatasetFieldMetadata.field_type == HUB_FIELD_TYPE,
        )
        .all()
    )
    return [row.field_name for row in rows]


def resolve_hub_file_fields(
    data: Dict[str, Any],
    field_names: List[str],
    cache_root: Optional[str] = None,
    db=None,
) -> Dict[str, str]:
    """将 data 中 hub_file 字段的 file_id 值替换为持久解包区的本地镜像路径

    数据集迭代阶段B重写：不再在评测链路在线下载，只复用登记表 hub_files
    的持久解包区（导入时 ensure_unpacked 物化）：
    - file_id 有持久解包区 → hardlink_tree 镜像到 {cache_root}/{file_id}/
      （同卷零拷贝，失败降级复制），字段值就地替换为该目录路径；
      任务内同 file_id 重复出现时直接复用已有镜像（幂等）
    - 无持久解包区（未导入 / 状态 failed / 解包目录已被清理）→ 抛
      HubFileNotAvailable（中文提示含「请重新导入」），调用方应把该数据项
      标记失败；镜像落盘等其它异常保留原值并记日志（不阻断其余字段）
    （from-hub 会话导入等在线下载场景走 HubFileService.download_and_extract，
    与本函数分工共存）

    Args:
        data: 数据项字段字典（就地替换 hub_file 字段值）
        field_names: hub_file 类型字段名列表
        cache_root: 镜像落盘根目录（缺省 uploads/hub_files/）。评测链路传入
                    任务级子目录 uploads/hub_files/{task_id}/，实现同 file_id
                    任务内只镜像一次、任务结束随临时产物整体清理（§七.6），
                    且任务间互不干扰。
        db: 数据库会话（登记表 hub_files 查询；缺省抛中文配置错误）

    Returns:
        {field: 镜像目录路径}（仅成功替换的字段）

    Raises:
        HubFileNotAvailable: 任一字段无持久解包区（fail fast，携带 field/file_id）
    """
    resolved: Dict[str, str] = {}
    fields = [f for f in (field_names or []) if f]
    if not fields or not isinstance(data, dict):
        return resolved
    if db is None:
        raise HubFileError(
            "resolve_hub_file_fields 缺少数据库会话（db），无法查询执行包登记表"
        )

    root = cache_root or os.path.join(os.getcwd(), "uploads", "hub_files")
    for field in fields:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        file_id = value.strip()
        record = _query_record(db, file_id)
        unpack_dir = getattr(record, "unpack_dir", None) if record is not None else None
        if (
            record is None
            or record.unpack_status != "unpacked"
            or not unpack_dir
            or not os.path.isdir(unpack_dir)
        ):
            status = getattr(record, "unpack_status", None) if record is not None else "未登记"
            raise HubFileNotAvailable(
                f"字段 {field} 的执行包（file_id={file_id}）无持久解包区"
                f"（登记状态: {status}），请重新导入该执行包后再发起评测"
            )

        mirror_dir = os.path.join(root, sanitize_hub_name(file_id, fallback="hub_file"))
        try:
            mirror_ready = False
            try:
                mirror_ready = (
                    os.path.isdir(mirror_dir)
                    and next(os.scandir(mirror_dir), None) is not None
                )
            except OSError:
                # 目录正被并发删除（rmtree 竞态窗口）→ 按无镜像处理，走重建
                mirror_ready = False
            if mirror_ready:
                # 同任务同 file_id 已镜像：直接复用（幂等；hardlink 的镜像与
                # 持久解包区内容一致，重复镜像只是浪费 IO）
                pass
            else:
                if os.path.isdir(mirror_dir):
                    shutil.rmtree(mirror_dir, ignore_errors=True)  # 清掉空/半成品镜像
                hardlink_tree(unpack_dir, mirror_dir)
                # 镜像完成后校验非空（缺陷B4）：镜像过程中目录被并发清理会产出
                # 空目录/半成品，脚本按 path_pattern 误匹配 0 文件——明确报错
                # 走 except 分支（保留原值 + error 日志），不静默放行
                try:
                    mirror_nonempty = next(os.scandir(mirror_dir), None) is not None
                except OSError:
                    mirror_nonempty = False
                if not mirror_nonempty:
                    raise HubFileError(
                        f"镜像目录 {mirror_dir} 构建后为空（可能被并发任务清理）: file_id={file_id}"
                    )
            # 人工补传合并：原始包先、补传后——同名以补传版为准（hardlink_tree
            # 文件级合并，os.link 同名冲突降级 copy2 覆盖）；复用分支同样做增量
            # 合并，镜像构建后新增的补传文件也进入本次任务镜像。合并失败走
            # except 分支（保留原值 + error 日志），数据项按可定位错误失败
            supplement_dir = _supplement_dir_for(file_id)
            try:
                has_supplement = (
                    os.path.isdir(supplement_dir)
                    and next(os.scandir(supplement_dir), None) is not None
                )
            except OSError:
                has_supplement = False
            if has_supplement:
                hardlink_tree(supplement_dir, mirror_dir)
            data[field] = mirror_dir
            resolved[field] = mirror_dir
            logger.info(
                f"[hub_file] 字段已镜像到任务目录: field={field} file_id={file_id} "
                f"unpack_dir={unpack_dir} mirror={mirror_dir}"
            )
        except Exception as e:  # noqa: BLE001 - 镜像失败保留原值，由调用方按 continue 语义处理
            logger.error(
                f"[hub_file] 字段镜像失败（保留原值）: field={field} file_id={file_id} 错误: {e}"
            )
    return resolved


def cleanup_hub_files(cache_root: Optional[str] = None) -> None:
    """清理 hub 拉取缓存目录（任务结束随临时产物清理，设计文档五/七.6）"""
    root = cache_root or os.path.join(os.getcwd(), "uploads", "hub_files")
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
        logger.info(f"[hub_file] 已清理缓存目录: {root}")


# ---------------------------------------------------------------------------
# 数据集迭代：anc-hub 执行包安全解包 / 索引 / 摘要（阶段A）
# ---------------------------------------------------------------------------

# 安全解包限额（解压前按 zip 声明值预检，超限直接拒绝）
UNPACK_MAX_TOTAL = 2 * 1024 * 1024 * 1024   # 总解压量上限 2GB
UNPACK_MAX_FILES = 20000                    # 文件数上限
UNPACK_MAX_SINGLE = 500 * 1024 * 1024       # 单文件上限 500MB

# 执行包解压根目录（ensure_unpacked 落盘：uploads/hub_datasets/{file_id}/）
HUB_DATASETS_DIRNAME = "hub_datasets"

# 人工补传区根目录（uploads/hub_supplements/{file_id}/）：
# 补传文件与解包区物理隔离——解包区保持"可从 anc-hub 幂等重建"的纯净性，
# 重新导入/重新解压不动补传内容；评测镜像与树/匹配视图在读取时合并
HUB_SUPPLEMENTS_DIRNAME = "hub_supplements"

# 补传单文件上限（散文件流式落盘时边写边计数；zip 走 unpack_zip_safely
# 的 UNPACK_MAX_SINGLE 同值预检）
MAX_SUPPLEMENT_FILE_SIZE = UNPACK_MAX_SINGLE

# 文件索引分类后缀（小写匹配）
_TEXT_EXTS = {
    ".json", ".jsonl", ".txt", ".md", ".csv", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".py", ".js", ".ts", ".log", ".sql", ".sh", ".ini",
    ".toml", ".env", ".conf", ".cfg",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".rar", ".7z", ".bz2", ".xz"}

# attempt 执行包摘要的固定 schema 标识
HUB_ATTEMPT_SCHEMA = "anc_attempt_v1"


def _hub_datasets_root(datasets_root: Optional[str] = None) -> str:
    return datasets_root or os.path.join(os.getcwd(), "uploads", HUB_DATASETS_DIRNAME)


def _hub_supplements_root(supplements_root: Optional[str] = None) -> str:
    """人工补传区根目录（缺省 uploads/hub_supplements/，测试可 monkeypatch）"""
    return supplements_root or os.path.join(os.getcwd(), "uploads", HUB_SUPPLEMENTS_DIRNAME)


def _supplement_dir_for(file_id: str, supplements_root: Optional[str] = None) -> str:
    """单个执行包的人工补传目录（uploads/hub_supplements/{file_id}/）"""
    return os.path.join(
        _hub_supplements_root(supplements_root),
        sanitize_hub_name(str(file_id or ""), fallback="hub_file"),
    )


def _safe_member_name(name: str) -> str:
    """校验并归一化 zip 成员名为相对 posix 路径，拒绝穿越与绝对路径

    Returns:
        归一化后的相对路径（反斜杠统一按分隔符处理）

    Raises:
        HubFileError: 成员名含绝对路径 / 盘符 / .. 穿越时抛中文错误
    """
    raw = str(name or "")
    # 反斜杠按分隔符处理（zip 规范是 /，但恶意包可能用 \ 逃逸检查）
    normalized = raw.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or raw.startswith("/") or raw.startswith("\\") or re.match(r"^[A-Za-z]:", raw):
        raise HubFileError(f"zip 成员路径不安全（禁止绝对路径）: {raw}")
    if any(part == ".." for part in pure.parts):
        raise HubFileError(f"zip 成员路径不安全（禁止 .. 穿越）: {raw}")
    return normalized


def unpack_zip_safely(zip_path: str, dest_dir: str) -> Dict[str, int]:
    """安全解包 zip 到 dest_dir（仅解第一层，嵌套 zip 原样保留不解压）

    解压前按 zip 声明值预检（超限抛中文错误，一个文件都不落盘）：
    - 成员路径禁止绝对路径 / 盘符 / .. 穿越
    - 文件数 > UNPACK_MAX_FILES（20000）拒绝
    - 单文件 > UNPACK_MAX_SINGLE（500MB）拒绝
    - 总解压量 > UNPACK_MAX_TOTAL（2GB）拒绝

    Returns:
        {file_count: 实际落盘文件数, total_size: 落盘总字节数}
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise HubFileError(f"不是有效的 zip 文件: {os.path.basename(str(zip_path))}（{e}）")

    with zf:
        members = [info for info in zf.infolist() if not info.is_dir()]
        if len(members) > UNPACK_MAX_FILES:
            raise HubFileError(
                f"zip 文件数超过上限 {UNPACK_MAX_FILES}（实际 {len(members)}），拒绝解压"
            )
        for info in members:
            if info.file_size > UNPACK_MAX_SINGLE:
                raise HubFileError(
                    f"zip 成员 {info.filename} 大小 {info.file_size} 超过单文件上限"
                    f" {UNPACK_MAX_SINGLE}，拒绝解压"
                )
        total_declared = sum(info.file_size for info in members)
        if total_declared > UNPACK_MAX_TOTAL:
            raise HubFileError(
                f"zip 总解压量 {total_declared} 超过上限 {UNPACK_MAX_TOTAL}，拒绝解压"
            )

        # 路径安全校验通过后才允许落盘（校验失败时 dest_dir 不留任何文件）
        for info in zf.infolist():
            _safe_member_name(info.filename)

        # ARCH-0-3：解包根目录（uploads/hub_datasets/{file_id}/）显式 0o750 创建
        secure_mkdir(dest_dir)
        file_count = 0
        total_size = 0
        for info in zf.infolist():
            rel = _safe_member_name(info.filename)
            if rel.endswith("/"):
                # 目录成员：建目录即可
                secure_mkdir(ensure_within(dest_dir, *rel.split("/")[:-1]))
                continue
            parts = [p for p in rel.split("/") if p]
            # 成员名校验之外再做 realpath 越界兜底
            target = ensure_within(dest_dir, *parts) if parts else None
            if target is None:
                continue
            secure_mkdir(os.path.dirname(target))
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            file_count += 1
            total_size += os.path.getsize(target)

    return {"file_count": file_count, "total_size": total_size}


def _classify_file(rel_path: str) -> str:
    """按后缀把文件分类为 text/image/archive/binary"""
    ext = PurePosixPath(rel_path).suffix.lower()
    if ext in _TEXT_EXTS:
        return "text"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _ARCHIVE_EXTS:
        return "archive"
    return "binary"


def _nested_zip_index(path: str) -> Optional[List[Dict[str, Any]]]:
    """读取嵌套 zip 的 central directory 生成条目索引（不解压内容）

    只读 central directory（zipfile 打开时不解压任何成员）。
    坏 zip / 不可读时返回 None（索引仍保留该文件条目，不阻断整体索引构建）。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            return [
                {
                    "path": info.filename,
                    "size": info.file_size,
                    "compress_size": info.compress_size,
                }
                for info in zf.infolist()
                if not info.is_dir()
            ]
    except (zipfile.BadZipFile, OSError) as e:
        logger.warning(f"[hub_file] 嵌套 zip 索引读取失败: {path}（{e}）")
        return None


def _index_entry_for_file(abs_path: str, root: str) -> Optional[Dict[str, Any]]:
    """构造单文件的索引条目（build_file_index 与人工补传索引共用）

    Returns:
        {path: 相对 posix 路径, type: text|image|archive|binary, size: 字节,
         is_nested_zip: bool, [nested_index]: 嵌套 zip 条目列表}；
        abs_path 不是 root 下的文件时返回 None
    """
    p = Path(abs_path)
    if not p.is_file():
        return None
    rel = p.relative_to(Path(root)).as_posix()
    ext = PurePosixPath(rel).suffix.lower()
    is_nested_zip = ext == ".zip"
    item: Dict[str, Any] = {
        "path": rel,
        "type": _classify_file(rel),
        "size": p.stat().st_size,
        "is_nested_zip": is_nested_zip,
    }
    if is_nested_zip:
        item["nested_index"] = _nested_zip_index(abs_path)
    return item


def build_file_index(unpack_root: str) -> List[Dict[str, Any]]:
    """遍历解包目录构建文件索引（相对包根）

    每项: {path: 相对 posix 路径, type: text|image|archive|binary,
           size: 字节, is_nested_zip: bool, [nested_index]: 嵌套 zip 条目列表}
    嵌套 zip 只读 central directory 生成 nested_index，不解压内容。
    """
    root = Path(unpack_root)
    index: List[Dict[str, Any]] = []
    if not root.is_dir():
        return index
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        entry = _index_entry_for_file(str(path), str(root))
        if entry:
            index.append(entry)
    return index


def _normalize_case_statuses(case_results: Any) -> List[str]:
    """归一化 manifest.case_results 两种形态为 status 小写字符串列表（缺陷B1）

    - list 形态：[{case_id, status}, ...]，各项取 .get("status")（非 dict 项归一为空串，
      保留在结果中以维持 case_total=len 的原语义）
    - dict 形态（真实执行包实测）：{"TC_001": "failed", ...}，value 为 str 直接是
      status；value 为 dict 时取其 .get("status")
    - 其它类型（缺失/None/标量）返回 []（case_total 走 acceptance-map/case_list 回退）
    """
    statuses: List[str] = []
    if isinstance(case_results, list):
        for item in case_results:
            if isinstance(item, dict):
                statuses.append(str(item.get("status", "")).lower())
            else:
                statuses.append("")
    elif isinstance(case_results, dict):
        for value in case_results.values():
            if isinstance(value, str):
                statuses.append(value.strip().lower())
            elif isinstance(value, dict):
                statuses.append(str(value.get("status", "")).lower())
            else:
                statuses.append("")
    return statuses


def extract_hub_summary(unpack_root: str) -> Optional[Dict[str, Any]]:
    """从解包目录提取 attempt 执行包摘要（无 execution-manifest.json 时返回 None）

    读取包内 execution-manifest.json（case_results / scope）、
    acceptance-map.json（cases）、case_list.json 产出：
    {schema, execution_id, case_total, case_passed, case_failed, segment_count, file_count}
    """
    root = Path(unpack_root)
    manifest_path = next(iter(sorted(root.rglob("execution-manifest.json"))), None)
    if manifest_path is None:
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[hub_file] execution-manifest.json 读取失败: {manifest_path}（{e}）")
        return None
    if not isinstance(manifest, dict):
        return None

    # 真实 attempt 包的 case_results 存在两种形态，统一归一化为 status 字符串列表：
    # - list：[{case_id, status}, ...]（各取 .get("status")）
    # - dict：{"TC_001": "failed", ...}（value 为 str 直接是 status；为 dict 取 .get("status")）
    # 非法条目归一为空串（计入 case_total、不计 passed/failed，与原 list 语义一致）
    statuses = _normalize_case_statuses(manifest.get("case_results"))

    def _load_json(name: str) -> Any:
        p = next(iter(sorted(root.rglob(name))), None)
        if p is None:
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    # case_total 优先 manifest.case_results，缺失时回退 acceptance-map / case_list
    case_total = len(statuses)
    if case_total == 0:
        acceptance = _load_json("acceptance-map.json")
        if isinstance(acceptance, dict) and isinstance(acceptance.get("cases"), list):
            case_total = len(acceptance["cases"])
    if case_total == 0:
        case_list = _load_json("case_list.json")
        if isinstance(case_list, list):
            case_total = len(case_list)

    passed = sum(1 for s in statuses if s in ("passed", "pass", "success"))
    failed = sum(1 for s in statuses if s in ("failed", "fail", "error"))
    scope = manifest.get("scope")
    if isinstance(scope, list):
        segment_count = len(scope)
    elif isinstance(scope, dict):
        segment_count = len(scope.get("segments") or []) or 1
    else:
        segment_count = 0

    return {
        "schema": HUB_ATTEMPT_SCHEMA,
        "execution_id": manifest.get("execution_id"),
        "case_total": case_total,
        "case_passed": passed,
        "case_failed": failed,
        "segment_count": segment_count,
        "file_count": sum(1 for p in root.rglob("*") if p.is_file()),
    }


def _sole_top_dir_of_root(root: Path) -> Optional[str]:
    """返回解包根下唯一顶层目录名（无目录 / 多顶层 / 混有根级文件时返回 None）

    真实执行包 zip 常带一层 attempt-1/ 顶层目录，「单顶层目录自动拼接」（缺陷B2）
    依赖本函数判定解包根是否仅有这一个目录。
    """
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    if len(entries) != 1 or not entries[0].is_dir():
        return None
    return entries[0].name


def match_files(unpack_root: str, pattern: str) -> List[str]:
    """共享文件匹配器（pathlib glob 语义，相对包根）

    阶段B执行与阶段D预览共用本函数，保证两处匹配语义一致。
    返回命中文件的相对 posix 路径列表（按路径排序，仅文件不含目录）。

    单顶层目录自动拼接（缺陷B2）：真实执行包常带一层 attempt-1/ 顶层目录，
    用户直觉 pattern 相对 attempt 根（如 reports/allure_results/*.json）。
    原始 pattern 匹配为空、解包根下仅含一个顶层目录、且 pattern 首段不是该目录名时，
    自动以 {顶层目录}/{pattern} 再匹配一次（取非空结果）。

    Raises:
        HubFileError: 模式为空或含 .. 穿越（不允许逃出包根）
    """
    root = Path(unpack_root)
    pat = str(pattern or "").strip().lstrip("/")
    if not pat:
        raise HubFileError("文件匹配模式不能为空")
    if any(part == ".." for part in PurePosixPath(pat).parts):
        raise HubFileError(f"文件匹配模式不允许 .. 穿越: {pattern}")
    if not root.is_dir():
        return []

    def _fs_match(parts: List[str], raw: str) -> List[str]:
        """文件系统侧匹配：与索引版 _parts_match 同一套部件匹配器

        pathlib.Path.glob 的裸 '**'（模式以 '**' 结尾）在 Python 3.12 不匹配
        文件、3.13 起才包含文件——生产运行 3.12 时 '**' 类模式会漏配，且违背
        与 match_files_in_index 的「同语义」承诺（等价性测试在 CI 3.12 下实测
        抓到）。因此 '**' 结尾的模式统一走 walk + _parts_match（三方同语义：
        fs / index / 各 Python 版本），其余模式仍走原始模式串的 pathlib glob
        （保留尾随 '/' 等原生语义，各 Python 版本行为一致）。
        """
        if not parts or parts[-1] != "**":
            return sorted(
                p.relative_to(root).as_posix()
                for p in root.glob(raw)
                if p.is_file()
            )
        return sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and _parts_match(p.relative_to(root).parts, parts)
        )

    pat_parts = [p for p in pat.split("/") if p and p != "."]
    if pat_parts and pat_parts[-1] == "**" and pat.endswith("/"):
        # 与索引版同口径：尾随 '/' 在 pathlib 中只匹配目录，仅文件结果恒为空
        return []

    matched = _fs_match(pat_parts, pat)
    if matched:
        return matched
    # 原始 pattern 未命中：尝试单顶层目录自动拼接（与预览侧 match_files_in_index 同源）
    top = _sole_top_dir_of_root(root)
    if top and PurePosixPath(pat).parts and PurePosixPath(pat).parts[0] != top:
        retried = _fs_match([top] + pat_parts, f"{top}/{pat}")
        if retried:
            return retried
    return matched


def match_files_detail(unpack_root: str, pattern: str) -> List[Dict[str, Any]]:
    """match_files 的明细版：按 pattern 匹配并返回文件明细（阶段B脚本输入映射用）

    复用 match_files 的匹配语义（与阶段D预览一致），在解包区上匹配，
    每项: {path: 绝对路径, rel_path: 相对包根 posix 路径, size: 字节, type: 分类}。
    """
    root = Path(unpack_root)
    detail: List[Dict[str, Any]] = []
    for rel in match_files(unpack_root, pattern):
        target = root / rel
        detail.append({
            "path": str(target),
            "rel_path": rel,
            "size": target.stat().st_size,
            "type": _classify_file(rel),
        })
    return detail


def hardlink_tree(src_root: str, dst_root: str) -> Dict[str, int]:
    """把 src_root 文件树以硬链接方式镜像到 dst_root（同卷零拷贝）

    同卷 os.link 生效；链接失败（跨卷/文件系统不支持等 OSError）时
    降级 shutil.copy2，保证目标内容一致。

    并发清理防御（缺陷B4）：目标目录被并发删除时（如 celery 孤儿消息重投导致
    同任务两个 worker 竞态、一方在 finally 清理任务级缓存目录），先重建父目录
    重试一次；重试仍失败明确抛 HubFileError（中文错误），不静默产出半成品镜像
    （半成品会让脚本按 path_pattern 误匹配 0 文件）。

    Returns:
        {linked: 硬链接成功数, copied: 降级复制数}

    Raises:
        HubFileError: 重试后仍失败（源文件被清理 / 目标目录持续被并发删除）
    """
    src = Path(src_root)
    dst = Path(dst_root)
    linked = 0
    copied = 0
    if not src.is_dir():
        return {"linked": linked, "copied": copied}
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        target = dst / path.relative_to(src)
        last_error: Optional[BaseException] = None
        for attempt in (1, 2):
            try:
                # ARCH-0-3：镜像目录逐级显式 0o750 权限创建
                secure_mkdir(target.parent)
                os.link(str(path), str(target))
                linked += 1
                last_error = None
                break
            except OSError as link_err:
                try:
                    # 硬链接失败（跨卷/不支持）降级复制；复制时父目录被并发删除
                    # 会抛 FileNotFoundError（OSError 子类）→ 下一轮重建后重试
                    secure_mkdir(target.parent)
                    shutil.copy2(str(path), str(target))
                    copied += 1
                    last_error = None
                    break
                except OSError as copy_err:
                    last_error = copy_err or link_err
                    continue
        if last_error is not None:
            raise HubFileError(
                f"镜像文件失败（源或目标目录可能被并发清理）: {path} -> {target}（{last_error}）"
            )
    return {"linked": linked, "copied": copied}


# ---------------------------------------------------------------------------
# 数据集迭代：执行包登记表（hub_files）幂等物化与引用计数
# ---------------------------------------------------------------------------

def _new_record_id() -> str:
    return f"hub_{uuid.uuid4().hex[:12]}"


def _query_record(db, file_id: str):
    """按 file_id 查登记记录（模型延迟导入，避免模块加载顺序依赖）"""
    from app.models.hub_file_models import HubFileRecord

    return db.query(HubFileRecord).filter(HubFileRecord.file_id == file_id).first()


def _atomic_ref_update_stmt(file_id: str, delta: int):
    """ref_count 原子 ±1 语句（W11）：SQL 端单语句更新，消除读改写竞态丢更新

    并发场景（acks_late 重投 / 两个 job 导入同一包 / 删除联动释放与执行持引
    并发）：两 worker 同时读到旧值再各自回写会互相覆盖；改为
    `UPDATE hub_files SET ref_count = ref_count ± 1` 由行锁串行化，计数精确。
    自减用 CASE 下限 0，对齐原 max(0, ...) 语义；自增列本身 NOT NULL，无需
    COALESCE。synchronize_session=False：不走会话内对象同步（权威值由调用方
    回读 DB 取得，避免把过期值写回）。
    """
    from sqlalchemy import case as sa_case

    from app.models.hub_file_models import HubFileRecord

    if delta >= 0:
        expr = HubFileRecord.ref_count + delta
    else:
        expr = sa_case(
            (HubFileRecord.ref_count > 0, HubFileRecord.ref_count + delta), else_=0
        )
    return (
        sa_update(HubFileRecord)
        .where(HubFileRecord.file_id == file_id)
        .values(ref_count=expr)
        .execution_options(synchronize_session=False)
    )


def _zero_ref_delete_stmt(file_id: str):
    """归零条件删除语句（W11）：仅当 ref_count 仍为 0 才删，防与并发 acquire 竞态
    误清理他人刚持有的包（acquire 先于本删除发生时 ref_count 已 >0，删除不命中）"""
    from app.models.hub_file_models import HubFileRecord

    return (
        sa_delete(HubFileRecord)
        .where(HubFileRecord.file_id == file_id, HubFileRecord.ref_count == 0)
        .execution_options(synchronize_session=False)
    )


def _read_ref_count(db, file_id: str) -> int:
    """权威回读 ref_count（Core SELECT 绕过会话身份映射，取 DB 当前值）"""
    from app.models.hub_file_models import HubFileRecord

    value = db.execute(
        sa_select(HubFileRecord.ref_count).where(HubFileRecord.file_id == file_id)
    ).scalar()
    return int(value or 0)


def _execute_ref_stmt_with_retry(db, stmt, attempts: int = 3):
    """执行原子 ref 语句；瞬时 DBAPI 错误（连接抖动/死锁牺牲品）回滚后有限重试

    单语句事务在 READ COMMITTED 下无丢失更新，重试只覆盖连接类瞬时失败；
    语句可重入（重试前事务已整体回滚，不存在半提交导致的重复计数）。
    """
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return db.execute(stmt)
        except sa_DBAPIError as e:  # noqa: BLE001 - SQLAlchemy 包装的 DBAPI 瞬时错误
            last_err = e
            try:
                db.rollback()
            except Exception:  # noqa: BLE001 - 回滚失败以原始异常为准
                pass
            logger.warning(
                f"[hub_file] ref 原子更新第 {attempt}/{attempts} 次执行失败"
                f"（已回滚，{'重试' if attempt < attempts else '放弃'}）: {e}"
            )
            if attempt < attempts:
                time.sleep(0.05 * attempt)
    raise last_err


def _mark_failed(db, record, file_id: str, unpack_dir: str, message: str) -> None:
    """best-effort 登记失败状态（失败信息写入 unpack_error，不向调用方抛出）"""
    from app.models.hub_file_models import HubFileRecord

    try:
        rec = record
        if rec is None:
            rec = HubFileRecord(id=_new_record_id(), file_id=file_id)
            db.add(rec)
        rec.unpack_dir = unpack_dir
        rec.unpack_status = "failed"
        rec.unpack_error = (message or "未知错误")[:2000]
        db.commit()
    except Exception as e:  # noqa: BLE001 - 登记失败只记日志，不掩盖原始异常
        logger.error(f"[hub_file] 登记失败状态写入异常: file_id={file_id} 错误: {e}")
        try:
            db.rollback()
        except Exception:
            pass


def ensure_unpacked(
    db,
    file_id: str,
    *,
    datasets_root: Optional[str] = None,
    service: Optional["HubFileService"] = None,
    team_code: Optional[str] = None,
) -> Dict[str, Any]:
    """幂等物化执行包：登记表 unpacked 命中直接返回（零下载），未命中走完整流程

    完整流程：下载（sha256/size 双校验）→ 安全解包到
    {datasets_root 或 uploads/hub_datasets}/{file_id}/ → 建索引 → 提摘要 → 写登记表。

    Args:
        db: 数据库会话（登记表读写）
        file_id: anc-hub file-store 文件 ID
        datasets_root: 解包根目录（缺省 uploads/hub_datasets/，测试注入临时目录）
        service: HubFileService 实例（缺省按端点配置新建，测试可注入 mock 端点）
        team_code: W9 团队归属——仅在**新建登记记录**时写入（导入时从目标
                   数据集继承）；已有记录幂等命中不覆盖（保持首次导入团队的归属，
                   避免后导入团队改写他人正在引用的资源的归属）

    Returns:
        {file_id, unpack_dir, file_index, summary, cached, record_id}
        cached=True 表示登记表命中（本次未发起下载）
    """
    file_id = str(file_id or "").strip()
    if not file_id:
        raise HubFileError("file_id 不能为空")

    record = _query_record(db, file_id)
    if (
        record is not None
        and record.unpack_status == "unpacked"
        and record.unpack_dir
        and os.path.isdir(record.unpack_dir)
    ):
        return {
            "file_id": file_id,
            "unpack_dir": record.unpack_dir,
            "filename": record.filename,
            "file_index": record.file_index or [],
            "summary": extract_hub_summary(record.unpack_dir),
            "cached": True,
            "record_id": record.id,
        }

    unpack_dir = os.path.join(
        _hub_datasets_root(datasets_root), sanitize_hub_name(file_id, fallback="hub_file")
    )
    # 注意：缺省匿名构造仅供无生产调用方的兼容路径（backfill 等）；
    # 登录闸门开启时匿名下载会被拒——生产调用方应传入带 bearer_token 的 service
    svc = service or HubFileService()
    try:
        info = svc.download_to_file(file_id)
        # 重新解包前清理可能存在的半成品（failed / 上次中断的目录）
        if os.path.isdir(unpack_dir):
            shutil.rmtree(unpack_dir, ignore_errors=True)
        stats = unpack_zip_safely(info["path"], unpack_dir)
        file_index = build_file_index(unpack_dir)
    except Exception as e:
        _mark_failed(db, record, file_id, unpack_dir, str(e))
        raise

    summary = extract_hub_summary(unpack_dir)
    from app.models.hub_file_models import HubFileRecord

    if record is None:
        record = HubFileRecord(id=_new_record_id(), file_id=file_id)
        # W9：归属锚点——新建登记时从目标数据集团队继承（None 落 DEFAULT 口径）
        if team_code and str(team_code).strip():
            record.team_code = str(team_code).strip()
        db.add(record)
    record.filename = info.get("filename")
    record.size = info.get("size")
    record.sha256 = info.get("sha256")
    record.unpack_dir = unpack_dir
    record.unpack_status = "unpacked"
    record.unpack_error = None
    record.file_count = stats["file_count"]
    record.total_size = stats["total_size"]
    record.file_index = file_index
    db.commit()
    logger.info(
        f"[hub_file] 执行包已解包: file_id={file_id} dir={unpack_dir} "
        f"files={stats['file_count']} summary={'有' if summary else '无'}"
    )
    return {
        "file_id": file_id,
        "unpack_dir": unpack_dir,
        "filename": info.get("filename"),
        "file_index": file_index,
        "summary": summary,
        "cached": False,
        "record_id": record.id,
    }


def acquire_ref(db, file_id: str) -> int:
    """执行包引用计数 +1（执行/预览开始持有时调用）

    W11 原子化：UPDATE ... SET ref_count = ref_count + 1（SQL 端单语句），
    多 worker 并发持有同一包时不再发生读改写丢更新；预检查仅用于给出
    「未登记」的明确错误，真正的存在性以 UPDATE 影响行数为准（TOCTOU 兜底）。

    Returns:
        加一后的引用计数（并发下为 DB 权威回读值，可能高于本地旧值+1）

    Raises:
        HubFileError: file_id 为空或未登记（需先 ensure_unpacked）
    """
    file_id = str(file_id or "").strip()
    if not file_id:
        raise HubFileError("file_id 不能为空")
    record = _query_record(db, file_id)
    if record is None:
        raise HubFileError(f"执行包未登记（file_id={file_id}），请先导入解包后再引用")
    result = _execute_ref_stmt_with_retry(db, _atomic_ref_update_stmt(file_id, +1))
    if getattr(result, "rowcount", 0) == 0:
        # 预检查后记录被并发归零清理（TOCTOU 兜底）
        db.rollback()
        raise HubFileError(f"执行包未登记（file_id={file_id}），请先导入解包后再引用")
    db.commit()
    return _read_ref_count(db, file_id)


def _hub_files_cache_root(cache_root: Optional[str] = None) -> str:
    """下载缓存根目录（缺省 uploads/hub_files/，与 HubFileService.cache_root 缺省一致）"""
    return cache_root or os.path.join(os.getcwd(), "uploads", "hub_files")


def release_ref(db, file_id: str) -> int:
    """执行包引用计数 -1；归零时清理解包目录、下载缓存目录与登记表记录

    W11 原子化：减一为 SQL 端单语句 UPDATE（下限 0），消除读改写竞态；归零
    清理改为「条件删除」（DELETE ... WHERE ref_count = 0）先行——并发 acquire
    抢先持有引用时删除不命中，跳过目录清理，避免误删他人刚持有的包。

    归零联动（小项1）：除解包目录 uploads/hub_datasets/{file_id}/ 外，
    同时清理下载缓存目录 uploads/hub_files/{file_id}/（导入时 download_to_file
    落盘的原始 zip 与 meta.json）。该目录此前无清理方（任务级清理只删
    uploads/hub_files/{task_id}/ 任务子目录），登记删除后永久残留。

    Returns:
        归还后的剩余引用计数（0 表示已清理或已被并发方清理）

    Raises:
        HubFileError: file_id 为空或未登记
    """
    file_id = str(file_id or "").strip()
    if not file_id:
        raise HubFileError("file_id 不能为空")
    record = _query_record(db, file_id)
    if record is None:
        raise HubFileError(f"执行包未登记（file_id={file_id}），无法释放引用")
    # unpack_dir 在任何 commit 前取值：expire_on_commit 下实例过期，条件删除
    # 落库后再访问属性会触发刷新并抛 ObjectDeletedError（行已删）
    unpack_dir = record.unpack_dir
    result = _execute_ref_stmt_with_retry(db, _atomic_ref_update_stmt(file_id, -1))
    if getattr(result, "rowcount", 0) == 0:
        # 预检查后记录被并发归零清理（TOCTOU 兜底）
        db.rollback()
        raise HubFileError(f"执行包未登记（file_id={file_id}），无法释放引用")
    db.commit()
    remaining = _read_ref_count(db, file_id)
    if remaining > 0:
        return remaining
    # 归零：条件删除（仅当 ref_count 仍为 0 才删）→ 删除命中才清理解包目录、
    # 下载缓存目录与人工补传目录（补传内容跟包走，包被彻底清理时一并删除）
    deleted = getattr(
        db.execute(_zero_ref_delete_stmt(file_id)), "rowcount", 0
    )
    db.commit()
    if deleted:
        if unpack_dir and os.path.isdir(unpack_dir):
            shutil.rmtree(unpack_dir, ignore_errors=True)
            logger.info(f"[hub_file] 引用归零，已清理解包目录: {unpack_dir}")
        # 归零联动清理下载缓存目录（原始 zip 落盘处，见类 docstring 小项1说明）
        cache_dir = os.path.join(
            _hub_files_cache_root(), sanitize_hub_name(file_id, fallback="hub_file")
        )
        if os.path.isdir(cache_dir):
            shutil.rmtree(cache_dir, ignore_errors=True)
            logger.info(f"[hub_file] 引用归零，已清理下载缓存目录: {cache_dir}")
        supplement_dir = _supplement_dir_for(file_id)
        if os.path.isdir(supplement_dir):
            shutil.rmtree(supplement_dir, ignore_errors=True)
            logger.info(f"[hub_file] 引用归零，已清理人工补传目录: {supplement_dir}")
    return 0


def collect_row_hub_file_ids(data: Dict[str, Any], field_names: List[str]) -> List[str]:
    """从数据行 data JSON 中按 hub_file 字段名收集非空 file_id 值（行内去重保序）

    数据集迭代阶段C：删除数据集/数据行前收集引用，供 release_hub_refs 释放。
    同一行内同一 file_id 出现在多个 hub_file 字段时只收集一次
    （导入时每行每包只 acquire 一次，释放粒度与之对齐）。
    """
    ids: List[str] = []
    if not isinstance(data, dict):
        return ids
    for field in field_names or []:
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            fid = value.strip()
            if fid not in ids:
                ids.append(fid)
    return ids


def release_hub_refs(db, file_ids: List[str]) -> None:
    """批量释放执行包引用（best-effort）：单个失败不影响其余与调用方主流程

    末引用（ref_count→0）由 release_ref 清理解包目录、下载缓存目录与登记记录。
    """
    for fid in file_ids or []:
        if not isinstance(fid, str) or not fid.strip():
            continue
        try:
            remaining = release_ref(db, fid.strip())
            logger.info(f"[hub_file] 数据删除联动释放引用: file_id={fid.strip()} 剩余引用={remaining}")
        except Exception as e:  # noqa: BLE001 - 删除主流程不因引用释放失败而中断
            logger.warning(f"[hub_file] 释放引用失败（跳过）: file_id={fid} 错误: {e}")


def discard_unpacked_if_unreferenced(db, file_id: str) -> bool:
    """丢弃未持有任何引用的登记记录及其解包/下载缓存目录

    导入链路校验失败时的回滚清理：仅当记录存在且 ref_count==0 时清理
    （被其他数据集持有的登记不动）。Returns: 是否执行了清理。
    """
    file_id = str(file_id or "").strip()
    if not file_id:
        return False
    record = _query_record(db, file_id)
    if record is None or int(record.ref_count or 0) != 0:
        return False
    if record.unpack_dir and os.path.isdir(record.unpack_dir):
        shutil.rmtree(record.unpack_dir, ignore_errors=True)
    cache_dir = os.path.join(
        _hub_files_cache_root(), sanitize_hub_name(file_id, fallback="hub_file")
    )
    if os.path.isdir(cache_dir):
        shutil.rmtree(cache_dir, ignore_errors=True)
    # 人工补传目录与解包区同生命周期（归零清理；重新导入不清理补充区）
    supplement_dir = _supplement_dir_for(file_id)
    if os.path.isdir(supplement_dir):
        shutil.rmtree(supplement_dir, ignore_errors=True)
    db.delete(record)
    db.commit()
    logger.info(f"[hub_file] 已丢弃未引用的登记记录: file_id={file_id}")
    return True


# ======================================================================
# 人工补传（执行包内容不足时，人工向包内补充文件）
#
# 语义（需求访谈拍板）：
# - 包级生效：补传内容跟 file_id 走，同一包的所有引用方（多行/多数据集）
#   镜像与树/匹配视图统一可见；file_index 落库保持纯原始，读取面叠加
# - 同名覆盖：补传文件压过原始包文件（评测镜像"原始包先、补传后"合并，
#   os.link 同名冲突降级 copy2 覆盖），重导新包后补传持续有效
# - 生命周期：ref_count 归零随包清理（release_ref/discard 联动）；重新
#   解压/重导不动补充区
# - 防护：zip slip 校验（复用 unpack_zip_safely）、单文件 ≤ MAX_SUPPLEMENT_
#   FILE_SIZE、补传区条目/总量沿用解包同款上限
# ======================================================================


def _clean_supplement_rel(rel_path: str) -> str:
    """清洗补传相对路径为安全 posix 相对路径（拒绝绝对路径 / 盘符 / .. 穿越）"""
    raw = str(rel_path or "")
    normalized = raw.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts:
        raise HubFileError(f"补传文件路径不能为空: {rel_path!r}")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise HubFileError(f"补传文件路径不安全（禁止绝对路径）: {rel_path}")
    if any(p == ".." for p in parts):
        raise HubFileError(f"补传文件路径不安全（禁止 .. 穿越）: {rel_path}")
    return "/".join(parts)


def merged_index(record) -> List[Dict[str, Any]]:
    """原始解包索引 + 人工补传索引的合并视图（补传条目打 is_manual=True）

    树 / 匹配预览等读取面统一走本函数：file_index 落库保持纯原始（重导重建
    索引后无需同步补传条目），补传条目在读取时合并。

    W9 优先级统一（计划：预览视图 =「合并后视图 + 补传条目打 is_manual 标记」）：
    同名 path 的补传条目**替换**原始条目（保留原始条目位置），而非简单追加——
    与执行镜像 resolve_hub_file_fields 的合并语义（解包区先、补传区后，
    同名以补传版为准）严格一致，保证「所见即所评」：补传覆盖后的文件在
    树/匹配预览中只出现一条（is_manual=True），matched_count 与执行时
    镜像内实际文件数一致（旧实现同名双条目会让预览计数虚高）。
    """
    base = list(getattr(record, "file_index", None) or [])
    supplements = getattr(record, "supplement_index", None) or []
    if not supplements:
        return base
    manual_paths = {
        item.get("path")
        for item in supplements
        if isinstance(item, dict) and item.get("path")
    }
    merged: List[Dict[str, Any]] = []
    for item in base:
        path = item.get("path") if isinstance(item, dict) else None
        if path is not None and path in manual_paths:
            # 同名补传条目在后面追加（替换视图：原始条目让位）
            continue
        merged.append(item)
    for item in supplements:
        entry = dict(item)
        entry["is_manual"] = True
        merged.append(entry)
    return merged


def add_supplements(
    db,
    file_id: str,
    files: List[Tuple[str, Any]],
    relative_paths: Optional[List[str]] = None,
    target_dir: str = "",
    uploaded_by: Optional[str] = None,
) -> Dict[str, Any]:
    """向执行包人工补传文件（散文件落 target_dir/relative_path，zip 解压保留结构）

    流程：登记校验 → staging 归一化（zip 用 unpack_zip_safely 安全解压，散文件
    流式落盘边写边计数）→ 条目/单文件/总量校验全过 → 同卷 os.replace 移入
    补充区（同名覆盖）→ supplement_index 整体重赋值后 commit（本次补传的原子
    提交点；commit 前失败补充区零残留）。

    Args:
        files: [(原始文件名, 文件对象)]，文件对象须支持 .read()（UploadFile.file）
        relative_paths: 与 files 等长的相对路径列表（散文件在 target_dir 下的
                        相对路径，可含子目录；zip 条目忽略此值按内部结构展开）
        target_dir: 散文件的目标子目录（缺省包根；zip 不受影响，按内部结构落盘）
        uploaded_by: 上传人（审计字段，缺省空串）

    Returns:
        {file_id, added_count, added: [{path, size}], overwritten: [被覆盖的相对路径]}

    Raises:
        HubFileError: 中文校验错误（未导入 / 路径不安全 / 超限 / 空 zip 等）
    """
    file_id = str(file_id or "").strip()
    if not file_id:
        raise HubFileError("file_id 不能为空")
    if not files:
        raise HubFileError("未选择任何补传文件")
    record = _query_record(db, file_id)
    if (
        record is None
        or record.unpack_status != "unpacked"
        or not record.unpack_dir
    ):
        status = getattr(record, "unpack_status", None) if record is not None else "未登记"
        raise HubFileError(
            f"执行包（file_id={file_id}）无持久解包区（登记状态: {status}），"
            "请先从 anc-hub 导入后再补传"
        )
    if relative_paths and len(relative_paths) != len(files):
        raise HubFileError(
            f"relative_paths 与文件数量不一致（{len(relative_paths)} vs {len(files)}）"
        )
    target_parts = _clean_supplement_rel(target_dir).split("/") if str(target_dir or "").strip() else []

    existing_supp = {
        e.get("path"): e for e in (getattr(record, "supplement_index", None) or [])
    }
    supplement_root = _hub_supplements_root()
    staging = os.path.join(supplement_root, f".staging_{uuid.uuid4().hex}")
    # 同批同名后者覆盖前者（与 zip 解压语义一致）
    staged: Dict[str, str] = {}
    added: List[Dict[str, Any]] = []
    try:
        for i, (name, fileobj) in enumerate(files):
            display = str(name or f"file_{i + 1}")
            rel_arg = str((relative_paths[i] if relative_paths else "") or "")
            if display.lower().endswith(".zip"):
                # zip：blob 流式落盘（zip 本体无独立上限）→ unpack_zip_safely
                # 安全解压（自带条目/单文件/总量预检与 zip slip 校验）
                blob_path = ensure_within(staging, f"blob_{i}.zip")
                # ARCH-0-3：staging 目录显式 0o750 权限创建
                secure_mkdir(os.path.dirname(blob_path))
                with open(blob_path, "wb") as out:
                    shutil.copyfileobj(fileobj, out, 1024 * 1024)
                unpack_dir_i = os.path.join(staging, f"unzip_{i}")
                stats = unpack_zip_safely(blob_path, unpack_dir_i)
                if stats.get("file_count") == 0:
                    raise HubFileError(f"上传的 zip 内没有任何文件: {display}")
                os.remove(blob_path)  # 解压完即删 blob，避免 staging 双份占盘
                for p in sorted(Path(unpack_dir_i).rglob("*")):
                    if p.is_file():
                        staged[p.relative_to(unpack_dir_i).as_posix()] = str(p)
            else:
                # 散文件：落 target_dir/relative_path（缺省用原始文件名的 basename）
                rel_src = rel_arg if rel_arg.strip() else os.path.basename(display.replace("\\", "/"))
                rel = "/".join(target_parts + _clean_supplement_rel(rel_src).split("/"))
                # 路径清洗之外再做 realpath 越界兜底
                dest = ensure_within(os.path.join(staging, "loose"), *rel.split("/"))
                # ARCH-0-3：staging 散文件目录显式 0o750 权限创建
                secure_mkdir(os.path.dirname(dest))
                written = 0
                with open(dest, "wb") as out:
                    while True:
                        chunk = fileobj.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_SUPPLEMENT_FILE_SIZE:
                            raise HubFileError(
                                f"补传文件超过单文件上限 {MAX_SUPPLEMENT_FILE_SIZE} 字节: {rel}"
                            )
                        out.write(chunk)
                staged[rel] = dest

        if not staged:
            raise HubFileError("补传内容为空（未解析出任何文件）")
        # 条目/总量上限（补传区累计口径：既有补传 + 本次）
        existing_count = len(existing_supp)
        if existing_count + len(staged) > MAX_UNPACK_ENTRIES:
            raise HubFileError(
                f"补传文件数超过上限 {MAX_UNPACK_ENTRIES}"
                f"（已有 {existing_count} + 本次 {len(staged)}），拒绝落盘"
            )
        existing_total = sum(int(e.get("size") or 0) for e in existing_supp.values())
        batch_total = sum(os.path.getsize(p) for p in staged.values())
        if existing_total + batch_total > MAX_UNPACK_TOTAL_SIZE:
            raise HubFileError(
                f"补传总量超过上限 {MAX_UNPACK_TOTAL_SIZE} 字节"
                f"（已有 {existing_total} + 本次 {batch_total}），拒绝落盘"
            )

        original_paths = {e.get("path") for e in (record.file_index or [])}
        overwritten = sorted(
            rel for rel in staged if rel in original_paths or rel in existing_supp
        )

        supplement_dir = _supplement_dir_for(file_id)
        # ARCH-0-3：hub_supplements 补传区目录显式 0o750 权限创建
        secure_mkdir(supplement_dir)
        uploaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for rel, src in sorted(staged.items()):
            dest = os.path.join(supplement_dir, *rel.split("/"))
            # ARCH-0-3：补传子目录显式 0o750 权限创建
            secure_mkdir(os.path.dirname(dest))
            os.replace(src, dest)
            entry = _index_entry_for_file(dest, supplement_dir) or {
                "path": rel, "type": "binary", "size": 0, "is_nested_zip": False,
            }
            entry["uploaded_by"] = uploaded_by or ""
            entry["uploaded_at"] = uploaded_at
            existing_supp[rel] = entry
            added.append({"path": rel, "size": entry.get("size") or 0})
        # SQLAlchemy JSON 列变更需整体重新赋值才会被追踪
        record.supplement_index = list(existing_supp.values())
        db.commit()
        logger.info(
            f"[hub_file] 人工补传完成: file_id={file_id} 本次 {len(added)} 个文件"
            f"（覆盖 {len(overwritten)} 个）uploaded_by={uploaded_by}"
        )
        return {
            "file_id": file_id,
            "added_count": len(added),
            "added": added,
            "overwritten": overwritten,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def delete_supplement(db, file_id: str, rel_path: str) -> Dict[str, Any]:
    """删除单个人工补传文件（原始执行包文件只读，不可删）

    Returns:
        {file_id, deleted, remaining_count}

    Raises:
        HubFileError: 未登记 / 路径不安全 / 目标不是补传文件 / 补传文件不存在
    """
    file_id = str(file_id or "").strip()
    if not file_id:
        raise HubFileError("file_id 不能为空")
    record = _query_record(db, file_id)
    if record is None:
        raise HubFileError(f"执行包未登记（file_id={file_id}）")
    rel = _clean_supplement_rel(rel_path)
    supplements = list(getattr(record, "supplement_index", None) or [])
    supp_paths = {e.get("path") for e in supplements}
    if rel not in supp_paths:
        original_paths = {e.get("path") for e in (record.file_index or [])}
        if rel in original_paths:
            raise HubFileError(
                f"该文件属于原始执行包内容，不支持删除（仅支持删除人工补传文件）: {rel}"
            )
        raise HubFileError(f"包内不存在该人工补传文件: {rel}")

    target = os.path.join(_supplement_dir_for(file_id), *rel.split("/"))
    if os.path.isfile(target):
        os.remove(target)
    else:
        # 实体已缺失（容器重建/人工清理）：仅清理索引条目
        logger.warning(f"[hub_file] 补传文件实体缺失，仅清理索引条目: {rel}")
    remaining = [e for e in supplements if e.get("path") != rel]
    record.supplement_index = remaining  # 整体重赋值触发 JSON 变更追踪
    db.commit()
    logger.info(f"[hub_file] 已删除人工补传文件: file_id={file_id} path={rel}")
    return {"file_id": file_id, "deleted": rel, "remaining_count": len(remaining)}


def backfill_refs_for_hub_file_datasets(
    db,
    service: Optional["HubFileService"] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """存量补登记：历史会话导入（from-hub）只解析未物化，补齐登记与引用计数

    幂等口径：按当前活跃数据行统计每个 file_id 的总需引用数（逐行多重、
    行内去重，与 release_dataset_hub_refs 释放粒度一致），ref_count 不足
    则补齐差值；登记缺失走 ensure_unpacked（已登记命中零下载）。

    Returns:
        {needed, materialized, refilled, errors, dry_run}
    """
    from app.models.dataset_models import EvaluationDataV2, DatasetFieldMetadata

    dataset_ids = [
        row[0]
        for row in db.query(DatasetFieldMetadata.dataset_id)
        .filter(DatasetFieldMetadata.field_type == HUB_FIELD_TYPE)
        .distinct()
        .all()
    ]
    needed: Dict[str, int] = {}
    for ds_id in dataset_ids:
        fields = get_hub_file_fields(db, ds_id)
        if not fields:
            continue
        rows = db.query(EvaluationDataV2).filter(
            EvaluationDataV2.dataset_id == ds_id,
            EvaluationDataV2.is_active == True,  # noqa: E712
        ).all()
        for row in rows:
            for fid in collect_row_hub_file_ids(row.data or {}, fields):
                needed[fid] = needed.get(fid, 0) + 1

    result: Dict[str, Any] = {
        "needed": needed, "materialized": [], "refilled": {},
        "errors": {}, "dry_run": dry_run,
    }
    if dry_run:
        return result
    for fid, want in needed.items():
        try:
            info = ensure_unpacked(db, fid, service=service)
            result["materialized"].append({"file_id": fid, "cached": bool(info.get("cached"))})
        except Exception as e:  # noqa: BLE001 - 单包失败记明细，继续其余补登记
            result["errors"][fid] = str(e)[:500]
            continue
        record = _query_record(db, fid)
        have = int(record.ref_count or 0) if record is not None else 0
        add = max(0, want - have)
        for _ in range(add):
            acquire_ref(db, fid)
        if add:
            result["refilled"][fid] = add
    return result


def release_dataset_hub_refs(db, dataset_id: str) -> None:
    """释放数据集全部活跃数据行持有的执行包引用（数据集删除入口共用）

    跨行保留多重性（两行引用同一 file_id → 释放两次，与导入时逐行 acquire 对齐）；
    行内同一 file_id 只释放一次（导入时每行每包只 acquire 一次）。
    删除数据集的各入口（delete_dataset / cleanup_duplicates）在软删提交后调用。
    """
    from app.models.dataset_models import EvaluationDataV2

    fields = get_hub_file_fields(db, dataset_id)
    if not fields:
        return
    rows = db.query(EvaluationDataV2).filter(
        EvaluationDataV2.dataset_id == dataset_id,
        EvaluationDataV2.is_active == True  # noqa: E712
    ).all()
    file_ids: List[str] = []
    for row in rows:
        file_ids.extend(collect_row_hub_file_ids(row.data or {}, fields))
    if file_ids:
        release_hub_refs(db, file_ids)


# ---------------------------------------------------------------------------
# 数据集迭代阶段D：文件树 / 内容预览 / 下载 / 匹配预览
# ---------------------------------------------------------------------------

# 内容预览支持的文本后缀与 mime（验收清单口径：json/py/log/txt/md/csv/html/js/css/ini；
# .jsonl 为会话导入数据集的原始文件形态，抽屉预览是主要动作，文本截断 2MB 保护）
PREVIEW_TEXT_MIME = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".py": "text/x-python",
    ".log": "text/plain",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".ini": "text/plain",
}
# 内容预览支持的图片后缀与 mime（FileResponse 内联返回，浏览器直接渲染）
PREVIEW_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
# 文本预览截断上限（超出只返回前 2MB，truncated=True）
PREVIEW_TEXT_LIMIT = 2 * 1024 * 1024


def preview_kind(rel_path: str):
    """按后缀分流内容预览：返回 (kind, mime)，kind ∈ text|image|binary

    xlsx/zip 等其余后缀归 binary，由端点返回 4xx 提示走 download。
    """
    ext = PurePosixPath(str(rel_path or "")).suffix.lower()
    if ext in PREVIEW_TEXT_MIME:
        return "text", PREVIEW_TEXT_MIME[ext]
    if ext in PREVIEW_IMAGE_MIME:
        return "image", PREVIEW_IMAGE_MIME[ext]
    return "binary", "application/octet-stream"


def get_hub_record(db, file_id: str):
    """按 file_id 查执行包登记记录（None 表示未登记）"""
    file_id = str(file_id or "").strip()
    if not file_id:
        raise HubFileError("file_id 不能为空")
    return _query_record(db, file_id)


def resolve_path_in_root(unpack_root: str, rel_path: str) -> str:
    """防穿越统一校验：解析包内相对路径为解包根内的绝对路径

    阶段D所有带 path 参数的端点共用本函数（realpath 后必须以解包根为前缀）：
    - 拒绝空路径 / 绝对路径（/ 开头或 Windows 盘符）
    - 拒绝 .. 穿越（含反斜杠写法；URL 编码逃逸经 FastAPI 解码后同样命中）
    - realpath 解析后越出解包根（symlink 指向包外等）一律拒绝

    Returns:
        校验通过后的绝对路径（realpath 结果；文件可以不存在，存在性由调用方判断）

    Raises:
        HubFileError: 路径为空 / 穿越 / 越出解包根（中文错误）
    """
    root_real = os.path.realpath(str(unpack_root))
    raw = str(rel_path or "").replace("\\", "/")
    if not raw.strip():
        raise HubFileError("包内文件路径不能为空")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise HubFileError(f"包内文件路径不安全（禁止绝对路径）: {rel_path}")
    parts = [p for p in PurePosixPath(raw).parts if p not in ("", ".")]
    if any(p == ".." for p in PurePosixPath(raw).parts):
        raise HubFileError(f"包内文件路径不安全（禁止 .. 穿越）: {rel_path}")
    try:
        candidate = os.path.join(root_real, *parts) if parts else root_real
        real = os.path.realpath(candidate)
    except (OSError, ValueError) as e:
        raise HubFileError(f"包内文件路径无法解析: {rel_path}（{e}）")
    if real != root_real and not real.startswith(root_real + os.sep):
        raise HubFileError(f"包内文件路径越出执行包范围: {rel_path}")
    return real


def _parts_match(path_parts, pat_parts) -> bool:
    """组件级递归匹配：普通组件按 fnmatchcase，'**' 匹配零个或多个组件

    与 pathlib.Path.glob 的语义对齐（实测 Python 3.14：'*' 匹配点开头文件、
    '**' 递归进入隐藏目录），仅文件路径组件参与（目录由索引天然排除）。
    """
    if not pat_parts:
        return not path_parts
    pat = pat_parts[0]
    if pat == "**":
        return _parts_match(path_parts, pat_parts[1:]) or (
            bool(path_parts) and _parts_match(path_parts[1:], pat_parts)
        )
    if not path_parts:
        return False
    return fnmatch.fnmatchcase(path_parts[0], pat) and _parts_match(path_parts[1:], pat_parts[1:])


def _normalize_pattern(pattern: str) -> List[str]:
    """校验并归一化匹配模式为组件列表（与 match_files 同口径）

    Raises:
        HubFileError: 模式为空或含 .. 穿越
    """
    pat = str(pattern or "").strip().lstrip("/")
    if not pat:
        raise HubFileError("文件匹配模式不能为空")
    if any(part == ".." for part in PurePosixPath(pat).parts):
        raise HubFileError(f"文件匹配模式不允许 .. 穿越: {pattern}")
    return [p for p in pat.split("/") if p and p != "."]


def _sole_top_dir_in_index(file_index: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """返回索引中唯一顶层目录名（与 _sole_top_dir_of_root 的文件系统判定同语义）

    所有文件路径首段唯一且存在嵌套路径（首段是目录而非根级文件）时返回该首段，
    其余情况返回 None。
    """
    tops = set()
    has_nested = False
    for item in file_index or []:
        path = item.get("path") if isinstance(item, dict) else None
        if not path:
            continue
        parts = PurePosixPath(str(path)).parts
        if not parts:
            continue
        tops.add(parts[0])
        if len(parts) > 1:
            has_nested = True
    if len(tops) == 1 and has_nested:
        return tops.pop()
    return None


def match_files_in_index(file_index: Optional[List[Dict[str, Any]]], pattern: str) -> List[str]:
    """match_files 的纯函数版：基于登记表 file_index 匹配（零文件 IO）

    阶段D匹配预览专用，与执行时 match_files（文件系统 pathlib glob）同语义：
    pathlib glob 的组件级 fnmatch 匹配 + '**' 递归 + 按路径排序、仅文件。
    空模式 / 含 .. 的模式与 match_files 抛同样的中文错误；尾随 '/' 的模式
    在 pathlib glob 中只匹配目录，这里同样恒为空（只匹配文件）。
    单顶层目录自动拼接（缺陷B2）与 match_files 同源：原始 pattern 未命中、
    索引中存在唯一顶层目录且 pattern 首段不是该目录名时，以 {顶层目录}/{pattern}
    再匹配一次（取非空结果），保证预览与执行对带 attempt 顶层目录的包行为一致。
    """
    pat = str(pattern or "").strip().lstrip("/")
    if pat.endswith("/"):
        return []
    pat_parts = _normalize_pattern(pattern)
    matched = sorted(
        item["path"]
        for item in (file_index or [])
        if isinstance(item, dict) and item.get("path")
        and _parts_match(PurePosixPath(str(item["path"])).parts, pat_parts)
    )
    if matched:
        return matched
    top = _sole_top_dir_in_index(file_index)
    if top and pat_parts[0] != top:
        retried = sorted(
            item["path"]
            for item in (file_index or [])
            if isinstance(item, dict) and item.get("path")
            and _parts_match(PurePosixPath(str(item["path"])).parts, [top] + pat_parts)
        )
        if retried:
            return retried
    return matched


def read_nested_zip_entries(zip_path: str) -> List[Dict[str, Any]]:
    """读取包内 zip 的内层清单（只读 central directory，不解压内容）

    Raises:
        HubFileError: 不是有效 zip / 不可读（中文错误）
    """
    entries = _nested_zip_index(zip_path)
    if entries is None:
        raise HubFileError(
            f"嵌套 zip 读取失败（不是有效的 zip 文件）: {os.path.basename(str(zip_path))}"
        )
    return entries


# 匹配预览返回的样例路径条数上限
MATCH_PREVIEW_SAMPLE_LIMIT = 20


def hub_match_preview(
    db,
    dataset_id: str,
    field_name: str,
    pattern: str,
) -> Dict[str, Any]:
    """匹配预览：数据集首条含 hub_file 字段样本的登记索引上试匹配 pattern

    与执行时同一 match_files 语义（match_files_in_index 纯函数版，零文件 IO）。
    无样本数据时返回可展示的空态（matched_count=0、sample_data_index=None）。

    Raises:
        HubRecordMissingError: 样本引用的执行包未登记（端点映射 404）
        HubFileError: pattern 为空或含 .. 穿越（端点映射 400）
    """
    from app.models.dataset_models import EvaluationDataV2

    dataset_id = str(dataset_id or "").strip()
    field_name = str(field_name or "").strip()
    if not dataset_id:
        raise HubFileError("dataset_id 不能为空")
    if not field_name:
        raise HubFileError("field_name 不能为空")

    base = {"dataset_id": dataset_id, "field_name": field_name, "pattern": pattern}
    # 先校验 pattern（空 / .. 穿越直接 400，不依赖样本是否存在）
    _normalize_pattern(pattern)

    rows = (
        db.query(EvaluationDataV2)
        .filter(
            EvaluationDataV2.dataset_id == dataset_id,
            EvaluationDataV2.is_active == True,  # noqa: E712
        )
        .order_by(EvaluationDataV2.index)
        .all()
    )
    file_id: Optional[str] = None
    sample_data_index: Optional[int] = None
    for row in rows:
        value = (row.data or {}).get(field_name) if isinstance(row.data, dict) else None
        if isinstance(value, str) and value.strip():
            file_id = value.strip()
            sample_data_index = row.index
            break

    if file_id is None:
        return {
            **base,
            "file_id": None,
            "matched_count": 0,
            "sample_paths": [],
            "sample_data_index": None,
            "message": f"数据集中暂无字段 {field_name} 的执行包样本数据，无法预览匹配效果",
        }

    record = _query_record(db, file_id)
    # 叠加视图（原始 + 人工补传）：补传文件同样参与预览匹配，与执行镜像一致
    preview_index = merged_index(record) if record is not None else []
    if (
        record is None
        or record.unpack_status != "unpacked"
        or not preview_index
    ):
        status = getattr(record, "unpack_status", None) if record is not None else "未登记"
        raise HubRecordMissingError(
            f"样本数据引用的执行包（file_id={file_id}）无可用索引"
            f"（登记状态: {status}），请重新导入该执行包后再预览"
        )

    matched = match_files_in_index(preview_index, pattern)
    return {
        **base,
        "file_id": file_id,
        "matched_count": len(matched),
        "sample_paths": matched[:MATCH_PREVIEW_SAMPLE_LIMIT],
        "sample_data_index": sample_data_index,
    }
