#!/usr/bin/env python
"""License 生成工具（M6 QE6-1）

Ed25519 离线 license 三件套：keygen（密钥对）/ issue（签发）/ verify（验签）。

license 文件格式（自包含 JSON 信封，平台侧 license_service 同口径解析）::

    {
      "alg": "Ed25519",
      "payload": "<base64url(canonical JSON claims)>",
      "sig": "<base64url(Ed25519 签名，对 payload 解码后的字节原文签名)>"
    }

claims 字段（与方案 §M6 QE6-1 一致）::

    customer            客户名（授权对象，必填）
    tier                oss | enterprise（必填）
    issued_at           签发时间 ISO8601（必填）
    expires_at          过期时间 ISO8601（可空=永久）
    max_users           用户规模上限（可空=不限）
    max_monthly_runs    月度评测运行上限（可空=不限；QE6-3 计量对账用）
    features            企业功能位列表（可空；枚举见 license_service.ENTERPRISE_FEATURES）

密钥纪律（生产/测试双轨）：
- 测试密钥对**有意入库**（tools/license/keys/，供开发自测与 CI 用例），
  平台默认 LICENSE_PUBLIC_KEY 即测试公钥——本地 gen 出的 license 开箱可验；
- 生产私钥由厂商持有、绝不入库：签发时 --key 指向部署机/厂商侧的私钥
  文件路径，且 keygen 默认输出目录用 --out-dir 显式给到仓库外；
- LICENSE_PUBLIC_KEY 缺省=测试公钥，生产部署必须换成厂商公钥
  （.env 覆盖），否则测试私钥签的 license 也能过验签（等于没设防）。

用法（一律 .venv/bin/python 运行）::

    # 生成测试密钥对（已入库，勿重复生成覆盖）
    .venv/bin/python tools/license/gen.py keygen --out-dir tools/license/keys --name test_signing_key

    # 签发 enterprise license（测试私钥）
    .venv/bin/python tools/license/gen.py issue \
        --customer "ACME 科技" --tier enterprise \
        --expires 2027-12-31 --max-users 50 --max-monthly-runs 5000 \
        --features audit_export,report_white_label \
        --key tools/license/keys/test_signing_key --out acme.json

    # 验签并打印 claims
    .venv/bin/python tools/license/gen.py verify --file acme.json
"""
import argparse
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# 与 app/core/config.py Settings.LICENSE_PUBLIC_KEY 默认值同源（测试公钥，入库）
DEFAULT_TEST_KEY_DIR = REPO_ROOT / "tools" / "license" / "keys"


