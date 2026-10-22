FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

# ARCH-0-1：非 root 运行用户（uid/gid 10002，设计 D3 钉死；与 script-runner
# 沙箱用户 10001 分离——主进程被攻破也拿不到沙箱工作区之外的属主身份）。
# 先于一切 COPY 建好用户，后续写面目录可直接 chown；安装/编译步骤仍以 root 执行。
RUN groupadd -g 10002 app \
    && useradd -m -u 10002 -g 10002 -s /usr/sbin/nologin app

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
# ARCH-0-1：原先放 /root/.local，而 /root 为 0700——改为非 root 运行后 uid 10002
# 既读不到也执行不了（claude-agent-sdk 的 which("claude") 会因此找不到 CLI）；
# 改放 /opt/claude（root 属主、全局可读可执行，运行用户不可改写），符号链接不变。
COPY claude-local/ /opt/claude/
RUN ln -sf /opt/claude/bin/claude /usr/local/bin/claude \
    && /usr/local/bin/claude --version
ENV CLAUDE_CODE_PATH=/usr/local/bin/claude

# 安装 git 运行时（插件市场 git 克隆的运行时依赖，见 plugin_marketplace_service）
# 离线安装：与 claude-local 同款模式——运行镜像基础 python:3.12-slim 不含 git，
# 构建机（部署机）无外网 apt 源；git-local/ 由 tools/prepare-git-bin.sh 在有外网
# 的开发机上从同版本基础镜像提取（git 2.47.3 + git-core + 递归 .so 依赖 +
# templates，校验和见 tools/git-bin.json）。
COPY git-local/ /usr/local/git-runtime/
COPY git-local/usrshare/templates/ /usr/share/git-core/templates/
ENV PATH=/usr/local/git-runtime/bin:$PATH
ENV GIT_EXEC_PATH=/usr/local/git-runtime/libexec/git-core
ENV LD_LIBRARY_PATH=/usr/local/git-runtime/lib
RUN git --version

# 复制应用代码
COPY app/ app/
# executor 小包（纯标准库）：app/core/config.py 顶层 import executor.sandbox_params
# （CONTAINER_RUNTIMES 白名单同源）——主镜像漏 COPY 会让 api/worker 启动即
# ModuleNotFoundError（NAS 冒烟实测缺陷，CI 镜像导入冒烟步防回归）
COPY executor/ executor/
# vendored anc-hub 插件 SDK（app/hub_plugin 的运行时骨架；来源与升级方式见
# hubkit/README-vendored.md；`python -m app.hub_plugin` 从 /app 直接 import）
COPY hubkit/ hubkit/
COPY alembic/ alembic/
COPY alembic.ini .

# 复制前端构建产物（由 FastAPI 托管）
# 注意：dist 不再入库，由 tools/build-in-container.sh 里的 `pnpm build` 产出；
# 本地直接 docker build 前需先执行 cd frontend && pnpm build
COPY frontend/dist/ frontend/dist/

# 复制用户使用文档
COPY docs/ docs/

# 复制入口脚本
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# ARCH-0-1：运行期写面收口。/app 下代码、前端产物、文档保持 root 属主（运行用户
# 只读，防篡改；compose 侧 read_only 之后也无写需求），仅 uploads/logs 两个写面
# 预建并 chown 10002：
#   - 命名卷首次创建时 docker 会按镜像内该目录的属主初始化，开箱即可写；
#   - bind 挂载目录属主由宿主决定（docker 自动创建的缺失目录是 root:root），
#     首次部署/历史部署的属主迁移由 compose 的 volume-init 一次性服务完成
#     （docker-entrypoint.sh 的 volume-init 分支），api/worker 启动前再做可写预检。
RUN mkdir -p /app/uploads /app/logs \
    && chown app:app /app/uploads /app/logs

# ARCH-0-1：以非 root 身份运行（设计 D3）。HOME 显式钉到 /home/app（compose 里以
# tmpfs 挂载，Claude Code CLI 的 ~/.claude 配置与会话落在这里）；uvicorn :8100 /
# hub-plugin :9500 均为非特权端口，不需要任何 capability。
ENV HOME=/home/app
USER app

ENTRYPOINT ["docker-entrypoint.sh"]
