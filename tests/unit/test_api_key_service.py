"""M1 QE1-1 单测：API Key 服务（生成文法 / parse 防篡改 / create-verify 回路）

中间件分支与节流的攻击面覆盖在 tests/security/ 三件套；本文件收敛服务本体的
契约单测（token 文法、校验段、prefix 扫描、scope 白名单校验）。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import scopes as scope_constants
from app.db.session import Base
from app.models.user_models import User
from app.services import api_key_service

pytestmark = pytest.mark.unit


# ---------------- token 文法 ----------------

def test_generate_token_grammar():
    token, prefix, key_hash = api_key_service.generate_api_key()
    # aievo_sk_<26 位随机>_<4 位 hex 校验>：总长 9 + 26 + 1 + 4 = 40
    assert token.startswith("aievo_sk_")
    body = token[len("aievo_sk_"):]
    random_part, checksum = body.split("_")
    assert len(random_part) == 26 and random_part.isalnum()
    assert len(checksum) == 4
    import hashlib
    assert checksum == hashlib.sha256(random_part.encode()).hexdigest()[:4]
    # prefix = 随机段前 8 位；hash = 完整 token sha256
    assert prefix == random_part[:8]
    assert key_hash == hashlib.sha256(token.encode()).hexdigest()


def test_generated_tokens_unique():
    tokens = {api_key_service.generate_api_key()[0] for _ in range(200)}
    assert len(tokens) == 200  # secrets 随机段，200 枚无碰撞（概率学下界验证）


def test_parse_accepts_valid_rejects_tampered():
    token, prefix, _ = api_key_service.generate_api_key()
    assert api_key_service.parse_api_key(token) == prefix
    random_part = token[len("aievo_sk_"):].split("_")[0]
    for tampered in (
        token[:-1],                                 # 截断（校验段缺位）
        "aievo_sk_" + random_part + "-",            # 缺校验段
        "aievo_sk_" + random_part + "_zzzz",        # 校验段非 hex
        "aievo_sk_" + random_part + "_abcd",        # 校验段不符（篡改探测）
        "aievo-sk_" + token[9:],                    # 前缀改写
        "",                                          # 空
        "Bearer " + token,                          # 带认证头前缀整体误传
    ):
        assert api_key_service.parse_api_key(tampered) is None, tampered


# ---------------- create / verify 回路（SQLite 内存库） ----------------

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_create_and_verify_roundtrip(db_session):
    db_session.add(User(user_code="u1", username="alice", is_active=True))
    db_session.commit()

    record, plaintext = api_key_service.create_api_key(
        db_session, user_code="u1", name="ci-key",
        granted_scopes=["tasks:run", "tasks:read"], team_code="DEFAULT")
    assert record.key_prefix == api_key_service.parse_api_key(plaintext)
    assert record.revoked_at is None and record.expires_at is None

    # 库内不落明文：prefix/hash 都不是 token 本身
    assert plaintext not in (record.key_prefix, record.key_hash)

    matched, reason = api_key_service.verify_api_key(db_session, plaintext)
    assert matched is not None and reason is None
    assert matched.id == record.id

    # 错 token：文法/校验段合法但库中无此 Key → hash_mismatch（prefix 无命中）
    import hashlib
    random_part = "b" * 26
    wrong = "aievo_sk_" + random_part + "_" + hashlib.sha256(random_part.encode()).hexdigest()[:4]
    _, reason = api_key_service.verify_api_key(db_session, wrong)
    assert reason == "hash_mismatch"
    # 文法错（校验段不符）→ malformed_token
    _, reason = api_key_service.verify_api_key(db_session, "aievo_sk_" + random_part + "_0000")
    assert reason == "malformed_token"


def test_create_rejects_unknown_scope(db_session):
    with pytest.raises(ValueError):
        api_key_service.create_api_key(
            db_session, user_code="u1", name="bad",
            granted_scopes=["tasks:run", "bogus:scope"])


def test_verify_revoked_and_expired(db_session):
    from datetime import datetime, timedelta
    db_session.add(User(user_code="u2", username="bob", is_active=True))
    db_session.commit()

    record, plaintext = api_key_service.create_api_key(
        db_session, user_code="u2", name="k", granted_scopes=[scope_constants.TASKS_READ])

    record.revoked_at = datetime.utcnow()
    db_session.commit()
    _, reason = api_key_service.verify_api_key(db_session, plaintext)
    assert reason == "revoked"

    record.revoked_at = None
    record.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    _, reason = api_key_service.verify_api_key(db_session, plaintext)
    assert reason == "expired"

    record.expires_at = datetime.utcnow() + timedelta(hours=1)
    db_session.commit()
    _, reason = api_key_service.verify_api_key(db_session, plaintext)
    assert reason is None
