"""
密码哈希服务
PBKDF2-HMAC-SHA256（标准库实现，无第三方依赖）
存储格式：pbkdf2_sha256$<iterations>$<salt_hex>$<dk_hex>
"""
import hashlib
import hmac
import os

_ITERATIONS = 260_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    """生成密码哈希（随机盐）"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码与哈希是否匹配；格式异常一律返回 False"""
    try:
        algo, iterations, salt_hex, dk_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(iterations),
        )
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError, TypeError):
        return False
