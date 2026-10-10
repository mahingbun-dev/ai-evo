FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

# 安装 Python 依赖（完全离线）
#
#   .wheels/          由 tools/prepare-offline.sh 在开发机生成，包含 requirements.lock
#                     的全部 wheel；其中 sdist-only 的包（如 jsonpath）已被预构建
#                     成 wheel，因此这里不需要任何编译工具链
#   requirements.lock 全量钉死版本，包集合与现网运行的一致
#
# numpy 被锁在 2.2.6 并使用 manylinux2014 基线 wheel：部署 VM 的 CPU 缺少
# X86_V2 指令集，新版 numpy（2.3.5 起只有 manylinux_2_28 wheel，编译时启用
# x86-64-v2）会直接崩溃。原先这里是「pip download --platform manylinux2014
# --force-reinstall numpy」的两步特判，现已由 tools/offline-constraints.txt
# 的约束 + 锁文件覆盖，行为一致但不再需要联网。
COPY .wheels /wheels
COPY requirements.lock .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.lock \
    && rm -rf /wheels

# 安装 Claude Code CLI（claude-agent-sdk 裁判引擎的运行时依赖）
# 离线安装：claude-local/ 由 build-service 在构建前从 GitLab Packages 下载
# 就位（版本与 sha256 记录在 tools/claude-bin.json，开发机在 linux/amd64
# 容器内预装打包）。部署机无外网，旧的「apt install curl + 在线跑
# install.sh」在部署机必然失败（2026-10-09 main 构建实测踩坑）。
# claude 为自包含 ELF 二进制，无系统包依赖。
COPY claude-local/ /root/.local/
RUN ln -sf /root/.local/bin/claude /usr/local/bin/claude \
    && /usr/local/bin/claude --version
ENV CLAUDE_CODE_PATH=/usr/local/bin/claude

# 复制应用代码
COPY app/ app/
COPY alembic/ alembic/
COPY alembic.ini .
COPY alembic_local.ini .

# 复制前端构建产物（由 FastAPI 托管）
# 注意：dist 不再入库，由 tools/build-in-container.sh 里的 `pnpm build` 产出；
# 本地直接 docker build 前需先执行 cd frontend && pnpm build
COPY frontend/dist/ frontend/dist/

# 复制用户使用文档
COPY docs/ docs/

# 复制入口脚本
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
