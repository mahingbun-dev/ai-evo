"""executor 包——沙箱执行服务（ARCH-1-1）

成员：
- ``app``            FastAPI 小服务：POST /sandbox/run、GET /sandbox/runs/{run_id}、
                    GET /health；EXECUTOR_TOKEN 鉴权；每次 run 审计落日志
- ``sandbox_params`` docker run 参数构造与白名单校验——与 worker 侧
                    ``app/services/script_container.py`` 共用的单一事实源
"""
