# build-service 镜像（在开发机构建，导出到部署机，见 docs/build-pipeline.md）
#
# 镜像名刻意不叫 build-service：同一台部署机上 anc 项目已有一个同名镜像在跑
# （占用 8180），重名会让 docker load 顶掉它的 tag、进而打断 anc 的流水线。
#
#   docker build --platform linux/amd64 -t ai-evo-build-service:latest tools/build-service/
#   docker save ai-evo-build-service:latest | gzip > ai-evo-build-service.tar.gz
#
# 运行：需要挂载宿主机的 docker.sock（调用 docker run/build/compose）
#
# 必须用 --network host：
#   部署后的健康检查访问的是 http://127.0.0.1:8100/...，那是宿主机上 api 容器
#   发布的端口。若本容器用默认 bridge 网络，127.0.0.1 指向的是它自己，
#   探活必然 connection refused（实测确认）。用宿主网络后 127.0.0.1 即宿主机，
#   这也让 CI runner 用 localhost:8181 调用它变得直接。
#   用宿主网络时不需要 -p 映射（端口直接开在宿主上）。
#
#   docker run -d --name ai-evo-build-service --restart unless-stopped \
#     --network host \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -v /apps/qa-code/ai-evo:/apps/qa-code/ai-evo \
#     -v /apps/qa-code/ai-evo-build:/apps/qa-code/ai-evo-build \
#     -v /apps/qa-code/ai-evo-build-cache:/apps/qa-code/ai-evo-build-cache \
#     -v /apps/qa-code/ai-evo/build-service/config.toml:/app/config.toml:ro \
#     ai-evo-build-service:latest
FROM --platform=linux/amd64 python:3.12-slim

ARG DOCKER_CLI_VERSION=27.5.1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory '*'

# docker CLI（静态二进制）：服务需要调用 docker run/build/compose，
# 通过挂载宿主机的 docker.sock 使用宿主 daemon，本机不跑 daemon。
# 在这里用 curl 安装而不是像 anc 那样把二进制提交进仓库，避免仓库再增大 70MB。
RUN curl -fsSL --retry 5 --retry-all-errors \
        "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
        | tar xz --strip-components=1 -C /usr/local/bin docker/docker \
    && docker --version

# docker compose 插件（compose v2）
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL --retry 5 --retry-all-errors \
        "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
    && docker compose version

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

COPY build_service.py /app/build_service.py

WORKDIR /app
# 8181 而非 8180：同机的 anc build-service 占用了 8180
EXPOSE 8181

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "build_service:app", "--host", "0.0.0.0", "--port", "8181"]
