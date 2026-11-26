<template>
  <div>
    <div class="toolbar">
      <h3 class="page-title">我的项目</h3>
      <el-button type="primary" :icon="Plus" @click="openCreate">新建项目</el-button>
    </div>
    <el-table :data="projects" v-loading="loading" border>
      <el-table-column prop="name" label="项目名称" min-width="200" />
      <el-table-column label="报告模式" width="148">
        <template #default="{ row }">{{ modes[row.mode] }}</template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ row.mode === 'survey' ? (row.dataset_id ? '已导入' : '待导入') : statusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="170" />
      <el-table-column label="操作" width="160">
        <template #default="{ row }">
          <el-button type="primary" link @click="openProject(row)">进入</el-button>
          <el-button type="danger" link :loading="deletingKey === row.mode + ':' + row.id" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>暂无项目，点击右上角新建</template>
    </el-table>

    <el-dialog v-model="dialogVisible" title="新建项目" width="min(485px, 96vw)">
      <el-form label-width="85px" @submit.prevent="create">
        <el-form-item label="报告模式">
          <el-radio-group v-model="mode">
            <el-radio-button value="general">通用 AI 报告</el-radio-button>
            <el-radio-button value="survey">批量问卷报告</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="项目名称"><el-input v-model="name" placeholder="请输入项目名称" maxlength="100" /></el-form-item>
        <template v-if="mode === 'survey'">
          <el-form-item label="省份"><el-input v-model="province" maxlength="100" /></el-form-item>
          <el-form-item label="年度"><el-input-number v-model="year" :min="1900" :max="2200" /></el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" :disabled="!name.trim() || (mode === 'survey' && !province.trim())" @click="create">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const router = useRouter()
const projects = ref([])
const loading = ref(false)
const dialogVisible = ref(false)
const name = ref('')
const mode = ref('general')
const province = ref('')
const year = ref(new Date().getFullYear())
const modes = { general: '通用 AI 报告', survey: '批量问卷报告' }
const creating = ref(false)
const deletingKey = ref(null)

function statusType(s) {
  return { analyzed: 'success', analyzing: 'warning', generating: 'warning', assembling: 'warning', failed: 'danger' }[s] || 'info'
}
function statusText(s) {
  return { created: '待分析', analyzing: '分析中', generating: '生成中', assembling: '导出中', analyzed: '已分析', failed: '分析失败' }[s] || s
}

async function load() {
  loading.value = true
  try {
    const results = await Promise.allSettled([api.get('/projects'), api.get('/surveys')])
    projects.value = results.flatMap((result, index) => result.status === 'fulfilled'
      ? result.value.data.map(row => ({ ...row, mode: index ? 'survey' : 'general' }))
      : projects.value.filter(row => row.mode === (index ? 'survey' : 'general')))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  name.value = ''
  province.value = ''
  mode.value = 'general'
  dialogVisible.value = true
}

function openProject(row) {
  return router.push((row.mode === 'survey' ? '/surveys/' : '/projects/') + row.id)
}

async function create() {
  if (!name.value.trim() || creating.value || (mode.value === 'survey' && !province.value.trim())) return
  creating.value = true
  const kind = mode.value
  try {
    const fields = { name: name.value.trim() }
    if (kind === 'survey') Object.assign(fields, { province: province.value.trim(), year: year.value })
    const { data } = await api.post(kind === 'survey' ? '/surveys' : '/projects', fields)
    dialogVisible.value = false
    openProject({ id: data.id, mode: kind })
  } catch {
    // The API interceptor reports validation and server errors.
  } finally {
    creating.value = false
  }
}

async function remove(row) {
  const survey = row.mode === 'survey'
  const contents = survey ? '上传资料、导入版本和出报结果' : '上传的文件和解析结果'
  try {
    await ElMessageBox.confirm(
      `确定删除项目「${row.name}」？${contents}会一并删除，不可恢复。`, '删除项目',
      { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' },
    )
  } catch { return }
  deletingKey.value = row.mode + ':' + row.id
  try {
    await api.delete((survey ? '/surveys/' : '/projects/') + row.id)
    ElMessage.success('已删除')
    await load()
  } catch {
    // The shared API displays request errors.
  } finally {
    deletingKey.value = null
  }
}

onMounted(load)
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-title { margin: 0; font-size: 17px; }
</style>
