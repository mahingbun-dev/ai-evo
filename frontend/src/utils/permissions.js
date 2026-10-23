/**
 * 前端权限工具（M4 QE4-3 RBAC 三角色）
 *
 * 与后端 app/services/access_control.py 同口径：
 * - 角色 owner/engineer/viewer（用户对象缺 role 按 engineer 兜底）；
 * - 写操作=owner+engineer，只读=viewer；超管（super_admin_flag===1）全局放行；
 * - 项目级细粒度 RBAC 不做（留 M6 企业版功能位）——前端只分「能写/只读」两档。
 */

export const ROLE_OPTIONS = [
  { value: 'owner', label: 'Owner（管理成员角色）', tag: 'danger' },
  { value: 'engineer', label: 'Engineer（读写）', tag: 'primary' },
  { value: 'viewer', label: 'Viewer（只读）', tag: 'info' }
]

export const userRole = (user) => {
  const role = String(user?.role || '').toLowerCase()
  return ['owner', 'engineer', 'viewer'].includes(role) ? role : 'engineer'
}

export const isSuperAdmin = (user) => Number(user?.super_admin_flag ?? user?.superAdminFlag ?? 0) === 1

export const roleLabel = (user) => ROLE_OPTIONS.find(o => o.value === userRole(user))?.label.split('（')[0] || 'Engineer'

/**
 * 写权限（按钮级禁用联动入口）：超管或 owner/engineer 可写，viewer 只读。
 * user 为 App 根组件注入的 currentUser（含后端下发的 role 字段）。
 */
export const canWrite = (user) => isSuperAdmin(user) || ['owner', 'engineer'].includes(userRole(user))

/** 团队角色管理权限：超管或本团队 owner（页面侧简化为超管或 owner） */
export const canManageTeam = (user, teamCode) => {
  if (isSuperAdmin(user)) return true
  const userTeam = String(user?.org_code || '').trim() || 'DEFAULT'
  return userRole(user) === 'owner' && (userTeam === (String(teamCode || '').trim() || 'DEFAULT'))
}
