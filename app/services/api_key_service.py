"""
API Key 服务（M1 QE1-1，门禁闭环）

token 文法（方案 §M1 指定）：``aievo_sk_<26 位随机>_<校验 4 位>``

- 随机段：secrets 生成，字母数字 62字符表（cryptographically secure）；
- 校验段：随机段 sha256 前 4 位十六进制——仅用于客户端拼写自查（CLI 本地
  报 typo），**不参与服务端安全判断**（服务端只信 key_hash 比对）；
- 库内只存完整 token 的 sha256（key_hash 列）与随机段前 8 位（key_prefix，
  展示 + 认证扫描索引）。

认证流程见 app/middleware/auth_middleware.py 的 API Key 分支：按 key_prefix
单查询扫描（走 ix_api_keys_prefix 索引）→ sha256 恒时比对 → 有效期/吊销/
用户活跃校验。
"""
import hashlib
import hmac
import re
import secrets
import string
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.core import scopes as scope_constants
from app.models.api_key_models import ApiKey

# token 前缀（区分 JWT 与 API Key 的分流标志）
API_KEY_TOKEN_PREFIX = "aievo_sk_"

# 随机段字母表：字母 + 数字（去掉易混淆字符会缩小熵，保留全 62 字符表）
_RANDOM_ALPHABET = string.ascii_letters + string.digits
_RANDOM_LENGTH = 26
_CHECKSUM_LENGTH = 4

# 完整 token 文法：aievo_sk_<26 位字母数字>_<4 位十六进制校验>
_API_KEY_RE = re.compile(
    r"^" + re.escape(API_KEY_TOKEN_PREFIX)
    + r"([A-Za-z0-9]{26})_([0-9a-f]{4})$"
)

# key_prefix 展示长度（随机段前 8 位）
KEY_PREFIX_LENGTH = 8


def generate_api_key() -> Tuple[str, str, str]:
    """生成一枚 API Key

    Returns:
        (plaintext, key_prefix, key_hash)——明文仅此一次返回，调用方（QE1-4
        创建端点）展示后不得再落地；key_prefix/key_hash 入库。
    """
    random_part = "".join(secrets.choice(_RANDOM_ALPHABET) for _ in range(_RANDOM_LENGTH))
    checksum = hashlib.sha256(random_part.encode("utf-8")).hexdigest()[:_CHECKSUM_LENGTH]
    plaintext = f"{API_KEY_TOKEN_PREFIX}{random_part}_{checksum}"
    return plaintext, random_part[:KEY_PREFIX_LENGTH], hash_api_key(plaintext)


def hash_api_key(token: str) -> str:
    """完整 token 的 sha256（十六进制，64 位，入 key_hash 列）"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_api_key(token: str) -> Optional[str]:
    """校验 token 文法与校验段，合法返回 key_prefix（随机段前 8 位）

    文法/校验段不符返回 None——中间件按认证失败处理（计节流）。注意：通过
    文法校验 ≠ 认证成功，认证以 key_hash 比对为准。
    """
    if not token:
        return None
    m = _API_KEY_RE.match(token)
    if not m:
        return None
    random_part, checksum = m.group(1), m.group(2)
    expected = hashlib.sha256(random_part.encode("utf-8")).hexdigest()[:_CHECKSUM_LENGTH]
    if not hmac.compare_digest(checksum, expected):
        return None
    return random_part[:KEY_PREFIX_LENGTH]


def verify_api_key(db: Session, token: str) -> Tuple[Optional[ApiKey], Optional[str]]:
    """按 token 认证：prefix 扫描 → 恒时 hash 比对 → 有效性校验

    Returns:
        (ApiKey, None) = 认证成功；
        (None, reason) = 失败，reason 为内部口径（记日志/排障用，**不得**
        原样返回给调用方——统一 401 文案防探测）。
    """
    prefix = parse_api_key(token)
    if prefix is None:
        return None, "malformed_token"

    key_hash = hash_api_key(token)
    # 按 key_prefix 索引单查询（方案指定扫描口径）；命中 0 条即失败
    candidates = (
        db.query(ApiKey)
        .filter(ApiKey.key_prefix == prefix)
        .all()
    )
    matched = None
    for candidate in candidates:
        if hmac.compare_digest(candidate.key_hash, key_hash):
            matched = candidate
            break
    if matched is None:
        return None, "hash_mismatch"

    now = datetime.now(timezone.utc)
    if matched.revoked_at is not None:
        return None, "revoked"
    if matched.expires_at is not None:
        expires_at = matched.expires_at
        if expires_at.tzinfo is None:  # SQLite 单测 naive datetime 兜底
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            return None, "expired"
    return matched, None


def create_api_key(
    db: Session,
    user_code: str,
    name: str,
    granted_scopes,
    team_code: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> Tuple[ApiKey, str]:
    """创建 API Key（QE1-4 端点接线用；scope 须为 ALL_SCOPES 子集）

    Returns:
        (ApiKey 已落库, plaintext 明文——仅此一次返回给调用方)
    """
    normalized = scope_constants.normalize_scopes(granted_scopes)
    unknown = normalized - frozenset(scope_constants.ALL_SCOPES)
    if unknown:
        raise ValueError(f"未知 scope：{sorted(unknown)}")
    plaintext, key_prefix, key_hash = generate_api_key()
    record = ApiKey(
        user_code=user_code,
        team_code=team_code,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=sorted(normalized),
        expires_at=expires_at,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record, plaintext
