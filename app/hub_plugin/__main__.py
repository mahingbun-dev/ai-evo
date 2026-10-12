"""入口：``python -m app.hub_plugin``（compose 里 SERVICE_TYPE=hub-plugin）。

配置全部来自环境变量：HUB_ADDR / HUB_ADVERTISE_ADDR 必填（见 hubkit.config），
AIEVO_API_BASE / AIEVO_FRONTEND_BASE / AIEVO_TIMEOUT 见 app/hub_plugin/plugin.py。
"""

from __future__ import annotations

import sys

import hubkit

from .plugin import AievoHubPlugin


def main() -> int:
    return hubkit.run(AievoHubPlugin())


if __name__ == "__main__":
    sys.exit(main())
