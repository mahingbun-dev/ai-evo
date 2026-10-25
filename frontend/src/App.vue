<template>
  <div id="app">
    <!-- 登录页独立布局（无侧边栏/顶栏） -->
    <template v-if="isLoginPage">
      <router-view />
    </template>
    <el-container v-else class="layout-container">
      <!-- 左侧导航栏 -->
      <el-aside :width="sidebarWidth">
        <div class="logo" :class="{ 'logo-collapsed': isCollapse }">
          <div class="logo-icon">
            <el-icon :size="24"><Monitor /></el-icon>
          </div>
          <span class="logo-text" v-show="!isCollapse">AgentEvo</span>
          <el-button
            class="collapse-btn"
            :icon="isCollapse ? Expand : Fold"
            text
            @click="toggleSidebar"
            :title="isCollapse ? '展开侧边栏' : '收起侧边栏'"
          />
        </div>
        <el-menu
          :default-active="route.path"
          router
          class="menu"
          :collapse="isCollapse"
        >
          <div class="menu-group-title" v-show="!isCollapse">概览</div>
          <el-menu-item index="/">
            <el-icon><HomeFilled /></el-icon>
            <span>首页</span>
          </el-menu-item>

          <div class="menu-group-title" v-show="!isCollapse">评测准备</div>
          <el-menu-item index="/models">
            <el-icon><Cpu /></el-icon>
            <span>模型配置</span>
          </el-menu-item>
          <el-menu-item index="/scripts">
            <el-icon><Files /></el-icon>
            <span>脚本库</span>
          </el-menu-item>
          <el-menu-item index="/datasets">
            <el-icon><FolderOpened /></el-icon>
            <span>数据集管理</span>
          </el-menu-item>
          <el-menu-item index="/tasks">
            <el-icon><List /></el-icon>
            <span>评测任务</span>
          </el-menu-item>
          <el-menu-item index="/templates">
            <el-icon><Document /></el-icon>
            <span>评测模板</span>
          </el-menu-item>

          <div class="menu-group-title" v-show="!isCollapse">结果与分析</div>
          <el-menu-item index="/results">
            <el-icon><DataAnalysis /></el-icon>
            <span>结果展示</span>
          </el-menu-item>
          <el-menu-item index="/simulation">
            <el-icon><VideoPlay /></el-icon>
            <span>仿真评测</span>
          </el-menu-item>
          <el-menu-item index="/ab-test">
            <el-icon><Switch /></el-icon>
            <span>A/B 对比测试</span>
          </el-menu-item>
          <el-menu-item index="/statistics" v-if="isSuperAdmin">
            <el-icon><TrendCharts /></el-icon>
            <span>统计分析</span>
          </el-menu-item>

          <div class="menu-group-title" v-show="!isCollapse">系统与治理</div>
          <el-menu-item index="/dev-settings">
            <el-icon><Key /></el-icon>
            <span>开发者设置</span>
          </el-menu-item>
          <el-menu-item index="/teams" v-if="isSuperAdmin">
            <el-icon><Share /></el-icon>
            <span>团队管理</span>
          </el-menu-item>
          <el-menu-item index="/audit-logs" v-if="isSuperAdmin">
            <el-icon><Clock /></el-icon>
            <span>审计日志</span>
          </el-menu-item>
          <el-menu-item index="/judge-pool" v-if="isSuperAdmin">
            <el-icon><Odometer /></el-icon>
            <span>并发池</span>
          </el-menu-item>

          <div class="menu-group-title" v-show="!isCollapse">帮助</div>
          <el-menu-item index="/docs">
            <el-icon><QuestionFilled /></el-icon>
            <span>使用文档</span>
          </el-menu-item>
          <el-menu-item index="/feedback" @click="openFeedback">
            <el-icon><ChatDotRound /></el-icon>
            <span>问题反馈与迭代计划</span>
          </el-menu-item>
        </el-menu>
      </el-aside>

      <!-- 右侧内容区 -->
      <el-container class="main-container">
        <!-- 顶部导航 -->
        <el-header>
          <div class="header-left">
            <h1 class="page-title">{{ route.meta.title }}</h1>
          </div>
          <div class="header-right">
            <el-button type="primary" :icon="Plus" @click="handleCreate">创建评测任务</el-button>
            <el-dropdown @command="handleUserCommand" trigger="click">
              <div class="user-info">
                <el-avatar :size="32" :icon="User" />
                <span class="user-name" v-if="currentUser && !isCollapse">{{ currentUser.user_name || currentUser.userName || currentUser.username || '' }}</span>
              </div>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item disabled v-if="currentUser">
                    {{ currentUser.username || currentUser.user_code || '' }}
                  </el-dropdown-item>
                  <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </el-header>

        <!-- 主内容区 -->
        <el-main>
          <router-view />
        </el-main>
      </el-container>
    </el-container>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, provide } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Plus, User, Monitor, Cpu, HomeFilled, FolderOpened, List, Document, DataAnalysis, Fold, Expand, Switch, QuestionFilled, Clock, Share, ChatDotRound, TrendCharts, Files, Key, VideoPlay, Odometer } from '@element-plus/icons-vue'
import { authApi, getToken, clearToken } from '@/api/index.js'

const route = useRoute()
const router = useRouter()

// 登录页使用独立布局
const isLoginPage = computed(() => route.path === '/login')

// 用户信息
const currentUser = ref(null)
const isSuperAdmin = computed(() => currentUser.value?.superAdminFlag === 1 || currentUser.value?.super_admin_flag === 1)

// M6 QE6-2：license 授权信息（/auth/status 附带 tier+功能位表，企业功能锁标渲染依据）
const licenseInfo = ref(null)

