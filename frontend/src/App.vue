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
          :active-text-color="'#3662EC'"
          :collapse="isCollapse"
        >
          <el-menu-item index="/">
            <el-icon><HomeFilled /></el-icon>
            <span>首页</span>
          </el-menu-item>
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
          <el-menu-item index="/results">
            <el-icon><DataAnalysis /></el-icon>
            <span>结果展示</span>
          </el-menu-item>
          <el-menu-item index="/statistics" v-if="isSuperAdmin">
            <el-icon><TrendCharts /></el-icon>
            <span>统计分析</span>
          </el-menu-item>
          <el-menu-item index="/ab-test">
            <el-icon><Switch /></el-icon>
            <span>A/B 对比测试</span>
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
import { Plus, User, Monitor, Cpu, HomeFilled, FolderOpened, List, Document, DataAnalysis, Fold, Expand, Switch, QuestionFilled, Clock, Share, ChatDotRound, TrendCharts, Files, Odometer } from '@element-plus/icons-vue'
import { authApi, getToken, clearToken } from '@/api/index.js'

const route = useRoute()
const router = useRouter()

// 登录页使用独立布局
const isLoginPage = computed(() => route.path === '/login')

// 用户信息
const currentUser = ref(null)
const isSuperAdmin = computed(() => currentUser.value?.superAdminFlag === 1 || currentUser.value?.super_admin_flag === 1)

// 向子组件提供用户信息
provide('currentUser', currentUser)
provide('isSuperAdmin', isSuperAdmin)

// 获取当前用户信息
const fetchCurrentUser = async () => {
  try {
    const result = await authApi.status()
    if (result.is_authenticated && result.user) {
      currentUser.value = result.user
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
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

:root {
  --primary-color: #3662EC;
  --primary-light: #EEF2FF;
  --bg-color: #F7F8FA;
  --text-primary: #1F2937;
  --text-secondary: #6B7280;
  --border-color: #E5E7EB;
  --sidebar-bg: #FFFFFF;
  --header-bg: #FFFFFF;
}

#app {
  height: 100vh;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Helvetica Neue', sans-serif;
}

.layout-container {
  height: 100%;
}

/* 侧边栏样式 */
.el-aside {
  background-color: var(--sidebar-bg);
  border-right: 1px solid var(--border-color);
}

.logo {
  height: 64px;
  display: flex;
  align-items: center;
  padding: 0 20px;
  gap: 12px;
  border-bottom: 1px solid var(--border-color);
  transition: all 0.3s ease;
}

.logo-collapsed {
  padding: 0 12px;
  justify-content: center;
}

.logo-icon {
  width: 36px;
  height: 36px;
  background: linear-gradient(135deg, #3662EC 0%, #5B8DEF 100%);
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
}

.logo-text {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
  flex: 1;
  white-space: nowrap;
}

.collapse-btn {
  margin-left: auto;
  padding: 4px;
}

.collapse-btn:hover {
  background-color: var(--primary-light);
}

.menu {
  border-right: none;
  background-color: transparent;
  padding: 12px;
}

.menu .el-menu-item {
  height: 44px;
  line-height: 44px;
  border-radius: 8px;
  margin-bottom: 4px;
  color: var(--text-secondary);
  font-size: 14px;
  transition: all 0.2s ease;
}

.menu .el-menu-item:hover {
  background-color: var(--primary-light);
  color: var(--primary-color);
}

.menu .el-menu-item.is-active {
  background-color: var(--primary-light);
  color: var(--primary-color);
  font-weight: 500;
}

.menu .el-menu-item .el-icon {
  margin-right: 12px;
  font-size: 18px;
}

/* 收起状态下的菜单样式 */
.menu.el-menu--collapse .el-menu-item {
  justify-content: center;
  padding: 0 12px;
}

.menu.el-menu--collapse .el-menu-item .el-icon {
  margin-right: 0;
}

/* 主内容区容器 */
.main-container {
  background-color: var(--bg-color);
  min-height: 100vh;
}

/* 顶部导航 */
.el-header {
  background-color: var(--header-bg);
  border-bottom: 1px solid var(--border-color);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 64px;
}

.header-left {
  display: flex;
  align-items: center;
}

.page-title {
  font-size: 20px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.header-right .el-button {
  border-radius: 8px;
  font-weight: 500;
}

.user-info {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
}

.user-info .el-avatar {
  background-color: var(--primary-light);
  color: var(--primary-color);
}

.user-name {
  font-size: 14px;
  color: var(--text-primary);
  white-space: nowrap;
}

/* 主内容区 */
.el-main {
  background-color: var(--bg-color);
  padding: 24px;
}

/* 全局卡片样式覆盖 */
.el-card {
  border: 1px solid var(--border-color);
  border-radius: 12px;
  box-shadow: none;
}

.el-card:hover {
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}
</style>
