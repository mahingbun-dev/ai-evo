<template>
  <div class="teams-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>团队管理</span>
        </div>
      </template>

      <el-table :data="teams" style="width: 100%" v-loading="loading">
        <el-table-column prop="team_code" label="团队代码" min-width="120" />
        <el-table-column prop="team_name" label="团队名称" min-width="150" />
        <el-table-column prop="is_active" label="状态" min-width="80">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'">
              {{ row.is_active ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="180" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="viewMembers(row)">查看成员</el-button>
            <el-button link type="primary" @click="viewStats(row)">查看统计</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination-container">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="handleSizeChange"
          @current-change="handleCurrentChange"
        />
      </div>
    </el-card>

    <!-- 成员详情对话框 -->
    <el-dialog v-model="membersDialogVisible" title="团队成员" width="700px">
      <el-table :data="members" v-loading="membersLoading" max-height="400">
        <el-table-column prop="user_code" label="用户代码" width="120" />
        <el-table-column prop="user_name" label="姓名" width="120" />
        <el-table-column prop="email" label="邮箱" min-width="180" />
        <el-table-column prop="position_name" label="职位" width="120" />
        <el-table-column prop="is_active" label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'">
              {{ row.is_active ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-popconfirm
              :title="`确定${row.is_active ? '禁用' : '启用'}该成员？`"
              @confirm="toggleMemberStatus(row)"
            >
              <template #reference>
                <el-button link :type="row.is_active ? 'danger' : 'success'" size="small" :loading="togglingStatusId === row.user_code" :disabled="togglingStatusId !== null">
                  {{ row.is_active ? '禁用' : '启用' }}
                </el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="membersDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 团队统计对话框 -->
    <el-dialog v-model="statsDialogVisible" title="团队资源统计" width="500px">
      <el-descriptions :column="2" border v-if="currentTeam">
        <el-descriptions-item label="团队代码">{{ currentTeam.team_code }}</el-descriptions-item>
        <el-descriptions-item label="团队名称">{{ currentTeam.team_name }}</el-descriptions-item>
        <el-descriptions-item label="评测任务数">{{ stats.task_count }}</el-descriptions-item>
        <el-descriptions-item label="数据集数">{{ stats.dataset_count }}</el-descriptions-item>
        <el-descriptions-item label="模型配置数">{{ stats.model_count }}</el-descriptions-item>
        <el-descriptions-item label="团队成员数">{{ stats.member_count }}</el-descriptions-item>
      </el-descriptions>
      <template #footer>
        <el-button @click="statsDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { teamApi } from '@/api'
import { formatTime } from '@/utils/time'

const teams = ref([])
const loading = ref(false)
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)

// 成员对话框
const membersDialogVisible = ref(false)
const members = ref([])
const membersLoading = ref(false)
const selectedTeamCode = ref('')
const togglingStatusId = ref(null)

// 统计对话框
const statsDialogVisible = ref(false)
const currentTeam = ref(null)
const stats = ref({
  task_count: 0,
  dataset_count: 0,
  model_count: 0,
  member_count: 0
})

const loadTeams = async () => {
  loading.value = true
  try {
    const params = {
      skip: (currentPage.value - 1) * pageSize.value,
      limit: pageSize.value
    }
    const res = await teamApi.list(params)
    teams.value = res.items || []
    total.value = res.total || 0
  } catch (e) {
    ElMessage.error('加载团队列表失败')
  } finally {
    loading.value = false
  }
}

const handleSizeChange = (val) => {
  pageSize.value = val
  loadTeams()
}

const handleCurrentChange = (val) => {
  currentPage.value = val
  loadTeams()
}

const viewMembers = async (row) => {
  selectedTeamCode.value = row.team_code
  membersDialogVisible.value = true
  membersLoading.value = true
  try {
    const res = await teamApi.getMembers(row.team_code, { limit: 100 })
    members.value = res.items || []
  } catch (e) {
    ElMessage.error('加载成员列表失败')
  } finally {
    membersLoading.value = false
  }
}

const toggleMemberStatus = async (row) => {
  if (togglingStatusId.value !== null) return
  togglingStatusId.value = row.user_code
  try {
    const newStatus = !row.is_active
    await teamApi.setMemberStatus(selectedTeamCode.value, row.user_code, newStatus)
    row.is_active = newStatus
    ElMessage.success(`成员已${newStatus ? '启用' : '禁用'}`)
  } catch (e) {
    ElMessage.error(e.message || '操作失败')
  } finally {
    togglingStatusId.value = null
  }
}

const viewStats = (row) => {
  currentTeam.value = row
  // 暂时使用团队成员数作为统计，实际应从后端获取
  stats.value = {
    task_count: '-',
    dataset_count: '-',
    model_count: '-',
    member_count: '-'
  }
  statsDialogVisible.value = true
}

onMounted(() => {
  loadTeams()
})
</script>

<style scoped>
.teams-page {
  max-width: 1400px;
  margin: 0 auto;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-header span {
  font-size: 16px;
  font-weight: 600;
  color: #1F2937;
}

.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}

:deep(.el-table) {
  --el-table-border-color: #E5E7EB;
}

:deep(.el-table th) {
  background: #F9FAFB;
  color: #374151;
  font-weight: 500;
}

:deep(.el-dialog) {
  border-radius: 12px;
}
</style>
