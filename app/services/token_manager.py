"""
Token 管理模块

负责获取和管理 SSO 认证 token。
支持 token 缓存和自动刷新。
"""

import time
import logging
import requests
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

from pathlib import Path


logger = logging.getLogger(__name__)


@dataclass
class TokenInfo:
    """Token 信息"""
    token: str
    expires_at: float = 0  # 过期时间戳
    # 默认 token 长期有效，不设置过期时间

    def is_valid(self) -> bool:
        """检查 token 是否有效"""
        if not self.token:
            return False
        # 如果设置了过期时间，检查是否过期
        if self.expires_at > 0 and time.time() > self.expires_at:
            return False
        return True


class TokenManager:
    """
    Token 管理器

    管理 SSO 认证 token 的获取和缓存。
    """

    _instance: Optional["TokenManager"] = None
    _token_info: Optional[TokenInfo] = None

    def __new__(cls):
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化 Token 管理器"""
        self._base_url: str = ""
        self._login_url: str = ""
        self._username: str = ""
        self._password: str = ""
        self._token_info = None

    def configure(
        self,
        base_url: str,
        login_url: str,
        username: str,
        password: str,
    ):
        """
        配置 Token 管理器

        Args:
            base_url: 基础 URL
            login_url: 登录接口路径（如 /api/users/login）
            username: 用户名
            password: 密码
        """
        self._base_url = base_url.rstrip("/")
        self._login_url = login_url
        self._username = username
        self._password = password
        # 清除旧 token，强制重新获取
        self._token_info = None

    def get_token(self, force_refresh: bool = False) -> Optional[str]:
        """
        获取 token

        Args:
            force_refresh: 强制刷新 token

        Returns:
            token 字符串，获取失败返回 None
        """
        # 检查现有 token 是否有效
        if not force_refresh and self._token_info and self._token_info.is_valid():
            return self._token_info.token

        # 获取新 token
        return self._refresh_token()

    def _refresh_token(self) -> Optional[str]:
        """刷新 token"""
        if not all([self._base_url, self._login_url, self._username, self._password]):
            logger.error("Token 管理器未正确配置")
            return None

        url = f"{self._base_url}{self._login_url}"
        payload = {
            "username": self._username,
            "password": self._password,
        }
        headers = {"Content-Type": "application/json"}

        try:
            logger.info(f"正在获取 SSO token...")
            response = requests.post(url, json=payload, headers=headers, timeout=30)

            if response.status_code != 200:
                logger.error(f"SSO 登录请求失败: {response.status_code} - {response.text}")
                return None

            data = response.json()

            if data.get("code") != 200:
                error_msg = data.get("msg", "未知错误")
                logger.error(f"SSO 登录失败: {error_msg}")
                return None

            token = data.get("data", {}).get("token")
            if not token:
                logger.error("响应中未找到 token")
                return None

            # 保存 token（假设 token 长期有效，不设置过期时间）
            self._token_info = TokenInfo(token=token)
            logger.info("SSO token 获取成功")

            return token

        except requests.RequestException as e:
            logger.error(f"SSO 登录请求异常: {e}")
            return None
        except Exception as e:
            logger.error(f"SSO 登录失败: {e}")
            return None

    def clear_cache(self):
        """清除 token 缓存"""
        self._token_info = None

    @property
    def is_configured(self) -> bool:
        """检查是否已配置"""
        return all([self._base_url, self._login_url, self._username, self._password])


# 全局 Token 管理器实例
_token_manager = TokenManager()


def get_token_manager() -> TokenManager:
    """获取全局 Token 管理器实例"""
    return _token_manager


def configure_token_manager(
    base_url: str,
    login_url: str,
    username: str,
    password: str,
):
    """配置全局 Token 管理器"""
    _token_manager.configure(base_url, login_url, username, password)


def get_sso_token(
    base_url: str,
    login_url: str,
    username: str,
    password: str,
    force_refresh: bool = False,
) -> Optional[str]:
    """
    便捷函数：获取 SSO token

    Args:
        base_url: 基础 URL
        login_url: 登录接口路径
        username: 用户名
        password: 密码
        force_refresh: 强制刷新

    Returns:
        token 字符串
    """
    configure_token_manager(base_url, login_url, username, password)
    return _token_manager.get_token(force_refresh)
