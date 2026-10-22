import { createRouter, createWebHistory } from 'vue-router'
import { authApi, getToken } from '@/api'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: { title: '登录' }
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
    meta: { title: '开发者设置' }
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
  // 登录页、根路径和文档路径不做登录检查
  if (to.path === '/login' || to.path === '/' || to.path === '/docs') {
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
      const isSuperAdmin = user?.superAdminFlag === 1 || user?.super_admin_flag === 1
      if (!isSuperAdmin) {
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
