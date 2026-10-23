<template>
  <div class="dev-settings-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>开发者设置</span>
          <span class="header-hint">API Key 供 CLI / CI 调用平台接口；Webhook 在评测收尾与门禁失败时推送通知</span>
        </div>
      </template>

      <el-tabs v-model="activeTab">
        <!-- ==================== API Keys ==================== -->
        <el-tab-pane label="API Keys" name="apiKeys">
          <div class="tab-toolbar">
            <el-button type="primary" :icon="Plus" :disabled="readonlyRole" @click="openKeyDialog">创建 API Key</el-button>
            <span class="tab-tip">明文 Key 仅创建时展示一次；scope 决定 Key 可调用的接口范围</span>
          </div>
          <el-table :data="apiKeys" v-loading="keysLoading" style="width: 100%">
            <el-table-column prop="name" label="名称" min-width="140" />
            <el-table-column label="Key 前缀" width="150">
              <template #default="{ row }">
                <code class="key-prefix">{{ row.key_prefix }}…</code>
              </template>
            </el-table-column>
            <el-table-column label="Scopes" min-width="260">
              <template #default="{ row }">
                <el-tag v-for="s in (row.scopes || [])" :key="s" size="small" class="scope-tag">{{ scopeLabel(s) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最近使用" width="165">
              <template #default="{ row }">{{ formatTime(row.last_used_at) || '从未使用' }}</template>
            </el-table-column>
            <el-table-column label="过期时间" width="165">
              <template #default="{ row }">{{ row.expires_at ? formatTime(row.expires_at) : '永不过期' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="keyStatus(row).type">{{ keyStatus(row).label }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90" fixed="right">
              <template #default="{ row }">
                <el-button
                  v-if="!row.revoked_at"
                  link
                  type="danger"
                  @click="revokeKey(row)"
                >吊销</el-button>
                <span v-else class="placeholder-text">-</span>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ==================== Webhooks ==================== -->
        <el-tab-pane label="Webhooks" name="webhooks">
          <div class="tab-toolbar">
            <el-button type="primary" :icon="Plus" :disabled="readonlyRole" @click="openWebhookDialog">创建 Webhook</el-button>
            <span class="tab-tip">HMAC-SHA256 签名头 X-Aievo-Signature 校验；secret 仅创建时展示一次</span>
          </div>
          <el-table :data="webhooks" v-loading="webhooksLoading" style="width: 100%">
            <el-table-column prop="name" label="名称" min-width="130" />
            <el-table-column prop="url" label="接收端 URL" min-width="240" show-overflow-tooltip />
            <el-table-column label="订阅事件" min-width="240">
              <template #default="{ row }">
                <el-tag v-for="e in (row.events || [])" :key="e" size="small" class="scope-tag">{{ eventLabel(e) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="80">
              <template #default="{ row }">
                <el-tag :type="row.active ? 'success' : 'info'">{{ row.active ? '启用' : '停用' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="160" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="openDeliveries(row)">送达历史</el-button>
                <el-button link type="danger" @click="removeWebhook(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <!-- ==================== SSO 企业登录（M4 QE4-1，超管） ==================== -->
        <el-tab-pane v-if="isSuperAdmin" label="SSO 企业登录" name="sso">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            class="sso-alert"
            title="OIDC 授权码 + PKCE（S256）登录。需先在 IdP 侧登记回调地址（下方「回调地址」或默认 /api/v1/auth/oidc/callback），再在部署 .env 设 AUTH_METHODS=local,oidc（纯 SSO 部署可去掉 local）开启入口"
          />
          <el-alert
            v-if="ssoMethodsHint"
            type="warning"
            :closable="false"
            show-icon
            class="sso-alert"
            :title="ssoMethodsHint"
          />
          <el-form :model="ssoForm" label-width="110px" class="sso-form" v-loading="ssoLoading">
            <el-form-item label="Issuer" required>
              <el-input v-model="ssoForm.issuer" placeholder="https://sso.corp.example（服务端拉取 /.well-known/openid-configuration）" />
            </el-form-item>
            <el-form-item label="Client ID" required>
              <el-input v-model="ssoForm.client_id" placeholder="IdP 侧登记的 client_id" />
            </el-form-item>
            <el-form-item label="Client Secret" required>
              <el-input v-model="ssoForm.client_secret" type="password" show-password
                        placeholder="已配置时留空 = 保留原值" />
            </el-form-item>
            <el-form-item label="Scopes">
              <el-input v-model="ssoForm.scopes" placeholder="openid profile email（空格分隔，须含 openid）" />
            </el-form-item>
            <el-form-item label="Claims 映射">
              <div class="claims-row">
                <el-input v-model="ssoForm.claim_email" placeholder="email 字段" />
                <el-input v-model="ssoForm.claim_username" placeholder="用户名字段" />
                <el-input v-model="ssoForm.claim_name" placeholder="显示名字段" />
                <el-input v-model="ssoForm.claim_groups" placeholder="用户组字段" />
              </div>
              <div class="scope-hint">IdP userinfo 中的字段名（标准 OIDC 缺省：email / preferred_username / name / groups）</div>
            </el-form-item>
            <el-form-item label="回调地址">
              <el-input v-model="ssoForm.redirect_uri" placeholder="留空按请求推导；反向代理部署必填完整外部地址" />
            </el-form-item>
            <el-form-item label="启用">
              <el-switch v-model="ssoForm.enabled" />
            </el-form-item>
            <el-form-item>
              <el-button :loading="ssoTesting" @click="testSso">连通性自检</el-button>
              <el-button type="primary" :loading="ssoSaving" @click="saveSso">保存配置</el-button>
            </el-form-item>
          </el-form>

          <el-alert
            v-if="ssoTestResult"
            :type="ssoTestResult.ok ? 'success' : 'error'"
            :closable="false"
            show-icon
            class="sso-alert"
            :title="ssoTestResult.ok
              ? `IdP 连通正常（${ssoTestResult.latency_ms}ms）：授权/换 token/拉 userinfo 端点均已发现`
              : `IdP 连通失败：${ssoTestResult.error}`"
          />

          <el-divider content-position="left">Groups → 团队映射</el-divider>
          <div class="tab-toolbar">
            <el-button type="primary" size="small" :icon="Plus" @click="openMappingDialog">新增映射</el-button>
            <span class="tab-tip">用户组命中首条映射（按创建顺序）即自动归属对应团队并即时建团；未命中保持原团队</span>
          </div>
          <el-table :data="ssoMappings" v-loading="ssoMappingsLoading" style="width: 100%">
            <el-table-column prop="idp_group" label="IdP 用户组" min-width="180" />
            <el-table-column prop="team_code" label="平台团队代码" min-width="140" />
            <el-table-column prop="team_name" label="团队显示名" min-width="140">
              <template #default="{ row }">{{ row.team_name || '-' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="90" fixed="right">
              <template #default="{ row }">
                <el-button link type="danger" @click="removeMapping(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!ssoMappingsLoading && ssoMappings.length === 0" description="暂无映射：SSO 用户将不做团队归属调整" :image-size="60" />
        </el-tab-pane>
        <!-- ==================== LDAP / AD（M4 QE4-2，超管） ==================== -->
        <el-tab-pane v-if="isSuperAdmin" label="LDAP / AD" name="ldap">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            class="sso-alert"
            title="LDAP/AD 只读认证（search + bind）：登录页账密表单旁路验证企业目录账号，成功后自动开户（JIT）并签发本站登录态；不做目录写同步。需在部署 .env 设 AUTH_METHODS 含 ldap（如 local,ldap）开启入口；用户组→团队映射复用「SSO 企业登录」Tab 的映射表"
          />
          <el-form :model="ldapForm" label-width="120px" class="sso-form" v-loading="ldapLoading">
            <el-form-item label="主机" required>
              <el-input v-model="ldapForm.host" placeholder="dc01.corp.test 或 ldap://dc01.corp.test" />
            </el-form-item>
            <el-form-item label="端口 / SSL">
              <div class="claims-row" style="grid-template-columns: 1fr auto auto;">
                <el-input v-model.number="ldapForm.port" placeholder="空=389/636 默认" />
                <el-switch v-model="ldapForm.use_ssl" active-text="LDAPS" />
              </div>
            </el-form-item>
            <el-form-item label="服务账号 DN">
              <el-input v-model="ldapForm.bind_dn" placeholder="CN=svc-aievo,OU=Service,DC=corp,DC=test（openldap 匿名读可留空）" />
            </el-form-item>
            <el-form-item label="服务账号密码">
              <el-input v-model="ldapForm.bind_password" type="password" show-password
                        placeholder="已配置时留空 = 保留原值" />
            </el-form-item>
            <el-form-item label="Base DN" required>
              <el-input v-model="ldapForm.base_dn" placeholder="OU=Users,DC=corp,DC=test" />
            </el-form-item>
            <el-form-item label="用户过滤器" required>
              <el-input v-model="ldapForm.user_filter"
                        placeholder="(&(objectClass=person)(sAMAccountName={username}))——AD 常用；openldap 常用 (uid={username})" />
              <div class="scope-hint">{username} 占位（用户输入做 RFC 4515 转义后再代入，杜绝过滤器注入）</div>
            </el-form-item>
            <el-form-item label="属性映射">
              <div class="claims-row">
                <el-input v-model="ldapForm.attr_username" placeholder="用户名属性" />
                <el-input v-model="ldapForm.attr_email" placeholder="邮箱属性" />
                <el-input v-model="ldapForm.attr_name" placeholder="显示名属性" />
                <el-input v-model="ldapForm.attr_groups" placeholder="用户组属性" />
              </div>
              <div class="scope-hint">AD 缺省 sAMAccountName / mail / displayName / memberOf；openldap 常用 uid / mail / cn / memberOf</div>
            </el-form-item>
            <el-form-item label="启用">
              <el-switch v-model="ldapForm.enabled" />
            </el-form-item>
            <el-form-item>
              <el-button :loading="ldapTesting" @click="testLdap">连通性自检</el-button>
              <el-button type="primary" :loading="ldapSaving" @click="saveLdap">保存配置</el-button>
            </el-form-item>
          </el-form>

          <el-alert
            v-if="ldapTestResult"
            :type="ldapTestResult.ok ? 'success' : 'error'"
            :closable="false"
            show-icon
            class="sso-alert"
            :title="ldapTestResult.ok
              ? `目录连通正常（${ldapTestResult.latency_ms}ms）：服务账号绑定与 base_dn 搜索均通过`
              : `目录连通失败：${ldapTestResult.error}`"
          />
        </el-tab-pane>
        <!-- ==================== License 授权（M6 QE6-1，超管） ==================== -->
        <el-tab-pane v-if="isSuperAdmin" label="License" name="license">
          <el-alert
            :type="licenseAlertType"
            :closable="false"
            show-icon
            class="sso-alert"
            :title="licenseAlertTitle"
          />
          <el-descriptions :column="2" border class="license-desc" v-loading="licenseLoading">
            <el-descriptions-item label="生效档位">
              <el-tag :type="licenseStatus.enterprise ? 'success' : 'info'">
                {{ licenseStatus.enterprise ? 'Enterprise（企业版）' : 'OSS（开源版）' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="授权状态">
              <el-tag :type="statusTagType(licenseStatus.status)">{{ statusLabel(licenseStatus.status) }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="授权客户">{{ licenseStatus.customer || '-' }}</el-descriptions-item>
            <el-descriptions-item label="到期时间">
              {{ licenseStatus.expires_at ? formatTime(licenseStatus.expires_at) : '永久授权' }}
            </el-descriptions-item>
            <el-descriptions-item label="用户规模上限">{{ licenseStatus.max_users ?? '不限' }}</el-descriptions-item>
            <el-descriptions-item label="月度运行上限">{{ licenseStatus.max_monthly_runs ?? '不限' }}</el-descriptions-item>
            <el-descriptions-item label="企业功能位" :span="2">
              <template v-if="(licenseStatus.features || []).length">
                <el-tag v-for="f in licenseStatus.features" :key="f" size="small" class="scope-tag">{{ featureLabel(f) }}</el-tag>
              </template>
              <span v-else class="placeholder-text">未授权任何企业功能位</span>
            </el-descriptions-item>
            <el-descriptions-item label="文件指纹" :span="2">
              <code class="key-prefix">{{ (licenseStatus.fingerprint || '').slice(0, 16) }}…</code>
            </el-descriptions-item>
          </el-descriptions>
          <div class="tab-toolbar">
            <input
              ref="licenseFileInput"
              type="file"
              accept=".json,application/json"
              style="display: none"
              @change="onLicenseFileChange"
            />
            <el-button type="primary" :loading="licenseUploading" @click="pickLicenseFile">
              上传 license 文件
            </el-button>
            <span class="tab-tip">Ed25519 离线验签，上传即生效；验签失败或过期只降级开源版，核心评测不受影响</span>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 新增 groups 映射弹窗 -->
    <el-dialog v-model="mappingDialogVisible" title="新增 Groups → 团队映射" width="520px">
      <el-form :model="mappingForm" label-width="110px">
        <el-form-item label="IdP 用户组" required>
          <el-input v-model="mappingForm.idp_group" placeholder="IdP groups claim 中的组名（精确匹配）" maxlength="200" />
        </el-form-item>
        <el-form-item label="团队代码" required>
          <el-input v-model="mappingForm.team_code" placeholder="平台团队代码（如 QE-TEAM）" maxlength="100" />
        </el-form-item>
        <el-form-item label="团队显示名">
          <el-input v-model="mappingForm.team_name" placeholder="可空；建团队时的显示名" maxlength="200" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="mappingDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="mappingCreating" @click="createMapping">创建</el-button>
      </template>
    </el-dialog>

    <!-- 创建 API Key 弹窗 -->
    <el-dialog v-model="keyDialogVisible" title="创建 API Key" width="560px">
      <el-form :model="keyForm" label-width="90px">
        <el-form-item label="名称" required>
          <el-input v-model="keyForm.name" placeholder="如：CI 门禁 Key" maxlength="100" />
        </el-form-item>
        <el-form-item label="Scopes" required>
          <div class="scope-check-group">
            <el-checkbox-group v-model="keyForm.scopes">
              <el-checkbox v-for="opt in SCOPE_OPTIONS" :key="opt.value" :value="opt.value">
                {{ opt.label }}
              </el-checkbox>
            </el-checkbox-group>
            <div class="scope-hint">CLI 门禁链路至少需要「触发运行 + 读取任务」；数据集上传需「写数据集」</div>
          </div>
        </el-form-item>
        <el-form-item label="过期时间">
          <el-date-picker
            v-model="keyForm.expires_at"
            type="datetime"
            placeholder="留空 = 永不过期"
            style="width: 100%"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="keyDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="keyCreating" @click="createKey">创建</el-button>
      </template>
    </el-dialog>

    <!-- 明文 Key 一次性展示弹窗 -->
    <el-dialog v-model="tokenDialogVisible" title="API Key 创建成功" width="620px" :close-on-click-modal="false">
      <el-alert type="warning" :closable="false" show-icon title="完整 Key 仅此一次展示，关闭后无法再查看，请立即复制保存" />
      <div class="secret-block">
        <code>{{ createdToken }}</code>
        <el-button size="small" type="primary" @click="copySecret(createdToken)">复制</el-button>
      </div>
      <div class="usage-tip">CLI 使用：aievo config add &lt;平台地址&gt; &lt;上方完整 Key&gt;</div>
      <template #footer>
        <el-button type="primary" @click="tokenDialogVisible = false">我已保存</el-button>
      </template>
    </el-dialog>

    <!-- 创建 Webhook 弹窗 -->
    <el-dialog v-model="webhookDialogVisible" title="创建 Webhook" width="560px">
      <el-form :model="webhookForm" label-width="90px">
        <el-form-item label="名称" required>
          <el-input v-model="webhookForm.name" placeholder="如：发布流水线通知" maxlength="100" />
        </el-form-item>
        <el-form-item label="URL" required>
          <el-input v-model="webhookForm.url" placeholder="https://接收端地址（内网目标需服务端放行）" />
        </el-form-item>
        <el-form-item label="事件" required>
          <el-checkbox-group v-model="webhookForm.events">
            <div class="event-check-item" v-for="ev in EVENT_OPTIONS" :key="ev.value">
              <el-checkbox :value="ev.value">{{ ev.label }}</el-checkbox>
              <span class="event-desc">{{ ev.desc }}</span>
            </div>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="webhookDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="webhookCreating" @click="createWebhook">创建</el-button>
      </template>
    </el-dialog>

    <!-- Webhook secret 一次性展示弹窗 -->
    <el-dialog v-model="secretDialogVisible" title="Webhook 创建成功" width="620px" :close-on-click-modal="false">
      <el-alert type="warning" :closable="false" show-icon title="签名 secret 仅此一次展示，请配置到接收端用于校验 X-Aievo-Signature" />
      <div class="secret-block">
        <code>{{ createdSecret }}</code>
        <el-button size="small" type="primary" @click="copySecret(createdSecret)">复制</el-button>
      </div>
      <div class="usage-tip">校验方式：HMAC-SHA256(secret, "时间戳.请求体")，对照请求头 X-Aievo-Signature（±300 秒防重放）</div>
      <template #footer>
        <el-button type="primary" @click="secretDialogVisible = false">我已保存</el-button>
      </template>
    </el-dialog>

    <!-- 送达历史抽屉 -->
    <el-drawer v-model="deliveriesVisible" :title="`送达历史 - ${deliveriesEndpoint?.name || ''}`" size="60%">
      <el-table :data="deliveries" v-loading="deliveriesLoading" style="width: 100%">
        <el-table-column prop="event" label="事件" min-width="190">
          <template #default="{ row }">{{ eventLabel(row.event) || row.event }}</template>
        </el-table-column>
        <el-table-column label="送达状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' ? 'success' : 'danger'">
              {{ row.status === 'success' ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="响应码" width="90">
          <template #default="{ row }">{{ row.response_status ?? '-' }}</template>
        </el-table-column>
        <el-table-column prop="attempts" label="尝试次数" width="90" />
        <el-table-column prop="last_error" label="最后错误" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.last_error || '-' }}</template>
        </el-table-column>
        <el-table-column label="时间" width="165">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!deliveriesLoading && deliveries.length === 0" description="暂无送达记录" />
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, computed, inject, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { apiKeyApi, webhookApi, ssoApi, ldapApi, licenseApi } from '@/api'
import { canWrite } from '@/utils/permissions'

// App 根组件共享用户信息：SSO 配置是超管功能位，非超管隐藏该 Tab
const currentUser = inject('currentUser', null)
const isSuperAdmin = computed(() =>
  currentUser?.value?.super_admin_flag === 1 || currentUser?.value?.superAdminFlag === 1
)

// M4 QE4-3：viewer 只读——Key/Webhook 创建按钮禁用联动
const readonlyRole = computed(() => !canWrite(currentUser.value))

// scope 与事件常量（与后端 app/core/scopes.py、webhook_service.WEBHOOK_EVENTS 对齐）
const SCOPE_OPTIONS = [
  { value: 'tasks:run', label: '触发运行' },
  { value: 'tasks:read', label: '读取任务/门禁' },
  { value: 'results:read', label: '读取结果' },
  { value: 'datasets:read', label: '读数据集' },
  { value: 'datasets:write', label: '写数据集' },
  { value: 'webhooks:manage', label: '管理 Webhook' }
]
const EVENT_OPTIONS = [
  { value: 'task.execution.completed', label: '执行完成', desc: '任务评测完成时推送' },
  { value: 'task.execution.failed', label: '执行失败', desc: '任务评测失败收尾时推送' },
  { value: 'gate.failed', label: '门禁失败', desc: '门禁判定为 failed 时额外推送' },
  // M5 QE5-3：周报生成完成（每周一 08:00 聚合上周覆盖度/提效/门禁通过率）
  { value: 'weekly.report.ready', label: '周报生成', desc: '每周一 08:00 周报生成完成时推送' }
]

const scopeLabel = (value) => SCOPE_OPTIONS.find(o => o.value === value)?.label || value
const eventLabel = (value) => EVENT_OPTIONS.find(o => o.value === value)?.label || value

const activeTab = ref('apiKeys')

// ---------- API Keys ----------
const apiKeys = ref([])
const keysLoading = ref(false)
const keyDialogVisible = ref(false)
const keyCreating = ref(false)
const keyForm = ref({ name: '', scopes: [], expires_at: null })
const tokenDialogVisible = ref(false)
const createdToken = ref('')

const loadKeys = async () => {
  keysLoading.value = true
  try {
    apiKeys.value = await apiKeyApi.list()
  } catch (e) {
    ElMessage.error(e.message || '加载 API Keys 失败')
  } finally {
    keysLoading.value = false
  }
}

const openKeyDialog = () => {
  keyForm.value = { name: '', scopes: ['tasks:run', 'tasks:read'], expires_at: null }
  keyDialogVisible.value = true
}

const createKey = async () => {
  if (!keyForm.value.name.trim()) {
    ElMessage.error('请填写 Key 名称')
    return
  }
  if (!keyForm.value.scopes.length) {
    ElMessage.error('请至少勾选一个 scope')
    return
  }
  keyCreating.value = true
  try {
    const created = await apiKeyApi.create({
      name: keyForm.value.name.trim(),
      scopes: keyForm.value.scopes,
      expires_at: keyForm.value.expires_at || null
    })
    keyDialogVisible.value = false
    createdToken.value = created.token
    tokenDialogVisible.value = true
    loadKeys()
  } catch (e) {
    ElMessage.error(e.message || '创建失败')
  } finally {
    keyCreating.value = false
  }
}

// Key 状态：已吊销 > 已过期 > 正常
const keyStatus = (row) => {
  if (row.revoked_at) return { label: '已吊销', type: 'info' }
  if (row.expires_at && new Date(row.expires_at) < new Date()) return { label: '已过期', type: 'warning' }
  return { label: '有效', type: 'success' }
}

const revokeKey = async (row) => {
  try {
    await ElMessageBox.confirm(
      `确定吊销「${row.name}」？使用该 Key 的 CLI / CI 调用将立即失效（不可恢复）`,
      '吊销 API Key',
      { type: 'warning', confirmButtonText: '吊销', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await apiKeyApi.revoke(row.id)
    ElMessage.success('已吊销')
    loadKeys()
  } catch (e) {
    ElMessage.error(e.message || '吊销失败')
  }
}

// ---------- Webhooks ----------
const webhooks = ref([])
const webhooksLoading = ref(false)
const webhookDialogVisible = ref(false)
const webhookCreating = ref(false)
const webhookForm = ref({ name: '', url: '', events: [] })
const secretDialogVisible = ref(false)
const createdSecret = ref('')
const deliveriesVisible = ref(false)
const deliveriesLoading = ref(false)
const deliveries = ref([])
const deliveriesEndpoint = ref(null)

const loadWebhooks = async () => {
  webhooksLoading.value = true
  try {
    webhooks.value = await webhookApi.list()
  } catch (e) {
    ElMessage.error(e.message || '加载 Webhooks 失败')
  } finally {
    webhooksLoading.value = false
  }
}

const openWebhookDialog = () => {
  webhookForm.value = { name: '', url: '', events: ['task.execution.completed'] }
  webhookDialogVisible.value = true
}

const createWebhook = async () => {
  if (!webhookForm.value.name.trim()) {
    ElMessage.error('请填写名称')
    return
  }
  if (!/^https?:\/\//.test(webhookForm.value.url.trim())) {
    ElMessage.error('URL 需以 http:// 或 https:// 开头')
    return
  }
  if (!webhookForm.value.events.length) {
    ElMessage.error('请至少订阅一个事件')
    return
  }
  webhookCreating.value = true
  try {
    const created = await webhookApi.create({
      name: webhookForm.value.name.trim(),
      url: webhookForm.value.url.trim(),
      events: webhookForm.value.events
    })
    webhookDialogVisible.value = false
    createdSecret.value = created.secret
    secretDialogVisible.value = true
    loadWebhooks()
  } catch (e) {
    ElMessage.error(e.message || '创建失败')
  } finally {
    webhookCreating.value = false
  }
}

const removeWebhook = async (row) => {
  try {
    await ElMessageBox.confirm(
      `确定删除 webhook「${row.name}」？删除后不再推送，历史送达记录一并清除`,
      '删除 Webhook',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await webhookApi.remove(row.id)
    ElMessage.success('已删除')
    loadWebhooks()
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  }
}

const openDeliveries = async (row) => {
  deliveriesEndpoint.value = row
  deliveriesVisible.value = true
  deliveriesLoading.value = true
  try {
    deliveries.value = await webhookApi.deliveries(row.id, { limit: 100 })
  } catch (e) {
    ElMessage.error(e.message || '加载送达历史失败')
  } finally {
    deliveriesLoading.value = false
  }
}

// ---------- SSO 企业登录（M4 QE4-1，超管） ----------
const ssoLoading = ref(false)
const ssoSaving = ref(false)
const ssoTesting = ref(false)
const ssoTestResult = ref(null)
const emptySsoForm = () => ({
  issuer: '', client_id: '', client_secret: '', scopes: 'openid profile email',
  claim_email: 'email', claim_username: 'preferred_username',
  claim_name: 'name', claim_groups: 'groups',
  redirect_uri: '', enabled: true
})
const ssoForm = ref(emptySsoForm())

const loadSsoConfig = async () => {
  ssoLoading.value = true
  try {
    const { configured, config } = await ssoApi.getConfig()
    if (configured && config) {
      ssoForm.value = { ...emptySsoForm(), ...config }
    } else {
      ssoForm.value = emptySsoForm()
    }
  } catch (e) {
    ElMessage.error(e.message || '加载 SSO 配置失败')
  } finally {
    ssoLoading.value = false
  }
}

const saveSso = async () => {
  if (!ssoForm.value.issuer.trim() || !ssoForm.value.client_id.trim()) {
    ElMessage.error('Issuer 与 Client ID 必填')
    return
  }
  ssoSaving.value = true
  try {
    await ssoApi.saveConfig(ssoForm.value)
    ElMessage.success('SSO 配置已保存')
    await loadSsoConfig()
    await loadSsoMethods()
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  } finally {
    ssoSaving.value = false
  }
}

const testSso = async () => {
  if (!ssoForm.value.issuer.trim()) {
    ElMessage.error('请先填写 Issuer')
    return
  }
  ssoTesting.value = true
  ssoTestResult.value = null
  try {
    ssoTestResult.value = await ssoApi.testConnectivity(ssoForm.value)
  } catch (e) {
    ElMessage.error(e.message || '自检请求失败')
  } finally {
    ssoTesting.value = false
  }
}

// AUTH_METHODS 是否已含 oidc/ldap：提示「开了配置没开开关」的半配置态
const ssoMethodsHint = ref('')
const loadSsoMethods = async () => {
  try {
    const { authApi } = await import('@/api')
    const methods = await authApi.methods()
    const enabled = methods.methods || []
    const missing = []
    if (!enabled.includes('oidc')) missing.push('oidc（SSO 登录入口不显示）')
    if (!enabled.includes('ldap')) missing.push('ldap（账密表单不做目录旁路）')
    ssoMethodsHint.value = missing.length
      ? `注意：部署 .env 的 AUTH_METHODS 尚未包含 ${missing.join('、')}`
      : ''
  } catch {
    ssoMethodsHint.value = ''
  }
}

const ssoMappings = ref([])
const ssoMappingsLoading = ref(false)
const mappingDialogVisible = ref(false)
const mappingCreating = ref(false)
const mappingForm = ref({ idp_group: '', team_code: '', team_name: '' })

const loadSsoMappings = async () => {
  ssoMappingsLoading.value = true
  try {
    ssoMappings.value = await ssoApi.listTeamMappings()
  } catch (e) {
    ElMessage.error(e.message || '加载映射失败')
  } finally {
    ssoMappingsLoading.value = false
  }
}

const openMappingDialog = () => {
  mappingForm.value = { idp_group: '', team_code: '', team_name: '' }
  mappingDialogVisible.value = true
}

const createMapping = async () => {
  if (!mappingForm.value.idp_group.trim() || !mappingForm.value.team_code.trim()) {
    ElMessage.error('IdP 用户组与团队代码必填')
    return
  }
  mappingCreating.value = true
  try {
    await ssoApi.addTeamMapping(mappingForm.value)
    ElMessage.success('映射已创建')
    mappingDialogVisible.value = false
    loadSsoMappings()
  } catch (e) {
    ElMessage.error(e.message || '创建失败')
  } finally {
    mappingCreating.value = false
  }
}

const removeMapping = async (row) => {
  try {
    await ElMessageBox.confirm(
      `确定删除映射「${row.idp_group} → ${row.team_code}」？已归属该团队的用户不受影响，仅后续登录不再自动归属`,
      '删除映射',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await ssoApi.removeTeamMapping(row.id)
    ElMessage.success('已删除')
    loadSsoMappings()
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  }
}

// ---------- LDAP / AD（M4 QE4-2，超管） ----------
const ldapLoading = ref(false)
const ldapSaving = ref(false)
const ldapTesting = ref(false)
const ldapTestResult = ref(null)
const emptyLdapForm = () => ({
  host: '', port: null, use_ssl: false, bind_dn: '', bind_password: '',
  base_dn: '', user_filter: '(&(objectClass=person)(sAMAccountName={username}))',
  attr_username: 'sAMAccountName', attr_email: 'mail',
  attr_name: 'displayName', attr_groups: 'memberOf', enabled: true
})
const ldapForm = ref(emptyLdapForm())

const loadLdapConfig = async () => {
  ldapLoading.value = true
  try {
    const { configured, config } = await ldapApi.getConfig()
    ldapForm.value = configured && config ? { ...emptyLdapForm(), ...config } : emptyLdapForm()
  } catch (e) {
    ElMessage.error(e.message || '加载 LDAP 配置失败')
  } finally {
    ldapLoading.value = false
  }
}

const saveLdap = async () => {
  if (!ldapForm.value.host.trim() || !ldapForm.value.base_dn.trim()) {
    ElMessage.error('主机与 Base DN 必填')
    return
  }
  if (!ldapForm.value.user_filter.includes('{username}')) {
    ElMessage.error('用户过滤器必须包含 {username} 占位符')
    return
  }
  ldapSaving.value = true
  try {
    await ldapApi.saveConfig(ldapForm.value)
    ElMessage.success('LDAP 配置已保存')
    await loadLdapConfig()
    await loadSsoMethods()
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  } finally {
    ldapSaving.value = false
  }
}

const testLdap = async () => {
  if (!ldapForm.value.host.trim()) {
    ElMessage.error('请先填写主机')
    return
  }
  ldapTesting.value = true
  ldapTestResult.value = null
  try {
    ldapTestResult.value = await ldapApi.testConnectivity(ldapForm.value)
  } catch (e) {
    ElMessage.error(e.message || '自检请求失败')
  } finally {
    ldapTesting.value = false
  }
}

// ---------- License 授权（M6 QE6-1，超管） ----------
const licenseLoading = ref(false)
const licenseUploading = ref(false)
const licenseFileInput = ref(null)
const emptyLicenseStatus = () => ({
  status: 'missing', tier: 'oss', effective_tier: 'oss', enterprise: false,
  customer: '', expires_at: null, max_users: null, max_monthly_runs: null,
  features: [], message: '', fingerprint: '', loaded_at: null
})
const licenseStatus = ref(emptyLicenseStatus())

const FEATURE_LABELS = {
  audit_export: '审计导出',
  report_white_label: '报告白标',
  report_watermark: '报告水印',
  project_rbac: '项目级 RBAC（预留）',
  scim: 'SCIM 同步（预留）',
  admin_api: 'Admin API（预留）'
}
const featureLabel = (f) => FEATURE_LABELS[f] || f

const statusLabel = (s) => ({
  valid: '有效',
  expired: '已过期（降级开源版）',
  invalid_signature: '验签失败（降级开源版）',
  missing: '未安装'
}[s] || s)

const statusTagType = (s) => ({
  valid: 'success', expired: 'warning', invalid_signature: 'danger', missing: 'info'
}[s] || 'info')

const licenseAlertType = computed(() => statusTagType(licenseStatus.value.status))
const licenseAlertTitle = computed(() => {
  const s = licenseStatus.value
  if (s.status === 'valid' && s.enterprise) return `企业版授权有效（客户：${s.customer}）`
  return s.message || '未上传 license：按开源版运行，核心评测不受影响，企业功能位锁定'
})

const loadLicenseStatus = async () => {
  licenseLoading.value = true
  try {
    licenseStatus.value = { ...emptyLicenseStatus(), ...await licenseApi.status() }
  } catch (e) {
    ElMessage.error(e.message || '加载 license 状态失败')
  } finally {
    licenseLoading.value = false
  }
}

const pickLicenseFile = () => licenseFileInput.value?.click()

const onLicenseFileChange = async (ev) => {
  const file = ev.target.files?.[0]
  ev.target.value = '' // 允许连续重传同一文件
  if (!file) return
  licenseUploading.value = true
  try {
    const form = new FormData()
    form.append('file', file)
    licenseStatus.value = { ...emptyLicenseStatus(), ...await licenseApi.upload(form) }
    ElMessage.success(licenseStatus.value.status === 'valid'
      ? 'license 已生效' : `license 已上传（当前状态：${statusLabel(licenseStatus.value.status)}）`)
  } catch (e) {
    ElMessage.error(e.message || '上传失败')
  } finally {
    licenseUploading.value = false
  }
}

// ---------- 公共 ----------
const formatTime = (value) => {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

const copySecret = async (text) => {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制到剪贴板')
  } catch {
    // 剪贴板 API 不可用（如非 https 环境）：退化提示手动选择
    ElMessage.warning('复制失败，请手动选择文本复制')
  }
}

onMounted(() => {
  loadKeys()
  loadWebhooks()
  if (isSuperAdmin.value) {
    loadSsoConfig()
    loadSsoMappings()
    loadSsoMethods()
    loadLdapConfig()
    loadLicenseStatus()
  }
})
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-hint {
  font-size: 12px;
  color: #909399;
  font-weight: normal;
}

.tab-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.tab-tip {
  font-size: 12px;
  color: #909399;
}

.key-prefix {
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 12px;
  color: #606266;
  background: #F4F4F5;
  padding: 2px 6px;
  border-radius: 4px;
}

.scope-tag {
  margin-right: 4px;
  margin-bottom: 2px;
}

.scope-check-group .el-checkbox {
  margin-right: 16px;
}

.scope-hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
  line-height: 1.5;
}

.event-check-item {
  display: flex;
  align-items: center;
  height: 32px;
}

.event-desc {
  font-size: 12px;
  color: #909399;
}

.secret-block {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 16px;
  padding: 12px;
  background: #F4F4F5;
  border-radius: 8px;
}

.secret-block code {
  flex: 1;
  word-break: break-all;
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 13px;
  color: #1F2937;
}

.usage-tip {
  margin-top: 10px;
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}

.placeholder-text {
  color: #C0C4CC;
}

/* M4 QE4-1：SSO 配置区 */
.sso-alert {
  margin-bottom: 16px;
}

.sso-form {
  max-width: 640px;
}

.claims-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  width: 100%;
}

/* M6 QE6-1：License 状态卡 */
.license-desc {
  margin-bottom: 16px;
  max-width: 760px;
}
</style>
