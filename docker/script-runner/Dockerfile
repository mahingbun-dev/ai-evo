# ============================================================
# ai-evo-script-runner —— 前处理脚本容器化隔离执行镜像（W15）
#
#   构建（仓库根为上下文，本机联网；部署机构建见 README.md 的离线流程）：
#     docker build --platform linux/amd64 \
#       -f docker/script-runner/Dockerfile -t ai-evo-script-runner:latest .
#
#   平台镜像的裁剪版：同基 python:3.12-slim，只装脚本常用依赖集合
#   （packages.txt，与 requirements.txt 同源钉死版本），不含平台代码、
#   平台重依赖（langfuse/deepeval/langgraph 等）与任何凭证——容器内
#   脚本只有「已装依赖 + 本次挂载的工作区」，无平台 import 面。
#
#   同步纪律：packages.txt 必须与 app/services/script_container.py 的
#   RUNNER_IMAGE_PACKAGES 逐行一致（依赖声明校验的静态清单即该常量），
#   tests/unit/test_w15_container_isolation.py::TestRunnerImagePackages
#   做一致性门禁；改任一侧必须同步另一侧并重建镜像。
# ============================================================
FROM --platform=linux/amd64 python:3.12-slim

# pip 源可在构建时覆盖（内网源：--build-arg PIP_INDEX_URL=...）
ARG PIP_INDEX_URL=https://pypi.org/simple

# 脚本常用依赖集合（W15 方案要点：runner 镜像内已装集合）
COPY docker/script-runner/packages.txt /tmp/packages.txt
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r /tmp/packages.txt \
    && rm -f /tmp/packages.txt

# 统一执行 wrapper：与 local 模式同一源文件（app/services/pre_judge_entry.py，
# 仓库根上下文直接 COPY，单一事实源），镜像内固定路径供 docker run 尾部命令引用
COPY app/services/pre_judge_entry.py /opt/runner/pre_judge_entry.py

# 非 root 运行用户（uid/gid 10001，与 SCRIPT_RUNNER_USER 缺省一致）；
# /workspace 为运行时 bind mount 挂载点（只读层里仅建目录）
RUN groupadd -g 10001 runner \
    && useradd -m -u 10001 -g 10001 -s /usr/sbin/nologin runner \
    && mkdir -p /workspace /home/runner \
    && chown -R runner:runner /workspace /home/runner

# 运行必需最小 env（docker run 侧还会显式 -e 注入同一集合；镜像内 ENV
# 保证绕过平台 run_args 直接 docker run 该镜像时行为一致）
ENV PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    HOME=/home/runner

WORKDIR /workspace
USER runner
