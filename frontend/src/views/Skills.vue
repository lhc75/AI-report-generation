<template>
  <div>
    <div class="toolbar">
      <h3 class="page-title">技能库</h3>
      <div class="toolbar-actions">
        <el-input v-model="search" clearable placeholder="搜索技能" :prefix-icon="Search" />
        <el-button :icon="Upload" @click="importVisible = true">导入</el-button>
        <el-button type="primary" :icon="Plus" @click="openEdit()">新建技能</el-button>
      </div>
    </div>

    <el-table :data="filteredSkills" v-loading="loading" border>
      <el-table-column prop="name" label="名称" min-width="160" />
      <el-table-column prop="description" label="描述" min-width="300" show-overflow-tooltip />
      <el-table-column label="来源" width="120">
        <template #default="{ row }">{{ sourceText(row.source_type) }}</template>
      </el-table-column>
      <el-table-column label="已挂载" width="90" align="center">
        <template #default="{ row }">{{ row.usage_count }}</template>
      </el-table-column>
      <el-table-column prop="updated_at" label="更新时间" width="170" />
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>暂无 Skill</template>
    </el-table>

    <el-dialog v-model="editVisible" :title="form.id ? '编辑技能' : '新建技能'" width="min(720px, calc(100vw - 32px))" class="skill-dialog">
      <el-form label-position="top">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" maxlength="128" show-word-limit />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" maxlength="512" show-word-limit />
        </el-form-item>
        <el-form-item label="内容" required>
          <el-input v-model="form.content" type="textarea" :autosize="{ minRows: 12, maxRows: 22 }" maxlength="50000" show-word-limit />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="importVisible" title="导入 Skills" width="min(760px, calc(100vw - 32px))" class="skill-dialog">
      <el-upload
        drag
        :auto-upload="false"
        :show-file-list="false"
        accept=".md,.json"
        :on-change="previewImport"
      >
        <el-icon class="upload-icon"><Upload /></el-icon>
        <div class="el-upload__text">选择或拖入 SKILL.md、Markdown 或 JSON 文件</div>
      </el-upload>

      <template v-if="previewItems.length">
        <div class="import-head">
          <span>导入预览</span>
          <el-radio-group v-model="conflictStrategy" size="small">
            <el-radio-button label="skip">跳过同名项</el-radio-button>
            <el-radio-button label="replace">覆盖同名项</el-radio-button>
          </el-radio-group>
        </div>
        <el-table :data="previewItems" size="small" max-height="320">
          <el-table-column prop="name" label="名称" min-width="150" />
          <el-table-column prop="description" label="描述" min-width="240" show-overflow-tooltip />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.status === 'create'" type="success" size="small">新建</el-tag>
              <el-tag v-else-if="row.status === 'conflict'" type="warning" size="small">同名</el-tag>
              <el-tooltip v-else :content="row.error">
                <el-tag type="danger" size="small">错误</el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
        </el-table>
      </template>
      <template #footer>
        <el-button @click="importVisible = false">取消</el-button>
        <el-button type="primary" :disabled="!validPreviewItems.length" :loading="importing" @click="commitImport">确认导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Upload } from '@element-plus/icons-vue'
import api from '../api'

const skills = ref([])
const search = ref('')
const loading = ref(false)
const saving = ref(false)
const importing = ref(false)
const editVisible = ref(false)
const importVisible = ref(false)
const previewItems = ref([])
const conflictStrategy = ref('skip')
const form = ref(emptyForm())

const filteredSkills = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return skills.value
  return skills.value.filter((skill) =>
    `${skill.name} ${skill.description} ${skill.content}`.toLowerCase().includes(query)
  )
})
const validPreviewItems = computed(() => previewItems.value.filter((item) => item.status !== 'error'))

function emptyForm() {
  return { id: null, name: '', description: '', content: '' }
}

function sourceText(source) {
  return {
    manual: '手动创建',
    seeded: '系统预置',
    imported_skill_md: 'SKILL.md',
    imported_json: 'JSON 导入',
  }[source] || source
}

async function load() {
  loading.value = true
  try {
    skills.value = (await api.get('/skills')).data
  } finally {
    loading.value = false
  }
}

function openEdit(skill = null) {
  form.value = skill
    ? { id: skill.id, name: skill.name, description: skill.description, content: skill.content }
    : emptyForm()
  editVisible.value = true
}

async function save() {
  const payload = { name: form.value.name.trim(), description: form.value.description.trim(), content: form.value.content.trim() }
  if (!payload.name || !payload.content) {
    ElMessage.warning('请填写技能名称和内容')
    return
  }
  saving.value = true
  try {
    if (form.value.id) await api.put(`/skills/${form.value.id}`, payload)
    else await api.post('/skills', payload)
    ElMessage.success('已保存')
    editVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function remove(skill) {
  await ElMessageBox.confirm(
    skill.usage_count
      ? `删除「${skill.name}」后，将从 ${skill.usage_count} 个模型配置中解除挂载。`
      : `确定删除「${skill.name}」？`,
    '删除技能',
    { type: 'warning' },
  )
  await api.delete(`/skills/${skill.id}`)
  ElMessage.success('已删除')
  await load()
}

async function previewImport(uploadFile) {
  const raw = uploadFile.raw
  if (!raw) return
  importing.value = true
  try {
    const data = new FormData()
    data.append('file', raw, raw.name)
    const response = await api.post('/skills/import/preview', data)
    previewItems.value = response.data.items
  } finally {
    importing.value = false
  }
}

async function commitImport() {
  importing.value = true
  try {
    const { data } = await api.post('/skills/import', {
      skills: validPreviewItems.value.map(({ name, description, content, source_type }) => ({
        name, description, content, source_type,
      })),
      conflict_strategy: conflictStrategy.value,
    })
    ElMessage.success(`导入完成：新建 ${data.created}，更新 ${data.updated}，跳过 ${data.skipped}`)
    importVisible.value = false
    previewItems.value = []
    await load()
  } finally {
    importing.value = false
  }
}

load()
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 16px; }
.page-title { margin: 0; font-size: 17px; }
.toolbar-actions { display: flex; align-items: center; gap: 8px; }
.toolbar-actions :deep(.el-input) { width: 220px; }
.upload-icon { margin-bottom: 8px; font-size: 42px; color: #409eff; }
.import-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin: 18px 0 10px; }
:deep(.skill-dialog .el-dialog__body) { max-height: calc(100vh - 190px); overflow-y: auto; }
@media (max-width: 640px) {
  .toolbar { align-items: stretch; flex-direction: column; }
  .toolbar-actions { flex-wrap: wrap; }
  .toolbar-actions :deep(.el-input) { width: 100%; }
  .import-head { align-items: flex-start; flex-direction: column; }
}
</style>
