<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <div class="login-logo">
          <el-icon :size="26"><Monitor /></el-icon>
        </div>
        <h1 class="login-title">AgentEvo</h1>
        <p class="login-subtitle">AI 评测平台</p>
      </div>

      <!-- M4 QE4-1：SSO 登录入口（AUTH_METHODS 含 oidc 且已配置 IdP 时显示） -->
      <el-button
        v-if="authMethods.oidc"
        type="primary"
        plain
        class="submit-btn sso-btn"
        :disabled="!authMethods.oidcConfigured"
        @click="startSsoLogin"
      >
        {{ authMethods.oidcConfigured ? '使用企业账号登录（SSO）' : 'SSO 未配置，请联系管理员' }}
      </el-button>
      <el-divider v-if="authMethods.oidc && passwordFormVisible" class="sso-divider">或</el-divider>

      <el-tabs v-if="passwordFormVisible" v-model="activeTab" class="login-tabs" stretch>
        <!-- 登录 -->
        <el-tab-pane label="登录" name="login">
          <el-form
            ref="loginFormRef"
            :model="loginForm"
            :rules="loginRules"
            label-position="top"
            size="large"
            @keyup.enter="handleLogin"
          >
            <el-form-item prop="username" label="用户名">
              <el-input
                v-model="loginForm.username"
                placeholder="请输入用户名"
                :prefix-icon="User"
                autocomplete="username"
              />
            </el-form-item>
            <el-form-item prop="password" label="密码">
              <el-input
                v-model="loginForm.password"
                type="password"
                placeholder="请输入密码"
                :prefix-icon="Lock"
                show-password
                autocomplete="current-password"
              />
            </el-form-item>
            <div v-if="ldapOnly" class="ldap-hint">使用企业目录账号登录（LDAP/AD，由管理员统一配置）</div>
            <el-button
              type="primary"
              class="submit-btn"
              :loading="loading"
              @click="handleLogin"
            >
              登 录
            </el-button>
          </el-form>
        </el-tab-pane>

        <!-- 注册 -->
        <el-tab-pane v-if="registerEnabled" label="注册" name="register">
          <el-form
            ref="registerFormRef"
            :model="registerForm"
            :rules="registerRules"
            label-position="top"
            size="large"
            @keyup.enter="handleRegister"
          >
            <el-form-item prop="username" label="用户名">
              <el-input
                v-model="registerForm.username"
                placeholder="3-32 位字母、数字或 _. -"
                :prefix-icon="User"
                autocomplete="username"
              />
            </el-form-item>
            <el-form-item prop="user_name" label="显示名（可选）">
              <el-input
                v-model="registerForm.user_name"
                placeholder="缺省与用户名一致"
                :prefix-icon="Postcard"
              />
            </el-form-item>
            <el-form-item prop="password" label="密码">
              <el-input
                v-model="registerForm.password"
                type="password"
                placeholder="至少 6 位"
                :prefix-icon="Lock"
                show-password
                autocomplete="new-password"
              />
            </el-form-item>
            <el-form-item prop="confirmPassword" label="确认密码">
              <el-input
                v-model="registerForm.confirmPassword"
                type="password"
                placeholder="再次输入密码"
                :prefix-icon="Lock"
                show-password
                autocomplete="new-password"
              />
            </el-form-item>
            <el-button
              type="primary"
              class="submit-btn"
              :loading="loading"
              @click="handleRegister"
            >
              注 册
            </el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, inject, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock, Monitor, Postcard } from '@element-plus/icons-vue'
import { authApi, setToken } from '@/api/index.js'

const route = useRoute()
const router = useRouter()

// App 根组件提供的共享用户信息：登录/注册成功后立即更新，
// 否则客户端路由跳转不重挂载 App，右上角用户名要等整页刷新才出现
const currentUser = inject('currentUser', null)

const activeTab = ref('login')
const loading = ref(false)
const loginFormRef = ref(null)
const registerFormRef = ref(null)

// M4 QE4-1/QE4-2：登录方式开关（后端 /auth/methods 公告）——
// 密码表单在 local 或 ldap 任一启用时显示（ldap 是账密表单的目录旁路），
// local 关闭时只显 SSO 按钮 + （若启用 ldap）目录账密表单
const authMethods = ref({ local: true, oidc: false, oidcConfigured: false })
const passwordFormVisible = ref(true)
const ldapOnly = ref(false)
const registerEnabled = ref(true)

const loginForm = reactive({ username: '', password: '' })
const registerForm = reactive({ username: '', user_name: '', password: '', confirmPassword: '' })

const loginRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

const registerRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { pattern: /^[A-Za-z0-9_.-]{3,32}$/, message: '3-32 位字母、数字或 _. -', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' }
  ],
  confirmPassword: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (rule, value, callback) => {
        if (value !== registerForm.password) {
          callback(new Error('两次输入的密码不一致'))
        } else {
          callback()
        }
      },
      trigger: 'blur'
    }
  ]
}

