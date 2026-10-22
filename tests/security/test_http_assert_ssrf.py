"""M2 安全测试：http_assert 校验器出网面 SSRF 防护

攻击路径复现（对象 app/services/verifier_runner._run_http_assert_verifier——
终态校验器新增的 worker 侧外呼通道，默认拒绝内网/环回/保留段，与 webhook
投递共用 webhook_url_guard 的解析后校验原语）：

1. 配置校验（validate_verifier_shape）：file/gopher/无 scheme 的 url 在任务
   保存前即拒绝（配置面无外呼语义）；
2. 执行面默认 deny：localhost/127.0.0.1/私网/链路本地/十六进制变形等目标
   → 校验器 error 且 requests 层零触达（不发任何包）；
3. DNS 解析到内网（假解析器注入，防重绑定）：守卫拒绝后校验器 error；
4. 显式 allow_private=true（任务配置可审计的内网回调端点）→ 守卫放行、
   请求发出（本测试不真实出网：requests 假体）——需管理员总闸
   HTTP_ASSERT_ALLOW_PRIVATE_TARGETS=true（默认关，控制权在部署管理员）；
5. 响应体断言不放大攻击面：body_snippet 截断，evidence 不回传完整内网响应；
6. 重定向不跟随（allow_redirects=False）：初始目标 302 跳内网/环回时请求
   不跟随、重定向目标零触达、内网响应体不进 evidence（回环双 server PoC）。
"""
import json
from unittest.mock import patch

import pytest

from app.services.verifier_runner import (
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    run_single_verifier,
    validate_verifier_shape,
)

pytestmark = pytest.mark.unit


def _run_http(config, run_dir):
    return run_single_verifier(
        {"type": "http_assert", "name": "回调校验", "config": config},
        task_id="task_sec", data_id="d1", run_dir=run_dir,
        model_output="mo", data={}, payload={},
    )


# ---------------- 1. 配置面：scheme 闸（保存前拒绝） ----------------

@pytest.mark.parametrize("bad_url", [
    "file:///etc/passwd",
    "gopher://127.0.0.1:6379/_STAT",
    "ftp://evil.example/x",
    "unix:///var/run/docker.sock",
    "not-a-url",
])
def test_config_rejects_non_http_schemes(bad_url):
    with pytest.raises(ValueError, match="http"):
        validate_verifier_shape([{"type": "http_assert", "config": {"url": bad_url}}])


# ---------------- 2. 执行面：内网目标默认拒绝且零外呼 ----------------

@pytest.mark.parametrize("url", [
    "http://localhost/callback",
    "http://127.0.0.1:8080/callback",
    "http://0.0.0.0/callback",
    "http://10.1.2.3/callback",          # 10/8 私网
    "http://172.16.0.9/callback",        # 172.16/12 私网
    "http://192.168.1.9/callback",       # 192.168/16 私网
    "http://169.254.169.254/latest/meta-data",  # 链路本地（云元数据端点）
    "http://0x7f000001/callback",        # 十六进制环回变形
    "http://2130706433/callback",        # 十进制环回变形
])
def test_private_targets_denied_without_request(url, tmp_path):
    with patch("requests.request") as mock_request:
        entry = _run_http({"url": url}, tmp_path)
    assert entry["status"] == STATUS_ERROR
    assert "egress" in entry["error"] or "拒绝" in entry["error"]
    assert not mock_request.called  # 守卫拒绝在请求发起前——零外呼


# ---------------- 3. DNS 解析到内网（重绑定形态，假解析器） ----------------

def test_dns_rebinding_to_private_denied(tmp_path):
    """公网域名解析出私网地址 → 拒绝（校验全部解析结果，防多记录混排）"""
    from app.services import webhook_url_guard

    original = webhook_url_guard.validate_webhook_url

    def _guarded_validator(url, **kwargs):
        # 假解析器：同域名解析出「公网 + 私网」混排记录（重绑定形态）
        return original(url, resolver=lambda host, port: ["203.0.113.10", "10.0.0.9"],
                        **kwargs)

    # _run_http_assert_verifier 在调用点局部 import 守卫——patch 源模块属性即生效
    with patch.object(webhook_url_guard, "validate_webhook_url",
                      side_effect=_guarded_validator), \
         patch("requests.request") as mock_request:
        entry = _run_http({"url": "https://public-looking.example.com/cb"}, tmp_path)
    assert entry["status"] == STATUS_ERROR
    assert not mock_request.called


# ---------------- 4. 显式 allow_private（双重声明：管理员总闸 + 任务意向） ----------------

class _FakeResponse:
    status_code = 200
    text = '{"ok":true}'

    def json(self):
        return {"ok": True}


def _patch_admin_switch(enabled: bool):
    """patch 管理员总闸 HTTP_ASSERT_ALLOW_PRIVATE_TARGETS（get_settings 局部
    import 自 app.core.config，patch 源模块属性即生效）"""
    from types import SimpleNamespace

    from unittest.mock import patch as _patch

    return _patch("app.core.config.get_settings",
                  return_value=SimpleNamespace(
                      HTTP_ASSERT_ALLOW_PRIVATE_TARGETS=enabled))


