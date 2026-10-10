"""
JWT 签发与校验服务
HS256，标准库实现（hmac + base64url + json），无第三方依赖
payload: {"sub": <user_code>, "username": <str>, "iat": <int>, "exp": <int>, "jti": <str>}
"""
import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Dict, Optional

from app.core.config import get_settings


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _signing_input(header_b64: str, payload_b64: str) -> bytes:
    return f"{header_b64}.{payload_b64}".encode("ascii")


def create_token(user_code: str, username: str) -> str:
    """为指定用户签发 JWT"""
    settings = get_settings()
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_code,
        "username": username,
        "iat": now,
        "exp": now + settings.AUTH_TOKEN_EXPIRE_HOURS * 3600,
        "jti": uuid.uuid4().hex,
    }
    header_b64 = _b64url_encode(
        json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(
        settings.auth_jwt_secret.encode("utf-8"),
        _signing_input(header_b64, payload_b64),
        hashlib.sha256,
    ).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """
    校验并解析 JWT；签名不符或已过期返回 None
    """
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        expected_sig = hmac.new(
            get_settings().auth_jwt_secret.encode("utf-8"),
            _signing_input(header_b64, payload_b64),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected_sig, _b64url_decode(signature_b64)):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        if not payload.get("sub"):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
