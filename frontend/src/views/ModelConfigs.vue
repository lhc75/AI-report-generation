<template>
  <div>
    <div class="toolbar">
      <h3 class="page-title">AI 配置</h3>
      <el-button type="primary" :icon="Plus" @click="openEdit(null)">新增模型配置</el-button>
    </div>

    <el-table :data="configs" v-loading="loading" border>
      <el-table-column prop="name" label="名称" min-width="140" />
      <el-table-column label="作用范围" width="110">
        <template #default="{ row }">{{ agentText(row.agent_type) }}</template>
      </el-table-column>
      <el-table-column prop="model" label="模型" min-width="140" show-overflow-tooltip />
      <el-table-column prop="base_url" label="Base URL" min-width="200" show-overflow-tooltip />
      <el-table-column label="Skills" min-width="180">
        <template #default="{ row }">
          <el-tag v-for="skill in row.skills" :key="skill.id" size="small" style="margin-right:4px">{{ skill.name }}</el-tag>
          <span v-if="!row.skills?.length" class="muted">无</span>
        </template>
      </el-table-column>
      <el-table-column label="启用参数" min-width="180">
        <template #default="{ row }">
          <el-tag v-for="p in enabledParams(row)" :key="p" size="small" style="margin-right:4px">{{ p }}</el-tag>
          <span v-if="!enabledParams(row).length" class="muted">无</span>
        </template>
      </el-table-column>
      <el-table-column label="默认" width="70">
        <template #default="{ row }">
          <el-tag v-if="row.is_default" type="success" size="small">默认</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :loading="testingId === row.id" @click="test(row)">测试连接</el-button>
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>暂无模型配置，点击右上角新增</template>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="form.id ? '编辑模型配置' : '新增模型配置'"
      width="min(760px, calc(100vw - 32px))"
      top="4vh"
      class="model-config-dialog"
    >
      <el-form label-width="110px" label-position="left">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="例如：默认 GPT 配置" />
        </el-form-item>
        <el-form-item label="作用范围">
          <el-select v-model="form.agent_type" style="width: 100%">
            <el-option v-for="a in agentOptions" :key="a.value" :label="a.label" :value="a.value" />
          </el-select>
          <el-tag v-if="form.agent_type === 'assemble'" type="info" size="small" class="inactive-tag">未参与当前流程</el-tag>
        </el-form-item>
        <el-form-item label="Base URL" required>
          <el-input v-model="form.base_url" placeholder="例如：https://api.openai.com/v1" />
        </el-form-item>
        <el-form-item :label="'API Key'" :required="!form.id">
          <el-input v-model="form.api_key" type="password" show-password
            :placeholder="form.id ? '留空表示不修改' : '请输入 API Key'" />
        </el-form-item>
        <el-form-item label="模型名称" required>
          <el-input v-model="form.model" placeholder="例如：gpt-4o" />
        </el-form-item>
        <el-form-item label="采样参数">
          <div class="params">
            <div v-for="p in paramDefs" :key="p.name" class="param-row">
              <el-checkbox v-model="form.params[p.name].enabled" :label="p.name" />
              <el-input-number
                v-model="form.params[p.name].value"
                :step="p.step"
                :min="p.min"
                :max="p.max"
                :disabled="!form.params[p.name].enabled"
                controls-position="right"
                size="small"
              />
            </div>
          </div>
        </el-form-item>
        <el-form-item label="System Prompt">
          <el-input v-model="form.system_prompt" type="textarea" :autosize="{ minRows: 4, maxRows: 12 }" placeholder="可选" />
        </el-form-item>
        <el-form-item label="Skills">
          <div class="skill-picker">
            <el-select v-model="form.skill_ids" multiple filterable placeholder="选择已创建的 Skills" style="width: 100%">
              <el-option v-for="skill in skills" :key="skill.id" :label="skill.name" :value="skill.id">
                <div class="skill-option">
                  <span>{{ skill.name }}</span>
                  <span class="muted">{{ skill.description }}</span>
                </div>
              </el-option>
            </el-select>
            <el-button text type="primary" @click="goSkills">管理</el-button>
          </div>
        </el-form-item>
        <el-form-item label="设为默认">
          <el-switch v-model="form.is_default" />
          <span class="muted" style="margin-left:8px">同一作用范围内只保留一个默认配置</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import api from '../api'

