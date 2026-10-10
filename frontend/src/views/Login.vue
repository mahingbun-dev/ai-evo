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

      <el-tabs v-model="activeTab" class="login-tabs" stretch>
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
        <el-tab-pane label="注册" name="register">
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

// 已登录用户访问登录页直接跳转
onMounted(async () => {
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
</style>
