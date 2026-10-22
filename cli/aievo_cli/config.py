"""aievo-cli 配置管理：~/.aievo/config.toml 按 profile 保存平台地址与 API Key。

配置文件含明文 API Key，写入时收紧为 0600；测试通过 AIEVO_CONFIG_DIR
环境变量重定向到临时目录。
"""
import os
import tomllib
from pathlib import Path

DEFAULT_PROFILE = "default"


def config_dir() -> Path:
    """配置目录（AIEVO_CONFIG_DIR 可重定向，便于测试与多环境隔离）"""
    return Path(os.environ.get("AIEVO_CONFIG_DIR", Path.home() / ".aievo"))


def config_path() -> Path:
    return config_dir() / "config.toml"


def _escape(value: str) -> str:
    """TOML 基本字符串转义（配置值只含 URL 与 Key，转义引号/反斜杠足够）"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def load_config() -> dict:
    """读取配置文件，不存在或损坏时返回空配置（不抛错，由命令层提示）"""
    path = config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def save_profile(name: str, url: str, api_key: str) -> Path:
    """保存/覆盖一个 profile（url 去尾部斜杠归一化），返回配置文件路径"""
    config = load_config()
    profiles = config.setdefault("profile", {})
    profiles[name] = {
        "url": url.rstrip("/"),
        "api_key": api_key,
    }

    config_dir().mkdir(parents=True, exist_ok=True)
    path = config_path()
    lines = []
    for profile_name, values in profiles.items():
        lines.append(f"[profile.{_escape(profile_name)}]")
        for key, value in values.items():
            lines.append(f"{key} = \"{_escape(value)}\"")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    # 明文 API Key 落盘，权限收紧为仅属主可读写
    os.chmod(path, 0o600)
    return path


def get_profile(name: str = DEFAULT_PROFILE) -> dict:
    """取指定 profile；不存在时抛 KeyError（命令层转为人话提示）"""
    profiles = load_config().get("profile", {})
    if name not in profiles:
        raise KeyError(name)
    return profiles[name]


def list_profiles() -> dict:
    return load_config().get("profile", {})


def mask_key(api_key: str) -> str:
    """日志友好：仅保留前 12 位用于核对，避免完整 Key 进 CI 日志"""
    if len(api_key) <= 12:
        return api_key[:4] + "****"
    return api_key[:12] + "****"