def test_allow_private_without_admin_switch_rejected(tmp_path):
    """任务声明 allow_private 但管理员总闸未开 → 拒绝且零外呼

    SSRF 控制面与 M1 webhook 同口径：放行内网目标的决定权在部署管理员，
    任务创建者不能自行声明后直连内网。
    """
    with _patch_admin_switch(False), patch("requests.request") as mock_request:
        entry = _run_http({
            "url": "http://10.0.0.5:9000/callback",
            "allow_private": True,
        }, tmp_path)
    assert entry["status"] == STATUS_ERROR
    assert "HTTP_ASSERT_ALLOW_PRIVATE_TARGETS" in entry["error"]
    assert not mock_request.called


def test_allow_private_opt_in_sends_request(tmp_path):
    """管理员总闸开启 + 任务显式声明 → 守卫放行、请求发出（不出网：假体）"""
    with _patch_admin_switch(True), \
         patch("requests.request", return_value=_FakeResponse()) as mock_request:
        entry = _run_http({
            "url": "http://10.0.0.5:9000/callback",
            "allow_private": True, "expect_status": 200,
        }, tmp_path)
    assert entry["status"] == STATUS_PASS
    assert mock_request.called
    # evidence 不携带响应头/内部地址扩展信息，只保留断言所需字段
    evidence = entry["evidence"]
    assert set(evidence) >= {"url", "method", "status_code", "checks"}


# ---------------- 5. 响应体截断 ----------------

def test_body_snippet_truncated_in_evidence(tmp_path):
    big = "x" * 5000

    class _BigResponse(_FakeResponse):
        text = big

    with patch("requests.request", return_value=_BigResponse()):
        entry = _run_http({"url": "http://93.184.216.34/cb"}, tmp_path)
    snippet = entry["evidence"]["body_snippet"]
    assert len(snippet) <= 600  # HTTP_BODY_SNIPPET_LIMIT=500 + 截断余量
    assert snippet.startswith("xxx")


# ---------------- 6. 重定向不跟随（302 → 内网的 SSRF 绕过面） ----------------

def test_request_issued_with_redirects_disabled(tmp_path):
    """requests.request 必须显式 allow_redirects=False（回归锚点）

    egress 守卫只校验初始 URL——跟随重定向时「公网目标 302 跳内网/环回」
    即绕过守卫并把内网响应体写进 evidence。
    """
    with patch("requests.request", return_value=_FakeResponse()) as mock_request:
        _run_http({"url": "http://93.184.216.34/cb"}, tmp_path)
    kwargs = mock_request.call_args.kwargs
    assert kwargs.get("allow_redirects") is False


def test_redirect_to_private_target_not_followed(tmp_path):
    """302 → 内网/环回：请求不跟随，重定向目标零触达、响应体不进 evidence

    回环 PoC（评审同型场景）：守卫对初始 URL 放行（此处 patch 守卫模拟公网
    目标），初始目标 302 跳环回第二服务——修复前 requests 会跟随并把
    LEAKED 内容写进 body_snippet；修复后拿到的是 302 本身。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from app.services import webhook_url_guard

    # 类属性传参（嵌套类的方法读不到外层函数局部名，不用闭包）
    class _RedirectHandler(BaseHTTPRequestHandler):
        target_url = ""

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", self.target_url)
            self.end_headers()

        def log_message(self, *args):  # 静默测试服务器日志
            pass

    class _LeakedHandler(BaseHTTPRequestHandler):
        hits = 0

        def do_GET(self):
            type(self).hits += 1
            body = b"LEAKED-INTERNAL-SECRET"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    redirector = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    leaked = ThreadingHTTPServer(("127.0.0.1", 0), _LeakedHandler)
    _LeakedHandler.hits = 0
    _RedirectHandler.target_url = f"http://127.0.0.1:{leaked.server_address[1]}/secret"
    threads = [threading.Thread(target=s.serve_forever, daemon=True)
               for s in (redirector, leaked)]
    try:
        for t in threads:
            t.start()
        # 守卫放行初始 URL（模拟公网目标——守卫语义由 1-3 节独立覆盖）
        with patch.object(webhook_url_guard, "validate_webhook_url", lambda *a, **k: None):
            entry = _run_http(
                {"url": f"http://127.0.0.1:{redirector.server_address[1]}/cb",
                 "expect_status": 200},
                tmp_path)
    finally:
        for s in (redirector, leaked):
            s.shutdown()
            s.server_close()

    assert _LeakedHandler.hits == 0  # 重定向目标零触达（未跟随）
    assert entry["evidence"]["status_code"] == 302  # 断言对象是 302 本身
    assert "LEAKED" not in entry["evidence"]["body_snippet"]
    assert entry["status"] == STATUS_FAIL  # 302 ≠ 期望 200 → 断言未通过
