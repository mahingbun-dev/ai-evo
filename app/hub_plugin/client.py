"""ai-evo REST API 的瘦客户端：Bearer 鉴权注入 + 错误翻译。

hub 透传的平台登录态（meta 键 ``hub.mas_token``）就是 ai-evo 签发的登录
token——ai-evo 的鉴权中间件按 ``Authorization: Bearer <token>`` 校验并加载
用户，因此这里只组一个 Bearer 头，ai-evo 侧的团队归属与操作审计全部按真实
用户自然生效，插件不持有也不缓存任何凭证。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import requests


class AievoApiError(Exception):
    """ai-evo 不可达/超时等基础设施错误（区别于 ai-evo 返回的业务失败）。"""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class AievoClient:
    """线程安全的瘦 HTTP 客户端（requests.Session 内部有连接池锁）。"""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self._base = (base_url or "http://127.0.0.1:8100").rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()

    @property
    def base_url(self) -> str:
        return self._base

    def _headers(self, mas_token: str) -> Dict[str, str]:
        # 只组这一个头：ai-evo 鉴权中间件按 Bearer token 校验并加载用户
        return {"Authorization": f"Bearer {mas_token}"}

    def request(
        self,
        method: str,
        path: str,
        mas_token: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """执行一次请求，返回 ``{"status": int, "data": Any}``。

        ``timeout`` 为单次请求的超时覆盖值（慢操作放宽用），缺省用客户端级超时。
        网络层故障（超时/连接失败）抛 :class:`AievoApiError`；ai-evo 返回什么
        HTTP 状态码都原样带回来（401/422/500 是业务结果，由调用方翻译）。
        """
        effective_timeout = self._timeout if timeout is None else float(timeout)
        url = f"{self._base}{path}"
        try:
            resp = self._session.request(
                method,
                url,
                headers=self._headers(mas_token),
                params=params,
                json=json_body,
                timeout=effective_timeout,
            )
        except requests.exceptions.Timeout as e:
            raise AievoApiError(
                "timeout",
                f"请求 ai-evo 超时（{effective_timeout}s）：{url}",
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise AievoApiError(
                "unreachable",
                f"无法连接 ai-evo {self._base}（{url}）：{e}",
            ) from e

        try:
            data: Any = resp.json()
        except ValueError:
            data = resp.text
        return {"status": resp.status_code, "data": data}

    def health(self, timeout: float = 5.0) -> bool:
        """探测 ai-evo 是否可达（/api/v1/auth/status 在鉴权白名单内，无需登录态）。"""
        try:
            resp = self._session.get(
                f"{self._base}/api/v1/auth/status",
                timeout=timeout,
            )
            return resp.status_code < 500
        except Exception:
            return False