const agentOptions = [
  { value: '', label: '全局默认' },
  { value: 'parse', label: '解析 Agent' },
  { value: 'analyze', label: '数据分析 Agent' },
  { value: 'write', label: '写作 Agent' },
  { value: 'chart', label: '图表 Agent' },
  { value: 'assemble', label: '组装 Agent' },
  { value: 'review', label: '审核 Agent' },
]
const paramDefs = [
  { name: 'temperature', step: 0.1, min: 0, max: 2 },
  { name: 'top_p', step: 0.05, min: 0, max: 1 },
  { name: 'top_k', step: 1, min: 1, max: 500 },
  { name: 'min_p', step: 0.01, min: 0, max: 1 },
  { name: 'presence_penalty', step: 0.1, min: -2, max: 2 },
  { name: 'repetition_penalty', step: 0.05, min: 0, max: 2 },
]

const configs = ref([])
const skills = ref([])
const loading = ref(false)
const dialogVisible = ref(false)
const saving = ref(false)
const testingId = ref(null)
const form = ref(emptyForm())
const router = useRouter()

function emptyParams() {
  const obj = {}
  for (const p of paramDefs) obj[p.name] = { enabled: false, value: null }
  return obj
}
function emptyForm() {
  return {
    id: null, name: '', agent_type: '', base_url: '', api_key: '', model: '',
    system_prompt: '', skill_ids: [], params: emptyParams(), is_default: false,
  }
}

function agentText(t) {
  return agentOptions.find((a) => a.value === t)?.label || t
}
function enabledParams(row) {
  return Object.entries(row.params || {})
    .filter(([, v]) => v && v.enabled)
    .map(([k]) => k)
}

async function load() {
  loading.value = true
  try {
    configs.value = (await api.get('/model-configs')).data
  } finally {
    loading.value = false
  }
}

async function loadSkills() {
  skills.value = (await api.get('/skills')).data
}

function openEdit(row) {
  if (row) {
    const params = emptyParams()
    for (const [k, v] of Object.entries(row.params || {})) {
      if (params[k]) params[k] = { enabled: !!v.enabled, value: v.value ?? null }
    }
    form.value = { ...row, api_key: '', skill_ids: [...(row.skill_ids || [])], params }
  } else {
    form.value = emptyForm()
  }
  dialogVisible.value = true
}

function goSkills() {
  dialogVisible.value = false
  router.push('/skills')
}

async function save() {
  const f = form.value
  if (!f.name || !f.base_url || !f.model || (!f.id && !f.api_key)) {
    ElMessage.warning('请填写名称、Base URL、模型名称和 API Key')
    return
  }
  saving.value = true
  try {
    if (f.id) {
      await api.put(`/model-configs/${f.id}`, f)
    } else {
      await api.post('/model-configs', f)
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function test(row) {
  testingId.value = row.id
  try {
    const { data } = await api.post(`/model-configs/${row.id}/test`)
    if (data.ok && data.warning) ElMessage.warning(`连接成功，但${data.warning}`)
    else if (data.ok) ElMessage.success(`连接成功：${data.reply}`)
    else ElMessage.error(`连接失败：${data.error}`)
  } finally {
    testingId.value = null
  }
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除配置「${row.name}」？`, '提示', { type: 'warning' })
  await api.delete(`/model-configs/${row.id}`)
  await load()
}

onMounted(async () => {
  await Promise.all([load(), loadSkills()])
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-title { margin: 0; font-size: 17px; }
.muted { color: #c0c4cc; font-size: 12px; }
.params { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 16px; width: 100%; }
.param-row { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 8px; }
.param-row :deep(.el-checkbox) { min-width: 0; overflow: hidden; }
.param-row :deep(.el-checkbox__label) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.param-row :deep(.el-input-number) { flex: 0 0 116px; width: 116px; }
.skill-picker { display: flex; align-items: flex-start; gap: 8px; width: 100%; }
.skill-option { display: flex; justify-content: space-between; gap: 16px; }
.inactive-tag { margin-top: 8px; }
:deep(.model-config-dialog .el-dialog__body) { max-height: calc(100vh - 190px); overflow-y: auto; }
@media (max-width: 700px) {
  .params { grid-template-columns: minmax(0, 1fr); }
  .skill-picker { align-items: stretch; flex-direction: column; }
}
</style>