const gotoRedirect = () => {
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
  router.push(redirect.startsWith('/') ? redirect : '/')
}

const handleLogin = async () => {
  const valid = await loginFormRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    const result = await authApi.login({
      username: loginForm.username,
      password: loginForm.password
    })
    setToken(result.token)
    if (currentUser) {
      currentUser.value = result.user
    }
    ElMessage.success('登录成功')
    gotoRedirect()
  } catch (e) {
    ElMessage.error(e?.message || '登录失败')
  } finally {
    loading.value = false
  }
}

const handleRegister = async () => {
  const valid = await registerFormRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    const result = await authApi.register({
      username: registerForm.username,
      password: registerForm.password,
      user_name: registerForm.user_name || undefined
    })
    setToken(result.token)
    if (currentUser) {
      currentUser.value = result.user
    }
    const isFirst = result.user?.super_admin_flag === 1
    ElMessage.success(isFirst ? '注册成功，已自动授予管理员权限' : '注册成功')
    gotoRedirect()
  } catch (e) {
    ElMessage.error(e?.message || '注册失败')
  } finally {
    loading.value = false
  }
}

// M4 QE4-1：发起 SSO 登录——302 到企业 IdP，回跳由 /login?sso_token= 接管
const startSsoLogin = () => {
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
  window.location.href = `/api/v1/auth/oidc/login?redirect=${encodeURIComponent(redirect)}`
}

// SSO 回跳落袋：/login?sso_token=<JWT>（后端 callback 302 而来）
const handleSsoCallback = async () => {
  const token = typeof route.query.sso_token === 'string' ? route.query.sso_token : ''
  if (!token) return false
  setToken(token)
  try {
    const status = await authApi.status()
    if (currentUser && status.user) {
      currentUser.value = status.user
    }
    ElMessage.success('SSO 登录成功')
  } catch {
    // token 无效极罕见（签发即回跳）；按失败处理并留在登录页
    ElMessage.error('SSO 登录态无效，请重试')
    return false
  }
  gotoRedirect()
  return true
}

// 已登录用户访问登录页直接跳转；SSO 回跳/失败提示优先处理
onMounted(async () => {
  // SSO 失败回跳：/login?sso_error=<原因>（URL 中文已解码）
  if (typeof route.query.sso_error === 'string' && route.query.sso_error) {
    ElMessage.error(route.query.sso_error)
  }
  if (await handleSsoCallback()) return

  try {
    const methods = await authApi.methods()
    const list = methods.methods || []
    authMethods.value = {
      local: list.includes('local'),
      oidc: list.includes('oidc'),
      oidcConfigured: Boolean(methods.oidc_configured)
    }
    passwordFormVisible.value = list.includes('local') || list.includes('ldap')
    ldapOnly.value = !list.includes('local') && list.includes('ldap')
    registerEnabled.value = Boolean(methods.register_enabled)
    if (!authMethods.value.local) activeTab.value = 'login'
  } catch {
    // 公告接口失败按缺省（local）渲染，本地账密仍可登录
  }

  const { getToken, authApi: api } = await import('@/api/index.js')
  if (getToken()) {
    try {
      const status = await api.status()
      if (status.is_authenticated) {
        gotoRedirect()
      }
    } catch {
      // token 无效，停留在登录页
    }
  }
})
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #EEF2FF 0%, #F7F8FA 60%, #E5EDFF 100%);
  padding: 24px;
}

.login-card {
  width: 400px;
  background: #fff;
  border: 1px solid var(--border-color, #E5E7EB);
  border-radius: 16px;
  padding: 36px 36px 28px;
  box-shadow: 0 8px 30px rgba(54, 98, 236, 0.08);
}

.login-header {
  text-align: center;
  margin-bottom: 20px;
}

.login-logo {
  width: 52px;
  height: 52px;
  margin: 0 auto 12px;
  background: linear-gradient(135deg, #3662EC 0%, #5B8DEF 100%);
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
}

.login-title {
  font-size: 22px;
  font-weight: 600;
  color: var(--text-primary, #1F2937);
  margin: 0;
}

.login-subtitle {
  font-size: 13px;
  color: var(--text-secondary, #6B7280);
  margin-top: 4px;
}

.login-tabs :deep(.el-tabs__nav) {
  width: 100%;
}

.submit-btn {
  width: 100%;
  margin-top: 4px;
  border-radius: 8px;
  font-weight: 500;
  letter-spacing: 4px;
}

/* M4 QE4-1：SSO 入口在本地表单上方，二者共存时加分隔线 */
.sso-btn {
  letter-spacing: 1px;
}

/* M4 QE4-2：纯 LDAP 部署（local 关闭）时账密表单的目录提示 */
.ldap-hint {
  font-size: 12px;
  color: var(--text-secondary, #6B7280);
  margin: -4px 0 8px;
}

.sso-divider :deep(.el-divider__text) {
  font-size: 12px;
  color: var(--text-secondary, #6B7280);
}
</style>
