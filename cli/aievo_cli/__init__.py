"""aievo-cli：AI-Evo 评测平台命令行工具。

门禁闭环（M1）的 CI 侧入口：API Key 认证触发评测、轮询门禁判定，
并按 0（通过）/ 1（门禁失败）/ 2（错误）退出码供流水线卡发布。
"""

__version__ = "0.1.0"

# CI 消费方约定的退出码：与 docs/user_guide 及 ci-templates/ 文档保持一致
EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_ERROR = 2
