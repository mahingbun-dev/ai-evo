"""
Webhook URL SSRF 守卫（M1 安全基建；QE1-3 投递通道接线）

纯函数校验：webhook 目标 URL 必须是 http/https 且解析出的全部地址均非
内网/环回/链路本地（方案 §M1「Webhook-安全」）。企业内网接收端场景可经
``WEBHOOK_ALLOW_PRIVATE_TARGETS=true`` 显式放行（默认关）。

设计要点：
- **解析后校验**（不只查字面量）：域名解析到私网 IP（DNS 重绑定/内网域名）
  同样拒绝——resolver 可注入，单测用假解析器，不依赖真实 DNS；
- 校验全部解析结果，任一命中私网即整体拒绝（不取「第一个公网地址」，
  防多 A 记录混排绕过）；
- QE1-3 投递时须**每次发送前重新调用本校验**（注册时校验通过≠发送时
  仍指向公网——TTL/DNS 切换可把已注册域名重绑到内网）。
"""
import ipaddress
import socket
from typing import Callable, List, Optional
from urllib.parse import urlsplit

from app.core.config import get_settings

# 白名单端口策略：不限制端口（企业接收端 8080/9000 常见），SSRF 面由
# 地址校验兜底；协议外（file/gopher/ftp/data 等）在 scheme 闸即拒


def _resolve_host(host: str, port: int) -> List[str]:
    """默认解析器：getaddrinfo 全家（IPv4+IPv6）→ 地址字符串列表"""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def _is_disallowed_ip(ip_str: str) -> Optional[str]:
    """地址命中禁止段则返回原因文案，公网地址返回 None"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return f"无法解析的地址 {ip_str!r}"
    if ip.is_loopback:
        return f"环回地址 {ip}"
    if ip.is_private:
        return f"私网地址 {ip}"
    if ip.is_link_local:
        return f"链路本地地址 {ip}"
    if ip.is_unspecified:
        return f"未指定地址 {ip}"
    if ip.is_reserved:
        return f"保留地址 {ip}"
    if ip.is_multicast:
        return f"组播地址 {ip}"
    return None


def validate_webhook_url(
    url: str,
    *,
    resolver: Optional[Callable[[str, int], List[str]]] = None,
    allow_private: Optional[bool] = None,
) -> str:
    """校验 webhook 目标 URL，非法抛 ValueError（含原因），合法返回规范化 URL

    Args:
        url: 待校验的完整 URL
        resolver: 主机解析器（默认 getaddrinfo；单测注入假解析器）
        allow_private: 是否放行内网目标（默认读 WEBHOOK_ALLOW_PRIVATE_TARGETS）
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("webhook URL 不能为空")

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"webhook URL 仅允许 http/https，当前 scheme：{parts.scheme or '(空)'}")
    host = parts.hostname
    if not host:
        raise ValueError("webhook URL 缺少主机名")

    if allow_private is None:
        allow_private = get_settings().WEBHOOK_ALLOW_PRIVATE_TARGETS
    if allow_private:
        # 企业内网接收端显式放行：scheme/主机闸已过即可
        return url

    port = parts.port or (443 if parts.scheme == "https" else 80)
    resolve = resolver or _resolve_host
    try:
        addresses = resolve(host, port)
    except Exception as exc:
        raise ValueError(f"webhook 主机 {host!r} 解析失败：{exc}") from exc
    if not addresses:
        raise ValueError(f"webhook 主机 {host!r} 未解析到任何地址")

    for addr in addresses:
        reason = _is_disallowed_ip(addr)
        if reason:
            raise ValueError(f"webhook 目标 {host!r} 解析到{reason}，已拒绝（SSRF 防护）")
    return url
