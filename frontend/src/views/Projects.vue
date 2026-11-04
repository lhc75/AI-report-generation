<template>
  <div>
    <div class="toolbar">
      <h3 class="page-title">我的项目</h3>
      <el-button type="primary" :icon="Plus" @click="dialogVisible = true">新建项目</el-button>
    </div>
    <el-table :data="projects" v-loading="loading" border>
      <el-table-column prop="name" label="项目名称" min-width="200" />
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="170" />
      <el-table-column label="操作" width="160">
        <template #default="{ row }">
          <el-button type="primary" link @click="$router.push(`/projects/${row.id}`)">进入</el-button>
          <el-button type="danger" link :loading="deletingId === row.id" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>暂无项目，点击右上角新建</template>
    </el-table>

    <el-dialog v-model="dialogVisible" title="新建项目" width="420px">
      <el-input v-model="name" placeholder="请输入项目名称" maxlength="100" />
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="create">创建</el-button>
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
const creating = ref(false)
const deletingId = ref(null)

function statusType(s) {
  return { analyzed: 'success', analyzing: 'warning', generating: 'warning', assembling: 'warning', failed: 'danger' }[s] || 'info'
}
function statusText(s) {
  return { created: '待分析', analyzing: '分析中', generating: '生成中', assembling: '导出中', analyzed: '已分析', failed: '分析失败' }[s] || s
}

async function load() {
  loading.value = true
  try {
    projects.value = (await api.get('/projects')).data
  } finally {
    loading.value = false
  }
}

async function create() {
  if (!name.value.trim()) return
  creating.value = true
  try {
    const { data } = await api.post('/projects', { name: name.value.trim() })
    dialogVisible.value = false
    router.push(`/projects/${data.id}`)
  } finally {
    creating.value = false
  }
}

async function remove(row) {
  await ElMessageBox.confirm(
    `确定删除项目「${row.name}」？上传的文件和解析结果会一并删除，不可恢复。`,
    '删除项目',
    { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' }
  )
  deletingId.value = row.id
  try {
    await api.delete(`/projects/${row.id}`)
    ElMessage.success('已删除')
    await load()
  } finally {
    deletingId.value = null
  }
}

onMounted(load)
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-title { margin: 0; font-size: 17px; }
</style>
