"""M1 安全测试：webhook SSRF 防护（tests/security 三件套之三）

攻击路径复现（红→绿，对象 app/services/webhook_url_guard.py——M1 QE1-1 先行
落地的共享守卫原语，QE1-3 webhook 投递通道接线后同样走本校验）：
1. 协议外（file/gopher/ftp/data/无 scheme）→ 拒绝；
2. 直球内网目标：localhost / 127.0.0.1 / 0.0.0.0 / 私网段 / 链路本地 /
   IPv6 环回与 ULA / 组播 / 保留段 → 拒绝（真实 getaddrinfo，无假体）；
3. 字面量变体：十进制整数地址（2130706433）、短格式（127.1）、十六进制
   （0x7f000001）——解析后与 127.0.0.1 同源 → 拒绝；
4. DNS 解析到内网（内网域名/重绑定形态）：假解析器注入（不依赖真实 DNS），
   解析结果含私网 IP 即整体拒绝——**全部**地址须公网，防多记录混排绕过；
5. 解析失败（NXDOMAIN/超时）→ 拒绝（不留「先注册后解析」的悬空端点）；
6. 企业放行开关 WEBHOOK_ALLOW_PRIVATE_TARGETS=true → 内网目标放行
   （scheme 闸仍生效——file:// 依旧拒绝）；
7. 正常公网目标（公网 IP + 解析到公网的域名）→ 放行且原样返回。
"""
import pytest

from app.services.webhook_url_guard import validate_webhook_url

pytestmark = pytest.mark.unit


# ---------------- 1. 协议闸 ----------------

@pytest.mark.parametrize("bad_url", [
    "file:///etc/passwd",
    "gopher://127.0.0.1:6379/_STAT",
    "ftp://evil.example/x",
    "data:text/html,hello",
    "unix:///var/run/docker.sock",
    "//no-scheme.example/hook",
    "http://",            # 空主机
    "https:///hook",      # 空主机
    "",
    "   ",
])
def test_non_http_schemes_and_empty_host_rejected(bad_url):
    with pytest.raises(ValueError):
        validate_webhook_url(bad_url)


# ---------------- 2. 直球内网目标（真实解析，无假体） ----------------

@pytest.mark.parametrize("bad_url", [
    "http://localhost/hook",
    "http://127.0.0.1/hook",
    "http://127.0.0.1:8080/hook",
    "http://0.0.0.0/hook",
    "http://10.0.0.5/hook",                    # 10/8
    "http://172.16.3.4:9000/hook",             # 172.16/12
    "http://192.168.1.10/hook",                # 192.168/16
    "http://169.254.169.254/latest/meta-data", # 链路本地（云元数据面）
    "http://[::1]/hook",                       # IPv6 环回
    "http://[fe80::1]/hook",                   # IPv6 链路本地
    "http://[fc00::1]/hook",                   # IPv6 ULA
])
def test_literal_private_targets_rejected(bad_url):
    with pytest.raises(ValueError) as exc:
        validate_webhook_url(bad_url)
    assert "SSRF" in str(exc.value) or "拒绝" in str(exc.value)


# ---------------- 3. 字面量变体（整数/短格式/十六进制） ----------------

@pytest.mark.parametrize("bad_url", [
    "http://2130706433/hook",   # 127.0.0.1 的十进制整数形式
    "http://127.1/hook",        # 短格式
    "http://0x7f000001/hook",   # 十六进制
])
def test_numeric_ip_variants_rejected(bad_url):
    """getaddrinfo 按 inet_aton 语义解析上述变体为 127.0.0.1 → 守卫拒绝

    平台口径：只要系统解析器把变体归一到环回/私网即拒（不维护变体黑名单——
    黑名单永远追不全，解析后校验才是根治）。八进制形态（0177.0.0.1）各平台
    解析行为不一（macOS 走 DNS 查询而非数值归一），不做真实解析断言——此类
    形态由「解析结果校验」天然覆盖：无论解析成什么，私网即拒。
    """
    with pytest.raises(ValueError):
        validate_webhook_url(bad_url)


# ---------------- 4. DNS 解析到内网（假解析器，不依赖真实 DNS） ----------------

def resolver_mapping(mapping):
    """构造假解析器：host → 预置地址列表（未登记 host 抛 gaierror 同款异常）"""
    def resolve(host, port):
        if host in mapping:
            return mapping[host]
        raise OSError(f"nodename nor servname provided: {host}")
    return resolve


def test_dns_rebind_to_private_rejected():
    """域名解析到私网 IP（重绑定形态）→ 拒绝"""
    fake = resolver_mapping({"hooks.internal.example": ["10.1.2.3"]})
    with pytest.raises(ValueError):
        validate_webhook_url("https://hooks.internal.example/hook", resolver=fake)


def test_mixed_records_any_private_rejects_all():
    """多 A 记录混排（公网+私网）：任一私网即整体拒绝，不取巧放行"""
    fake = resolver_mapping({"mixed.example": ["93.184.216.34", "192.168.0.9"]})
    with pytest.raises(ValueError):
        validate_webhook_url("https://mixed.example/hook", resolver=fake)


def test_ipv6_private_record_rejected():
    fake = resolver_mapping({"v6.example": ["2606:2800:220:1:248:1893:25c8:1946", "fd00::5"]})
    with pytest.raises(ValueError):
        validate_webhook_url("https://v6.example/hook", resolver=fake)


# ---------------- 5. 解析失败 → 拒绝 ----------------

def test_unresolvable_host_rejected():
    fake = resolver_mapping({})  # 所有 host 都解析失败
    with pytest.raises(ValueError):
        validate_webhook_url("https://nonexistent.example/hook", resolver=fake)


# ---------------- 6. 企业放行开关 ----------------

def test_allow_private_flag_permits_private_target():
    url = validate_webhook_url(
        "http://10.0.0.5/hook", resolver=resolver_mapping({}), allow_private=True)
    assert url == "http://10.0.0.5/hook"


def test_allow_private_flag_keeps_scheme_gate():
    """放行内网 ≠ 放行任意协议：scheme 闸在开关之前"""
    with pytest.raises(ValueError):
        validate_webhook_url("file:///etc/passwd", allow_private=True)


# ---------------- 7. 正常公网目标放行 ----------------

def test_public_ip_allowed():
    url = validate_webhook_url("https://93.184.216.34/hook")
    assert url == "https://93.184.216.34/hook"


def test_public_domain_allowed_with_fake_resolver():
    fake = resolver_mapping({"hooks.example.com": ["93.184.216.34"]})
    url = validate_webhook_url("https://hooks.example.com/hook", resolver=fake)
    assert url == "https://hooks.example.com/hook"