def b64u_encode(data: bytes) -> str:
    """base64url 无填充编码（信封 payload/sig 的统一编码）"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """base64url 无填充解码（宽容已填充输入）"""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def canonical_claims(claims: dict) -> bytes:
    """claims 的规范化字节序（排序键 + 紧分隔符，签名与验签两侧唯一依据）"""
    return json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ==================== keygen ====================

def cmd_keygen(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    priv_path = out_dir / args.name
    pub_path = out_dir / f"{args.name}.pub"

    if priv_path.exists() and not args.force:
        print(f"已存在 {priv_path}（覆盖请加 --force；测试密钥入库后不要轮换，否则旧 license 全部失效）")
        return 1

    private_key = Ed25519PrivateKey.generate()
    # 存储格式：PEM PKCS8（私钥）/ OpenSSH 公钥体都不便复用，这里统一存
    # 裸 32 字节 base64（raw key），两侧 b64u/raw 互转零歧义
    priv_b64 = base64.b64encode(private_key.private_bytes_raw()).decode("ascii")
    pub_b64 = base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii")
    priv_path.write_text(priv_b64 + "\n")
    pub_path.write_text(pub_b64 + "\n")

    inside_repo = str(priv_path.resolve()).startswith(str(REPO_ROOT) + "/")
    print(f"私钥: {priv_path}")
    print(f"公钥: {pub_path}")
    print(f"LICENSE_PUBLIC_KEY={pub_b64}")
    if inside_repo and not args.test:
        print("⚠️  私钥生成在仓库目录内：仓库内仅允许存放测试密钥（tools/license/keys/）。")
        print("    生产私钥请用 --out-dir 指到仓库外目录并由厂商保管，绝不入库。")
    return 0


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    raw = b64u_decode_or_b64(path.read_text().strip())
    return Ed25519PrivateKey.from_private_bytes(raw)


def _load_public_key(path: Path) -> Ed25519PublicKey:
    raw = b64u_decode_or_b64(path.read_text().strip())
    return Ed25519PublicKey.from_public_bytes(raw)


def b64u_decode_or_b64(text: str) -> bytes:
    """密钥文件读取：标准 base64 与 base64url 均兼容"""
    try:
        return base64.b64decode(text, validate=True)
    except Exception:
        return b64u_decode(text)


# ==================== issue ====================

def build_claims(args: argparse.Namespace) -> dict:
    claims = {
        "customer": args.customer.strip(),
        "tier": args.tier.strip().lower(),
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    if args.expires:
        # 日期口径：--expires 2027-12-31 → 当天 23:59:59 UTC（含当日）
        try:
            d = datetime.strptime(args.expires, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc)
        except ValueError:
            d = datetime.fromisoformat(args.expires)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        claims["expires_at"] = d.isoformat()
    elif args.days:
        claims["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(days=args.days)
        ).isoformat()
    if args.max_users is not None:
        claims["max_users"] = args.max_users
    if args.max_monthly_runs is not None:
        claims["max_monthly_runs"] = args.max_monthly_runs
    if args.features:
        claims["features"] = [f.strip() for f in args.features.split(",") if f.strip()]
    return claims


def cmd_issue(args: argparse.Namespace) -> int:
    if args.tier not in ("oss", "enterprise"):
        print(f"tier 仅允许 oss/enterprise（收到 {args.tier}）")
        return 1
    private_key = _load_private_key(Path(args.key).expanduser())
    claims = build_claims(args)
    payload_bytes = canonical_claims(claims)
    sig = private_key.sign(payload_bytes)
    envelope = {
        "alg": "Ed25519",
        "payload": b64u_encode(payload_bytes),
        "sig": b64u_encode(sig),
    }
    out = Path(args.out).expanduser() if args.out else None
    text = json.dumps(envelope, indent=2, ensure_ascii=False) + "\n"
    if out:
        out.write_text(text)
        print(f"license 已签发: {out}")
    else:
        print(text, end="")
    print(json.dumps(claims, indent=2, ensure_ascii=False))
    return 0


# ==================== verify ====================

def cmd_verify(args: argparse.Namespace) -> int:
    text = Path(args.file).expanduser().read_text()
    try:
        envelope = json.loads(text)
        payload = b64u_decode(envelope["payload"])
        claims = json.loads(payload.decode("utf-8"))
    except Exception as e:
        print(f"解析失败（非合法 license 信封）: {e}")
        return 1

    public_key = _load_public_key(
        Path(args.pubkey).expanduser() if args.pubkey
        else DEFAULT_TEST_KEY_DIR / "test_signing_key.pub")
    try:
        public_key.verify(b64u_decode(envelope["sig"]), payload)
    except InvalidSignature:
        print("❌ 验签失败：签名与 payload/公钥不匹配（篡改或密钥对不符）")
        return 1
    print("✅ 验签通过，claims:")
    print(json.dumps(claims, indent=2, ensure_ascii=False))
    if claims.get("expires_at"):
        exp = datetime.fromisoformat(claims["expires_at"])
        now = datetime.now(timezone.utc)
        print(f"过期时间: {claims['expires_at']}（当前 UTC {now.isoformat(timespec='seconds')}，"
              f"{'未过期' if exp >= now else '已过期——平台侧将降级 oss'}）")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AgentEvo 离线 license 生成工具（Ed25519）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_keygen = sub.add_parser("keygen", help="生成 Ed25519 密钥对")
    p_keygen.add_argument("--out-dir", default=str(DEFAULT_TEST_KEY_DIR), help="输出目录（生产密钥指到仓库外）")
    p_keygen.add_argument("--name", default="test_signing_key", help="密钥文件名（生成 <name> 与 <name>.pub）")
    p_keygen.add_argument("--force", action="store_true", help="覆盖已存在密钥")
    p_keygen.add_argument("--test", action="store_true", help="显式声明这是测试密钥（抑制仓库内告警）")
    p_keygen.set_defaults(func=cmd_keygen)

    p_issue = sub.add_parser("issue", help="签发 license 文件")
    p_issue.add_argument("--customer", required=True, help="客户名")
    p_issue.add_argument("--tier", required=True, choices=["oss", "enterprise"])
    p_issue.add_argument("--expires", help="过期日期 YYYY-MM-DD（当天 23:59:59 UTC）或完整 ISO8601")
    p_issue.add_argument("--days", type=int, help="有效天数（与 --expires 二选一）")
    p_issue.add_argument("--max-users", type=int, dest="max_users", help="用户规模上限")
    p_issue.add_argument("--max-monthly-runs", type=int, dest="max_monthly_runs", help="月度评测运行上限")
    p_issue.add_argument("--features", help="企业功能位，逗号分隔（audit_export,report_white_label,...）")
    p_issue.add_argument("--key", default=str(DEFAULT_TEST_KEY_DIR / "test_signing_key"), help="签名私钥文件")
    p_issue.add_argument("--out", help="输出文件路径（缺省打印到 stdout）")
    p_issue.set_defaults(func=cmd_issue)

    p_verify = sub.add_parser("verify", help="验签并打印 claims")
    p_verify.add_argument("--file", required=True, help="license 文件")
    p_verify.add_argument("--pubkey", help="公钥文件（缺省用仓库测试公钥）")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
