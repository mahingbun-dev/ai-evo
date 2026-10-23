# License 生成工具（M6 QE6-1）

Ed25519 离线 license 三件套：密钥对生成 / 签发 / 验签。平台侧对应实现在
`app/services/license_service.py`（启动加载 + 验签 + 缓存，失败只降级开源版
不阻断核心评测），超管上传入口在「开发者设置 → License」。

## 用法

一律用仓库虚拟环境执行：

```bash
# 1. 生成密钥对（测试密钥已入库 tools/license/keys/，勿重复生成覆盖；
#    生产密钥 --out-dir 必须指到仓库外，由厂商保管，绝不入库）
.venv/bin/python tools/license/gen.py keygen --out-dir /secure/path --name prod_signing_key

# 2. 签发 license（claims：客户/档位/到期/规模/企业功能位）
.venv/bin/python tools/license/gen.py issue \
    --customer "ACME 科技" --tier enterprise \
    --expires 2027-12-31 --max-users 50 --max-monthly-runs 5000 \
    --features audit_export,report_white_label \
    --key /secure/path/prod_signing_key --out acme-license.json

# 3. 验签自检（缺省用仓库测试公钥；生产加 --pubkey 指向厂商公钥）
.venv/bin/python tools/license/gen.py verify --file acme-license.json
```

## license 文件格式（自包含 JSON 信封）

```json
{
  "alg": "Ed25519",
  "payload": "<base64url(canonical JSON claims，排序键+紧分隔符)>",
  "sig": "<base64url(Ed25519 签名，对 payload 解码后的字节原文签名)>"
}
```

claims 字段：`customer`（必填）、`tier`（`oss`/`enterprise`，必填）、
`issued_at`（签发时间 ISO8601）、`expires_at`（可空=永久）、`max_users`、
`max_monthly_runs`（可空=不限，续约对账口径）、`features[]`（企业功能位：
`audit_export` / `report_white_label` / `report_watermark` / `project_rbac`（预留）/
`scim`（预留）/ `admin_api`（预留））。

## 密钥纪律

- **测试密钥对有意入库**（`keys/test_signing_key` / `keys/test_signing_key.pub`），
  平台 `LICENSE_PUBLIC_KEY` 缺省值即测试公钥——开发与 CI 用 gen 出的 license
  开箱可验。若确要轮换测试密钥，必须同步 `app/core/config.py` 缺省值
  （有单测静态断言把关）；
- **生产私钥由厂商持有、绝不入库**：签发时 `--key` 指向仓库外私钥文件；
  生产部署 `.env` 的 `LICENSE_PUBLIC_KEY` 必须覆盖为厂商公钥，否则测试私钥
  签出的 license 也能通过验签（等于没设防）；
- 验签失败 / 过期的 license：平台只降级开源版（企业功能位锁定）并醒目告警，
  **不阻断核心评测**——降级路径见 `tests/security/test_license_admin_api.py`
  `TestDegradationDrill`。
