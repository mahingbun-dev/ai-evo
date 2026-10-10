"""
多种鉴权方式处理模块
支持：none/api_key/bearer/basic/oauth/custom_header/query/login_token
"""
import base64
import time
import json
from typing import Dict, Any, Optional
import httpx
# nest_asyncio 1.6.0 在 Python 3.14 下会破坏 sniffio/anyio 的 asyncio 上下文检测
# （asyncio.current_task() 返回 None），导致 claude-agent-sdk 等抛 NoEventLoopError；
# 3.12（生产镜像）不受影响，故仅在 <3.14 时应用嵌套补丁
import sys
if sys.version_info < (3, 14):
    import nest_asyncio
    # 允许在已有事件循环中嵌套运行asyncio
    nest_asyncio.apply()


class TokenCache:
    """Token缓存管理器（Redis 优先，自动降级到内存缓存）"""
    _cache: Dict[str, Dict[str, Any]] = {}
    _redis_client = None          # None=未初始化，False=不可用，其他=可用客户端
    _REDIS_PREFIX = "token_cache:"

    @classmethod
    def _get_redis(cls):
        """获取 Redis 客户端（懒加载，连接失败时标记为不可用）"""
        if cls._redis_client is None:
            try:
                import redis
                import os
                redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
                client = redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)
                client.ping()
                cls._redis_client = client
            except Exception:
                cls._redis_client = False  # 标记为不可用，后续直接走内存缓存
        return cls._redis_client if cls._redis_client else None

    @classmethod
    def get(cls, key: str) -> Optional[str]:
        """获取token（Redis 优先，降级到内存缓存）"""
        r = cls._get_redis()
        if r:
            try:
                return r.get(cls._REDIS_PREFIX + key)
            except Exception:
                return None
        else:
            # 降级：内存缓存
            entry = cls._cache.get(key)
            if not entry:
                return None
            if time.time() < entry["expires_at"]:
                return entry["token"]
            cls._cache.pop(key, None)
        return None

    @classmethod
    def set(cls, key: str, token: str, expires_in: int = 3600):
        """缓存token（Redis 优先，降级到内存缓存）"""
        r = cls._get_redis()
        if r:
            try:
                r.setex(cls._REDIS_PREFIX + key, expires_in, token)
                return
            except Exception:
                pass
        else:
            # 降级：内存缓存
            cls._cache[key] = {
                "token": token,
                "expires_at": time.time() + expires_in
            }

    @classmethod
    def clear(cls, key: str):
        """清除指定缓存"""
        r = cls._get_redis()
        if r:
            try:
                r.delete(cls._REDIS_PREFIX + key)
            except Exception:
                pass
        else:
            cls._cache.pop(key, None)

    @classmethod
    def clear_all(cls):
        """清除所有缓存"""
        r = cls._get_redis()
        if r:
            try:
                keys = r.keys(cls._REDIS_PREFIX + "*")
                if keys:
                    r.delete(*keys)
            except Exception:
                pass
        else:
            cls._cache.clear()