// 向子组件提供用户信息
provide('currentUser', currentUser)
provide('isSuperAdmin', isSuperAdmin)
provide('licenseInfo', licenseInfo)

// 获取当前用户信息
const fetchCurrentUser = async () => {
  try {
    const result = await authApi.status()
    if (result.is_authenticated && result.user) {
      currentUser.value = result.user
      licenseInfo.value = result.license || null
    }
  } catch (e) {
    console.warn('获取用户信息失败:', e)
  }
}

// 应用启动时获取当前登录用户（token 失效由 401 拦截器统一跳转登录页）
onMounted(async () => {
  if (getToken()) {
    await fetchCurrentUser()
  }
})

// 侧边栏收起状态
const isCollapse = ref(false)

// 计算侧边栏宽度
const sidebarWidth = computed(() => isCollapse.value ? '64px' : '240px')

// 切换侧边栏收起/展开
const toggleSidebar = () => {
  isCollapse.value = !isCollapse.value
}

const handleCreate = () => {
  router.push('/tasks')
}

const openFeedback = () => {
  window.open('https://github.com/mahingbun-dev/ai-evo/issues', '_blank')
}

const handleUserCommand = (command) => {
  if (command === 'logout') {
    // 本地登录：通知后端（可选）并清除本地 token，回到登录页
    authApi.logout().catch(() => {})
    clearToken()
    currentUser.value = null
    router.push('/login')
  }
}
</script>

<style>
/* 应用外壳样式：墨蓝侧边栏 + 靛蓝主色的工程控制台风格。
   设计令牌与 Element Plus 全局覆盖统一在 src/styles/theme.css，这里只管外壳布局。 */

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

#app {
  height: 100vh;
  font-family: var(--el-font-family);
}

.layout-container {
  height: 100%;
}

/* ---------- 侧边栏（深墨蓝） ---------- */
.el-aside {
  background: linear-gradient(180deg, var(--aievo-ink-900) 0%, var(--aievo-ink-850) 100%);
  border-right: 1px solid var(--aievo-ink-800);
  display: flex;
  flex-direction: column;
}

.logo {
  height: 64px;
  display: flex;
  align-items: center;
  padding: 0 16px;
  gap: 12px;
  border-bottom: 1px solid var(--aievo-ink-800);
  transition: all 0.3s ease;
  flex-shrink: 0;
}

.logo-collapsed {
  padding: 0 12px;
  justify-content: center;
}

.logo-icon {
  width: 36px;
  height: 36px;
  background: var(--aievo-brand-gradient);
  border-radius: var(--aievo-radius-m);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.08) inset, 0 2px 10px rgba(79, 70, 229, 0.45);
  flex-shrink: 0;
}

.logo-text {
  font-size: 17px;
  font-weight: 700;
  letter-spacing: 0.01em;
  color: #F8FAFC;
  flex: 1;
  white-space: nowrap;
}

.collapse-btn {
  margin-left: auto;
  padding: 4px;
  color: #7C8AA5;
}

.collapse-btn:hover {
  background-color: rgba(148, 163, 184, 0.12);
  color: #E2E8F0;
}

/* ---------- 菜单 ---------- */
.menu {
  border-right: none;
  background-color: transparent;
  padding: 10px 12px 20px;
  overflow-y: auto;
  flex: 1;
  --el-menu-bg-color: transparent;
  --el-menu-text-color: #94A3B8;
  --el-menu-hover-bg-color: rgba(148, 163, 184, 0.10);
  --el-menu-active-color: #C7D2FE;
}

.menu-group-title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  color: #5B6B8C;
  padding: 14px 10px 6px;
  white-space: nowrap;
}

.menu-group-title:first-child {
  padding-top: 6px;
}

.menu .el-menu-item {
  height: 40px;
  line-height: 40px;
  border-radius: var(--aievo-radius-m);
  margin-bottom: 2px;
  color: #94A3B8;
  font-size: 14px;
  transition: background-color 0.18s ease, color 0.18s ease;
}

.menu .el-menu-item:hover {
  background-color: rgba(148, 163, 184, 0.10);
  color: #E2E8F0;
}

.menu .el-menu-item.is-active {
  background-color: rgba(99, 102, 241, 0.20);
  color: #FFFFFF;
  font-weight: 500;
  position: relative;
}

.menu .el-menu-item.is-active::before {
  content: '';
  position: absolute;
  left: -12px;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--aievo-brand-gradient);
}

.menu .el-menu-item .el-icon {
  margin-right: 12px;
  font-size: 17px;
}

/* 收起状态下的菜单样式 */
.menu.el-menu--collapse .el-menu-item {
  justify-content: center;
  padding: 0 12px;
}

.menu.el-menu--collapse .el-menu-item .el-icon {
  margin-right: 0;
}

.menu.el-menu--collapse .el-menu-item.is-active::before {
  left: 4px;
}

/* ---------- 顶部导航 ---------- */
.main-container {
  background-color: var(--aievo-bg);
  min-height: 100vh;
}

.el-header {
  background-color: var(--aievo-surface);
  border-bottom: 1px solid var(--aievo-line-soft);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 60px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.page-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--aievo-text-1);
  margin: 0;
  letter-spacing: 0.01em;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.user-info {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  border-radius: var(--aievo-radius-m);
  transition: background-color 0.18s ease;
}

.user-info:hover {
  background-color: var(--aievo-primary-soft);
}

.user-info .el-avatar {
  background: var(--aievo-brand-gradient);
  color: #fff;
}

.user-name {
  font-size: 14px;
  color: var(--aievo-text-1);
  white-space: nowrap;
}

/* ---------- 主内容区 ---------- */
.el-main {
  background-color: var(--aievo-bg);
  padding: 24px;
}
</style>