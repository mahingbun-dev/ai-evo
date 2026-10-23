import { createRouter, createWebHistory } from 'vue-router'
import { authApi, getToken } from '@/api'
import { isSuperAdmin, userRole } from '@/utils/permissions'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: { title: '登录' }
  },
  {
    // M5 QE5-4：免登录只读报告页（token 即凭证；过期/吊销后端 404，页面提示失效）
    path: '/share/:token',
    name: 'ShareReport',
    component: () => import('@/views/ShareReport.vue'),
    meta: { title: '评测报告（分享）' }
  },
  {
    path: '/',
    name: 'Home',
    component: () => import('@/views/Home.vue'),
    meta: { title: '首页' }
  },
  {
    path: '/models',
    name: 'Models',
    component: () => import('@/views/Models.vue'),
    meta: { title: '模型配置' }
  },
  {
    path: '/scripts',
    name: 'scripts',
    component: () => import('@/views/Scripts.vue'),
    meta: { title: '脚本库' }
  },
  {
    path: '/datasets',
    name: 'Datasets',
    component: () => import('@/views/Datasets.vue'),
    meta: { title: '数据集管理' }
  },
  {
    path: '/tasks',
    name: 'Tasks',
    component: () => import('@/views/Tasks.vue'),
    meta: { title: '评测任务' }
  },
  {
    path: '/templates',
    name: 'Templates',
    component: () => import('@/views/Templates.vue'),
    meta: { title: '评测模板' }
  },
  {
    path: '/results',
    name: 'Results',
    component: () => import('@/views/Results.vue'),
    meta: { title: '结果展示' }
  },
  {
    // M7 QE7-5：仿真评测（第 9 种类型：场景构建器 + 运行列表 + 逐 trial 对话回放）
    path: '/simulation',
    name: 'Simulation',
    component: () => import('@/views/Simulation.vue'),
    meta: { title: '仿真评测' }
  },
  {
    path: '/statistics',
    name: 'Statistics',
    component: () => import('@/views/Statistics.vue'),
    meta: { title: '统计分析' }
  },
  {
    path: '/ab-test',
    name: 'ABTest',
    component: () => import('@/views/ABTest.vue'),
    meta: { title: 'A/B 对比测试' }
  },
  {
    path: '/dev-settings',
    name: 'DevSettings',
    component: () => import('@/views/DevSettings.vue'),
    // M4 QE4-3：页面主操作是创建 API Key/Webhook（写操作），viewer 只读禁入
    meta: { title: '开发者设置', requireWrite: true }
  },
  {
    path: '/teams',
    name: 'Teams',
    component: () => import('@/views/Teams.vue'),
    meta: { title: '团队管理', requireSuperAdmin: true }
  },
  {
    path: '/audit-logs',
    name: 'AuditLogs',
    component: () => import('@/views/AuditLogs.vue'),
    meta: { title: '审计日志', requireSuperAdmin: true }
  },
  {
    path: '/docs',
    name: 'UserDocs',
    component: () => import('@/views/UserDocs.vue'),
    meta: { title: '使用文档' }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// 全局路由守卫：在每次路由跳转前检查登录状态
router.beforeEach(async (to, from, next) => {
  // 登录页、根路径和文档路径不做登录检查（/share/:token 免登录只读报告同口径）
  if (to.path === '/login' || to.path === '/' || to.path === '/docs' || to.name === 'ShareReport') {
    return next()
  }

  try {
    // 本地无 token 直接跳登录页；有 token 再向后端确认有效性
    if (!getToken()) {
      return next({ path: '/login', query: { redirect: to.fullPath } })
    }

    const status = await authApi.status()

    if (!status.is_authenticated) {
      return next({ path: '/login', query: { redirect: to.fullPath } })
    }

    // 超管页面权限检查
    if (to.meta.requireSuperAdmin) {
      const user = status.user
      const isSuper = isSuperAdmin(user)
      if (!isSuper) {
        return next('/')
      }
    }

    // 角色守卫（M4 QE4-3）：requireWrite 页面主操作为写（如开发者设置的
    // Key/Webhook 创建），viewer 只读禁入；超管与 owner/engineer 放行。
    // 只读浏览页（任务/数据集/模型等）不挂本守卫——viewer 可看不可写，
    // 按钮级禁用联动在页面内（readonlyRole）。
    if (to.meta.requireWrite) {
      const user = status.user
      const canWritePage = isSuperAdmin(user) || ['owner', 'engineer'].includes(userRole(user))
      if (!canWritePage) {
        console.warn('路由守卫：当前角色（viewer）无权访问该页面', to.path)
        return next('/')
      }
    }
  } catch (e) {
    console.error('路由守卫检查登录状态失败:', e)
  }

  next()
})

// 捕获路由导航错误（防止 parentNode 为 null 的组件渲染错误）
router.onError((error) => {
  // 忽略 DOM 相关的导航错误
  if (error.message && error.message.includes('parentNode')) {
    console.warn('路由导航 DOM 错误（已忽略）:', error.message)
    return
  }
  console.error('路由导航错误:', error)
})

export default router