class AuthHandler:
    """鉴权处理器"""

    @staticmethod
    def get_auth_headers(auth_type: str, auth_config: Dict[str, Any]) -> Dict[str, str]:
        """
        根据鉴权类型获取请求头

        Args:
            auth_type: 鉴权类型
            auth_config: 鉴权配置

        Returns:
            请求头字典
        """
        headers = {}

        if auth_type == "none":
            pass

        elif auth_type == "api_key":
            api_key = auth_config.get("auth_api_key", "")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

        elif auth_type == "bearer":
            token = auth_config.get("auth_token", "")
            if token:
                headers["Authorization"] = f"Bearer {token}"

        elif auth_type == "basic":
            username = auth_config.get("auth_username", "")
            password = auth_config.get("auth_password", "")
            if username and password:
                credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
                headers["Authorization"] = f"Basic {credentials}"

        elif auth_type == "custom_header":
            custom_headers = auth_config.get("auth_custom_headers", {})
            headers.update(custom_headers)

        # OAuth 2.0 需要先获取 token
        elif auth_type == "oauth":
            # OAuth 会在请求时动态获取 token，这里返回空
            pass

        return headers

    @staticmethod
    async def get_oauth_token(auth_config: Dict[str, Any]) -> Optional[str]:
        """
        获取 OAuth 2.0 访问令牌

        Args:
            auth_config: OAuth 配置

        Returns:
            访问令牌
        """
        oauth_url = auth_config.get("auth_oauth_url")
        client_id = auth_config.get("client_id")
        client_secret = auth_config.get("client_secret")

        if not all([oauth_url, client_id, client_secret]):
            return None

        data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(oauth_url, data=data, timeout=30.0)
                if response.status_code == 200:
                    token_data = response.json()
                    return token_data.get("access_token")
        except Exception:
            pass

        return None

    @staticmethod
    async def get_login_token(auth_config: Dict[str, Any], parse_config: Dict[str, Any]) -> Optional[str]:
        """
        通过登录接口获取Token

        Args:
            auth_config: 登录配置，包含 login_url, login_method, login_body 等
            parse_config: 响应解析配置，用于从登录响应中提取 token

        Returns:
            提取的 token 字符串
        """
        import logging
        from app.services.response_parser import ResponseParser
        logger = logging.getLogger(__name__)

        login_url = auth_config.get("login_url")
        if not login_url:
            logger.warning("get_login_token: login_url 为空")
            return None

        login_method = auth_config.get("login_method", "POST").upper()
        login_headers = auth_config.get("login_headers", {})
        login_body = auth_config.get("login_body", {})
        login_params = auth_config.get("login_params", {})

        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                if login_method == "GET":
                    response = await client.get(
                        login_url,
                        params=login_params,
                        headers=login_headers,
                        timeout=60.0
                    )
                else:
                    response = await client.post(
                        login_url,
                        json=login_body,
                        params=login_params,
                        headers=login_headers,
                        timeout=60.0
                    )

                if response.status_code == 200:
                    response_data = response.json()
                    token = ResponseParser.parse(response_data, parse_config)
                    return token
                else:
                    logger.warning(f"get_login_token: 登录失败，状态码 = {response.status_code}")
        except Exception as e:
            logger.error(f"get_login_token: 登录异常 = {str(e)}", exc_info=True)

        return None

    @staticmethod
    def get_login_token_sync(auth_config: Dict[str, Any], parse_config: Dict[str, Any]) -> Optional[str]:
        """
        同步版本：通过登录接口获取Token

        Args:
            auth_config: 登录配置，包含 login_url, login_method, login_body 等
            parse_config: 响应解析配置，用于从登录响应中提取 token

        Returns:
            提取的 token 字符串
        """
        import logging
        from app.services.response_parser import ResponseParser
        logger = logging.getLogger(__name__)

        login_url = auth_config.get("login_url")
        if not login_url:
            logger.warning("get_login_token_sync: login_url 为空")
            return None

        login_method = auth_config.get("login_method", "POST").upper()
        login_headers = auth_config.get("login_headers", {})
        login_body = auth_config.get("login_body", {})
        login_params = auth_config.get("login_params", {})

        try:
            if login_method == "GET":
                response = requests.get(
                    login_url,
                    params=login_params,
                    headers=login_headers,
                    timeout=60.0
                )
            else:
                response = requests.post(
                    login_url,
                    json=login_body,
                    params=login_params,
                    headers=login_headers,
                    timeout=60.0
                )

                if response.status_code == 200:
                    response_data = response.json()
                    token = ResponseParser.parse(response_data, parse_config)
                    return token
                else:
                    logger.warning(f"get_login_token_sync: 登录失败，状态码 = {response.status_code}")
        except Exception as e:
            logger.error(f"get_login_token_sync: 登录异常 = {str(e)}", exc_info=True)

        return None

    @staticmethod
    def get_query_params(auth_type: str, auth_config: Dict[str, Any]) -> Dict[str, str]:
        """
        获取 URL 查询参数

        Args:
            auth_type: 鉴权类型
            auth_config: 鉴权配置

        Returns:
            查询参数字典
        """
        if auth_type == "query":
            return auth_config.get("auth_query_params", {})
        return {}

    @staticmethod
    def prepare_auth(
        auth_type: str,
        auth_config: Dict[str, Any],
        response_parse_config: Optional[Dict] = None,
        model_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        准备鉴权信息，返回 headers 和 params

        Args:
            auth_type: 鉴权类型
            auth_config: 鉴权配置
            response_parse_config: 响应解析配置（用于 login_token 类型）
            model_id: 模型ID（用于 token 缓存）

        Returns:
            包含 headers, params 和 token_header 的字典
        """
        result = {
            "headers": {},
            "params": {},
            "token_header": None,
        }

        # 处理 OAuth
        if auth_type == "oauth":
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # 如果在异步上下文中，使用 create_task
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        token = executor.submit(asyncio.run, AuthHandler.get_oauth_token(auth_config)).result()
                else:
                    token = loop.run_until_complete(AuthHandler.get_oauth_token(auth_config))
            except RuntimeError:
                token = asyncio.run(AuthHandler.get_oauth_token(auth_config))

            if token:
                result["headers"]["Authorization"] = f"Bearer {token}"

        # 处理登录接口 Token 鉴权
        elif auth_type == "login_token":
            import asyncio
            import concurrent.futures
            import logging
            logger = logging.getLogger(__name__)

            # 获取 token_header 类型（用于判断是 Cookie 还是 Authorization）
            token_header = auth_config.get("token_header", "Authorization")
            token_prefix = auth_config.get("token_prefix", "")

            # 尝试从缓存获取 token
            auth_value = None
            cache_key = f"{model_id}_{token_header}" if model_id else None
            if cache_key:
                auth_value = TokenCache.get(cache_key)

            # 缓存中没有 auth_value，重新登录
            if not auth_value:
                # 直接使用同步requests库发送登录请求，避免asyncio嵌套问题
                import requests
                try:
                    login_url = auth_config.get("login_url")
                    login_method = auth_config.get("login_method", "POST").upper()
                    login_headers = auth_config.get("login_headers", {})
                    login_body = auth_config.get("login_body", {})
                    login_params = auth_config.get("login_params", {})

                    if login_method == "GET":
                        response = requests.get(login_url, params=login_params, headers=login_headers, timeout=60)
                    else:
                        response = requests.post(login_url, json=login_body, params=login_params, headers=login_headers, timeout=60)

                    if response.status_code == 200:
                        from app.services.response_parser import ResponseParser
                        response_data = response.json()
                        auth_value = ResponseParser.parse(response_data, response_parse_config or {})
                    else:
                        logger.warning(f"同步登录失败，状态码 = {response.status_code}")
                except Exception as e:
                    logger.error(f"同步登录异常: {str(e)}")

                # 缓存 auth_value
                if auth_value and cache_key:
                    expires_in = auth_config.get("token_expires_in", 3600)
                    TokenCache.set(cache_key, auth_value, expires_in)

            if auth_value:
                # 处理 Authorization 类型的 Bearer 格式
                if token_header.lower() == "authorization":
                    if token_prefix:
                        # 前缀有值，添加空格后拼接（如 "Bearer " + token）
                        auth_value = f"{token_prefix} {auth_value}"
                    else:
                        # 前缀为空，自动添加 "Bearer " 前缀
                        auth_value = f"Bearer {auth_value}"
                else:
                    # 其他类型（如 Cookie），直接添加前缀
                    auth_value = f"{token_prefix}{auth_value}"

                result["headers"][token_header] = auth_value
                result["token_header"] = token_header
            else:
                logger.warning("无法获取认证信息，鉴权头为空")

        else:
            result["headers"] = AuthHandler.get_auth_headers(auth_type, auth_config)

        result["params"] = AuthHandler.get_query_params(auth_type, auth_config)

        return result
