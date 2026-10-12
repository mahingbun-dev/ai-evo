# hubkit（vendored）

本目录是内部中台 anc-gateway（开源对应 [plugin-hub](https://github.com/mahingbun-dev/plugin-hub)）
`sdk/python/hubkit` 的 vendored 拷贝，作为 `app/hub_plugin`（anc-hub 插件 sidecar）的运行时骨架。

## 来源

| 项 | 值 |
|---|---|
| 源仓库 | anc-gateway（anc-hub 中台）`sdk/python/hubkit` |
| 拷贝时 commit | `9c6ecc3`（Merge branch 'feat/auth-credential-passthrough' into 'main'） |
| 拷贝时间 | 2026-10-12 |
| 拷贝方式 | 整包 rsync，排除 `__pycache__` / `*.egg-info` |
| 运行时依赖 | `grpcio==1.84.0`、`protobuf==7.36.2`（requirements.txt 已钉，版本下限由 `hubkit/proto/hubv1` 生成代码在 import 期硬校验，低了直接抛错） |

包内自带（无需另行安装）：

- `hubkit/proto/hubv1/`：proto 生成代码（已提交，无需 protoc）；
- `hubkit/conformance/`：契约一致性自检（L1/L2）；
- `hubkit/mockhub/`：本地 mock 中台（离线测试用）。

## 为什么 vendored 而不是 pip 依赖

anc-gateway 的 Python SDK 未发布 PyPI；部署机无外网、镜像构建走离线 wheels
快照（`tools/prepare-offline.sh`）。vendored 拷贝让构建与部署完全自包含，
与中台其他插件工程的做法同构（插件工程普遍把 SDK 整份拷进
`<工程>/sdk/`）。

## 升级方式

1. 在 anc-gateway 仓库确认目标 commit（`sdk/python/hubkit` 无未提交改动）；
2. 重新 rsync 整包覆盖本目录（同样排除 `__pycache__` / `*.egg-info`）；
3. 核对 `sdk/python/pyproject.toml` 的 `dependencies` 版本下限，若变化则同步
   `requirements.txt` 与 `tools/prod-constraints.txt` 并重跑离线三步曲
   （`prepare-offline.sh` → `upload-offline-snapshot.sh` → 提交 lock/快照）；
4. 跑 `tests/unit/hub_plugin/`（含 L1 conformance）确认契约兼容；
5. 插件契约（工具清单/描述）有变化时**必须升 `app/hub_plugin/manifest.py`
   的 PLUGIN_VERSION**——中台拒绝同号不同契约的注册（VERSION_CONFLICT）。

## 不要改这里

对 hubkit 的任何本地改动都会让「升级 = 重新拷贝」变脏。发现 SDK 问题请去
anc-gateway 修，升级后重新 vendored。
