"""ai-evo 的 anc-hub 插件（sidecar）。

把 ai-evo 评测平台的核心能力暴露为 anc-hub MCP 工具，供任意 agent 经中台调用：
数据集管理/导入、评测任务的 CRUD 与执行、状态与结果查询。

插件本体是纯 HTTP 代理——不复制 ai-evo 的业务逻辑，所有请求转发到同机的
ai-evo REST API（/api/v1）。调用方的平台登录态（hub 透传的 meta 键
``hub.mas_token``）被组装成 ``Authorization: Bearer`` 头原样转发，ai-evo 校验
JWT 并加载对应用户，因此所有操作都以真实用户身份执行，团队归属与操作审计
自然生效。

运行方式（与 api/worker 共用镜像，compose 里 SERVICE_TYPE=hub-plugin）::

    python -m app.hub_plugin

配置环境变量见 :mod:`hubkit.config`（HUB_ADDR/HUB_ADVERTISE_ADDR/...）与
本包的 ``AIEVO_API_BASE``/``AIEVO_FRONTEND_BASE``/``AIEVO_TIMEOUT``。
"""
