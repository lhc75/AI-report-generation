<template>
  <div class="statistics-page" v-loading="sourcesLoading && !dataFiles.length">
    <div class="toolbar">
      <div>
        <h3 class="page-title">数据统计</h3>
        <p class="page-subtitle">学生作答情况可视化分析</p>
      </div>
      <el-button :icon="Refresh" :loading="sourcesLoading" @click="refresh">刷新</el-button>
    </div>

    <el-alert v-if="sourceError" type="error" :title="sourceError" show-icon class="page-alert">
      <el-button size="small" @click="loadSources">重新加载</el-button>
    </el-alert>

    <el-card v-if="dataFiles.length" class="selector-card" shadow="never">
      <div class="selector-grid builtin-selector-grid">
        <div class="selector-item selector-file">
          <label>数据文件</label>
          <el-select v-model="selectedFileId" filterable placeholder="选择问卷数据文件"
            @change="resetForSelectedFile">
            <el-option v-for="file in dataFiles" :key="file.id" :value="file.id"
              :label="fileOptionLabel(file)" />
          </el-select>
        </div>
        <div class="selector-tip">
          <el-icon><DataAnalysis /></el-icon>
          <span>统计内置学生作答数据中的已提交记录</span>
        </div>
      </div>
    </el-card>

    <el-empty v-else-if="!sourcesLoading" description="暂无可统计数据文件">
      <el-button type="primary" @click="loadSources">重新加载</el-button>
    </el-empty>

    <template v-if="selectedFileId">
      <el-alert v-if="pageError" type="error" :title="pageError" show-icon class="page-alert">
        <el-button size="small" @click="loadAll">重新加载</el-button>
      </el-alert>

      <template v-if="overview">
        <el-row :gutter="16" class="metric-row">
          <el-col v-for="metric in metricCards" :key="metric.key" :xs="12" :sm="12" :lg="6">
            <el-card class="metric-card" shadow="never">
              <div class="metric-icon" :class="metric.tone">
                <el-icon><component :is="metric.icon" /></el-icon>
              </div>
              <div>
                <div class="metric-label">{{ metric.label }}</div>
                <div class="metric-value">{{ metric.value }}</div>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-row :gutter="16">
          <el-col :xs="24" :lg="12">
            <el-card class="panel-card" shadow="never">
              <template #header>
                <div class="panel-head">
                  <span>各市提交人次</span>
                  <span class="panel-tip">按城市汇总</span>
                </div>
              </template>
              <DashboardBarChart :data="cityChartData" color="#409eff" :height="310" empty-text="暂无城市统计数据" />
            </el-card>
          </el-col>
          <el-col :xs="24" :lg="12">
            <el-card class="panel-card" shadow="never">
              <template #header>
                <div class="panel-head">
                  <span>各年级提交人次</span>
                  <span class="panel-tip">按年级自然顺序排列</span>
                </div>
              </template>
              <DashboardBarChart :data="gradeChartData" color="#67c23a" :height="310" empty-text="暂无年级统计数据" />
            </el-card>
          </el-col>
        </el-row>

        <el-card class="panel-card" shadow="never">
          <template #header>
            <div class="panel-head">
              <span>单校查询</span>
              <span class="panel-tip">支持学校名称关键字或学校账号精确查询</span>
            </div>
          </template>
          <div class="query-grid">
            <el-input v-model="schoolKeyword" clearable placeholder="按学校名称搜索，例如：第一中学" @keyup.enter="loadOverview" />
            <el-input v-model="schoolId" clearable placeholder="按学校账号（schoolId）精确查询" @keyup.enter="loadOverview" />
            <div class="query-actions">
              <el-button type="primary" :icon="Search" :loading="overviewLoading" @click="loadOverview">查询</el-button>
              <el-button @click="resetSchoolQuery">重置</el-button>
            </div>
          </div>

          <el-alert v-if="overview.school_query && !overview.school_query.found" type="warning"
            title="未找到匹配的学校记录" show-icon class="query-result-alert" />
          <template v-else-if="overview.school_query?.found">
            <div class="school-result">
              <div class="school-result-main">
                <div class="school-title-row">
                  <strong>{{ overview.school_query.school }}</strong>
                  <el-tag size="small" effect="plain">ID：{{ overview.school_query.schoolId }}</el-tag>
                </div>
                <div class="school-meta">{{ overview.school_query.city }} · {{ overview.school_query.area }}</div>
              </div>
              <div class="school-result-stats">
                <div>
                  <span>提交人次</span>
                  <strong>{{ formatNumber(overview.school_query.total_submissions) }}</strong>
                </div>
                <div>
                  <span>参与学生数</span>
                  <strong>{{ formatNumber(overview.school_query.student_count) }}</strong>
                </div>
              </div>
            </div>
            <DashboardBarChart :data="schoolQueryChartData" color="#e6a23c" :height="240" empty-text="该学校暂无年级统计数据" />
          </template>
          <div v-else class="query-placeholder">输入学校名称或学校账号后点击“查询”</div>
        </el-card>

        <el-card class="panel-card" shadow="never">
          <template #header>
            <div class="panel-head">
              <span>学校提交情况统计</span>
              <el-tag size="small" effect="plain">共 {{ formatNumber(schoolTableTotal) }} 条</el-tag>
            </div>
          </template>
          <div class="filter-grid">
            <el-input v-model="schoolFilters.city" clearable placeholder="城市" @keyup.enter="loadSchoolTable" />
            <el-input v-model="schoolFilters.area" clearable placeholder="区县" @keyup.enter="loadSchoolTable" />
            <el-input v-model="schoolFilters.school" clearable placeholder="学校关键字" @keyup.enter="loadSchoolTable" />
            <el-input v-model="schoolFilters.schoolId" clearable placeholder="学校账号" @keyup.enter="loadSchoolTable" />
            <el-input v-model="schoolFilters.grade" clearable placeholder="年级" @keyup.enter="loadSchoolTable" />
            <div class="range-inputs">
              <el-input v-model="schoolFilters.submitMin" type="number" placeholder="提交人次 ≥" @keyup.enter="loadSchoolTable" />
              <span>至</span>
              <el-input v-model="schoolFilters.submitMax" type="number" placeholder="提交人次 ≤" @keyup.enter="loadSchoolTable" />
            </div>
            <div class="filter-actions">
              <el-button type="primary" :icon="Search" :loading="schoolLoading" @click="loadSchoolTable">筛选</el-button>
              <el-button @click="resetSchoolFilters">重置</el-button>
            </div>
          </div>
          <el-table :data="schoolRows" v-loading="schoolLoading" border stripe class="dashboard-table" max-height="430">
            <el-table-column v-for="column in schoolTableColumns" :key="column" :prop="column"
              :label="columnLabel(column)" :min-width="column === 'school' ? 220 : 130" show-overflow-tooltip>
              <template #default="{ row }">{{ formatTableCell(column, row[column]) }}</template>
            </el-table-column>
            <template #empty>暂无符合条件的学校统计</template>
          </el-table>
        </el-card>

        <el-card class="panel-card detail-panel" shadow="never">
          <template #header>
            <div class="panel-head">
              <span>学生提交明细</span>
              <el-radio-group v-model="detailMode" size="small" @change="handleModeChange">
                <el-radio-button value="meta">元信息</el-radio-button>
                <el-radio-button value="full">完整题目明细</el-radio-button>
              </el-radio-group>
            </div>
          </template>

          <div class="detail-filter-head">
            <div class="detail-filter-title">明细筛选</div>
            <el-button size="small" :icon="Plus" @click="addDetailFilter">添加筛选条件</el-button>
          </div>
          <div v-if="detailFilterRows.length" class="detail-filter-list">
            <div v-for="row in detailFilterRows" :key="row.id" class="detail-filter-row">
              <el-select v-model="row.column" filterable placeholder="选择列" class="filter-column-select">
                <el-option v-for="column in detailColumns" :key="column" :value="column" :label="columnLabel(column)" />
              </el-select>
              <el-select v-model="row.kind" class="filter-kind-select">
                <el-option value="text" label="包含文本" />
                <el-option value="range" label="数值范围" />
                <el-option value="date" label="日期范围" />
              </el-select>
              <div class="filter-value">
                <el-input v-if="row.kind === 'text'" v-model="row.value" clearable placeholder="输入包含的字符" />
                <template v-else-if="row.kind === 'range'">
                  <el-input v-model="row.minimum" type="number" placeholder="最小值" />
                  <span class="range-separator">至</span>
                  <el-input v-model="row.maximum" type="number" placeholder="最大值" />
                </template>
                <el-date-picker v-else v-model="row.dateRange" type="daterange" value-format="YYYY-MM-DD"
                  start-placeholder="开始日期" end-placeholder="结束日期" class="date-range" />
              </div>
              <el-button :icon="Delete" circle plain type="danger" @click="removeDetailFilter(row.id)" />
            </div>
          </div>
          <div class="detail-filter-actions">
            <el-button type="primary" :icon="Search" :loading="detailLoading" @click="applyDetailFilters">查询</el-button>
            <el-button :disabled="!detailFilterRows.length" @click="resetDetailFilters">清空筛选</el-button>
            <span class="detail-tip">完整题目明细只在切换后加载；默认显示 {{ detailPageSize }} 条/页</span>
          </div>

          <el-table :data="detailRows" v-loading="detailLoading" border stripe class="dashboard-table detail-table" height="470">
            <el-table-column v-for="column in detailColumns" :key="column" :prop="column"
              :label="columnLabel(column)" :min-width="column.startsWith('question') ? 180 : 150"
              show-overflow-tooltip>
              <template #default="{ row }">{{ formatTableCell(column, row[column]) }}</template>
            </el-table-column>
            <template #empty>暂无符合条件的提交明细</template>
          </el-table>
          <div class="pagination-row">
            <span class="pagination-total">共 {{ formatNumber(detailTotal) }} 条</span>
            <el-pagination v-model:current-page="detailPage" v-model:page-size="detailPageSize"
              :page-sizes="[20, 50, 100, 200]" :total="detailTotal" background
              layout="sizes, prev, pager, next, jumper" @current-change="loadRecords"
              @size-change="handleDetailSizeChange" />
          </div>
        </el-card>
      </template>
    </template>

    <el-empty v-else-if="!sourcesLoading" description="请选择数据文件" />
  </div>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  Clock, DataAnalysis, Delete, Plus, Refresh, School, Search, User,
} from '@element-plus/icons-vue'
import api from '../api'
import DashboardBarChart from '../components/DashboardBarChart.vue'

const router = useRouter()
const projects = ref([])
const selectedProjectId = ref(null)
const selectedFileId = ref(null)
const sourcesLoading = ref(false)
const sourceError = ref('')
const pageError = ref('')

const overview = ref(null)
const overviewLoading = ref(false)
const schoolKeyword = ref('')
const schoolId = ref('')
const schoolRows = ref([])
const schoolTableTotal = ref(0)
const schoolLoading = ref(false)
const detailMode = ref('meta')
const detailRows = ref([])
const detailColumns = ref(['username', 'city', 'area', 'school', 'schoolId', 'grade', 'submit_count', 'update_time'])
const detailTotal = ref(0)
const detailPage = ref(1)
const detailPageSize = ref(50)
const detailLoading = ref(false)
const detailFilterRows = ref([])

let filterRowId = 0
let overviewSeq = 0
let schoolSeq = 0
let detailSeq = 0

const META_COLUMN_ORDER = [
  'username', 'city', 'area', 'school', 'schoolId', 'grade', 'submit_count', 'update_time',
]
const COLUMN_LABELS = {
  username: '用户名',
  city: '城市',
  area: '区县',
  school: '学校',
  schoolId: '学校账号',
  grade: '年级',
  submit_count: '提交人次',
  update_time: '最后提交时间',
}
const FILTER_KIND_LABELS = { text: '文本', range: '数值', date: '日期' }

const BUILTIN_PROJECT_ID = 'builtin-student'
const builtinProject = computed(() => projects.value.find((item) => item.id === BUILTIN_PROJECT_ID) || null)
const dataFiles = computed(() => builtinProject.value?.data_files || [])
const summary = computed(() => overview.value?.summary || {})
const cityChartData = computed(() => overview.value?.charts?.city || [])
const gradeChartData = computed(() => overview.value?.charts?.grade || [])
const schoolQueryChartData = computed(() => overview.value?.school_query?.grades || [])
const schoolTableColumns = computed(() => ['city', 'area', 'school', 'schoolId', 'grade', 'submit_count'])
const metricCards = computed(() => [
  {
    key: 'submissions', label: '提交总人次', value: formatNumber(summary.value.total_submissions),
    icon: DataAnalysis, tone: 'blue',
  },
  {
    key: 'students', label: '参与学生数', value: formatNumber(summary.value.student_count),
    icon: User, tone: 'green',
  },
  {
    key: 'schools', label: '参与学校数', value: formatNumber(summary.value.school_count),
    icon: School, tone: 'orange',
  },
  {
    key: 'lastUpdate', label: '最后提交时间', value: formatDateTime(summary.value.last_update),
    icon: Clock, tone: 'purple',
  },
])

function formatNumber(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toLocaleString('zh-CN') : '0'
}

function formatDateTime(value) {
  if (!value) return '未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '未知'
  const pad = (number) => String(number).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}


function displayFileName(fileName) {
  return String(fileName || '').replace(/_answers\.csv$/i, '')
}

function fileOptionLabel(file) {
  return displayFileName(file.file_name)
}

function columnLabel(column) {
  return COLUMN_LABELS[column] || column
}

function formatTableCell(column, value) {
  if (value === null || value === undefined || value === '') return '—'
  if (column === 'update_time') return formatDateTime(value)
  if (typeof value === 'number') return formatNumber(value)
  return String(value)
}

function errorMessage(error, fallback) {
  const detail = error?.response?.data?.detail
  if (Array.isArray(detail)) return detail.map((item) => item?.msg || item).filter(Boolean).join('；')
  if (typeof detail === 'string' && detail) return detail
  return error?.message || fallback
}

function ensureDefaultSelection() {
  if (!builtinProject.value || !builtinProject.value.data_files.length) {
    selectedProjectId.value = null
    selectedFileId.value = null
    return
  }
  selectedProjectId.value = builtinProject.value.id
  const currentFile = builtinProject.value.data_files.find((item) => item.id === selectedFileId.value)
  selectedFileId.value = currentFile?.id || builtinProject.value.data_files[0]?.id || null
}

async function loadSources({ resetDashboard = true } = {}) {
  sourcesLoading.value = true
  sourceError.value = ''
  try {
    const { data } = await api.get('/dashboard/sources')
    projects.value = data.projects || []
    ensureDefaultSelection()
    if (resetDashboard) {
      if (selectedFileId.value) resetForSelectedFile()
      else clearDashboardState()
    }
  } catch (error) {
    sourceError.value = errorMessage(error, '数据来源加载失败')
  } finally {
    sourcesLoading.value = false
  }
}

function clearDashboardState() {
  pageError.value = ''
  overview.value = null
  schoolRows.value = []
  schoolTableTotal.value = 0
  detailRows.value = []
  detailColumns.value = [...META_COLUMN_ORDER]
  detailTotal.value = 0
  detailPage.value = 1
  detailFilterRows.value = []
}

function resetForSelectedFile() {
  clearDashboardState()
  schoolKeyword.value = ''
  schoolId.value = ''
  Object.assign(schoolFilters, emptySchoolFilters())
  detailMode.value = 'meta'
  if (selectedFileId.value) loadAll()
}

async function refresh() {
  const currentFileId = selectedFileId.value
  await loadSources({ resetDashboard: false })
  const currentFile = dataFiles.value.find((item) => item.id === currentFileId)
  selectedProjectId.value = builtinProject.value?.id || null
  selectedFileId.value = currentFile?.id || dataFiles.value[0]?.id || null
  resetForSelectedFile()
}

async function loadAll() {
  if (!selectedFileId.value) return
  pageError.value = ''
  await Promise.allSettled([loadOverview(), loadSchoolTable(), loadRecords()])
}

async function loadOverview() {
  if (!selectedFileId.value) return
  const seq = ++overviewSeq
  overviewLoading.value = true
  try {
    const { data } = await api.get('/dashboard/overview', {
      timeout: 120000,
      params: {
        project_id: selectedProjectId.value,
        file_id: selectedFileId.value,
        school_keyword: schoolKeyword.value.trim(),
        school_id: schoolId.value.trim(),
      },
    })
    if (seq !== overviewSeq) return
    overview.value = data
  } catch (error) {
    if (seq === overviewSeq) pageError.value = errorMessage(error, '统计概览加载失败')
  } finally {
    if (seq === overviewSeq) overviewLoading.value = false
  }
}

function resetSchoolQuery() {
  schoolKeyword.value = ''
  schoolId.value = ''
  loadOverview()
}

function emptySchoolFilters() {
  return { city: '', area: '', school: '', schoolId: '', grade: '', submitMin: '', submitMax: '' }
}

const schoolFilters = reactive(emptySchoolFilters())

function numberInputValue(value) {
  if (value === '' || value === null || value === undefined) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function schoolFilterPayload() {
  const filters = []
  for (const [column, value] of Object.entries({
    city: schoolFilters.city,
    area: schoolFilters.area,
    school: schoolFilters.school,
    schoolId: schoolFilters.schoolId,
    grade: schoolFilters.grade,
  })) {
    const text = String(value || '').trim()
    if (text) filters.push({ column, kind: 'text', value: text })
  }
  const minimum = numberInputValue(schoolFilters.submitMin)
  const maximum = numberInputValue(schoolFilters.submitMax)
  if (minimum !== null || maximum !== null) {
    filters.push({ column: 'submit_count', kind: 'range', minimum, maximum })
  }
  return filters
}

async function loadSchoolTable() {
  if (!selectedFileId.value) return
  const seq = ++schoolSeq
  schoolLoading.value = true
  try {
    const { data } = await api.post('/dashboard/school-table', {
      project_id: selectedProjectId.value,
      file_id: selectedFileId.value,
      filters: schoolFilterPayload(),
    }, { timeout: 120000 })
    if (seq !== schoolSeq) return
    schoolRows.value = data.rows || []
    schoolTableTotal.value = data.total || 0
  } catch (error) {
    if (seq === schoolSeq) pageError.value = errorMessage(error, '学校统计加载失败')
  } finally {
    if (seq === schoolSeq) schoolLoading.value = false
  }
}

function resetSchoolFilters() {
  Object.assign(schoolFilters, emptySchoolFilters())
  loadSchoolTable()
}

function addDetailFilter() {
  if (!detailColumns.value.length) {
    ElMessage.warning('当前明细尚无可用列')
    return
  }
  detailFilterRows.value.push({
    id: ++filterRowId,
    column: detailColumns.value[0],
    kind: 'text',
    value: '',
    minimum: '',
    maximum: '',
    dateRange: [],
  })
}

function removeDetailFilter(id) {
  detailFilterRows.value = detailFilterRows.value.filter((row) => row.id !== id)
}

function buildDetailFilters() {
  const filters = []
  for (const row of detailFilterRows.value) {
    if (row.kind === 'text') {
      const value = String(row.value || '').trim()
      if (value) filters.push({ column: row.column, kind: 'text', value })
    } else if (row.kind === 'range') {
      const minimum = numberInputValue(row.minimum)
      const maximum = numberInputValue(row.maximum)
      if ((row.minimum !== '' && minimum === null) || (row.maximum !== '' && maximum === null)) {
        return { error: `筛选列「${columnLabel(row.column)}」的数值范围无效` }
      }
      if (minimum !== null || maximum !== null) {
        filters.push({ column: row.column, kind: 'range', minimum, maximum })
      }
    } else if (row.kind === 'date') {
      const range = Array.isArray(row.dateRange) ? row.dateRange : []
      if (range[0] || range[1]) {
        filters.push({ column: row.column, kind: 'date', start: range[0] || null, end: range[1] || null })
      }
    }
  }
  return { filters }
}

async function loadRecords() {
  if (!selectedFileId.value) return
  const built = buildDetailFilters()
  if (built.error) {
    ElMessage.warning(built.error)
    return
  }
  const seq = ++detailSeq
  detailLoading.value = true
  try {
    const { data } = await api.post('/dashboard/records', {
      project_id: selectedProjectId.value,
      file_id: selectedFileId.value,
      mode: detailMode.value,
      page: detailPage.value,
      page_size: detailPageSize.value,
      filters: built.filters,
    }, { timeout: 120000 })
    if (seq !== detailSeq) return
    detailRows.value = data.rows || []
    detailColumns.value = data.columns?.length ? data.columns : [...META_COLUMN_ORDER]
    detailTotal.value = data.total || 0
  } catch (error) {
    if (seq === detailSeq) pageError.value = errorMessage(error, '提交明细加载失败')
  } finally {
    if (seq === detailSeq) detailLoading.value = false
  }
}

function applyDetailFilters() {
  detailPage.value = 1
  loadRecords()
}

function resetDetailFilters() {
  detailFilterRows.value = []
  detailPage.value = 1
  loadRecords()
}

function handleModeChange() {
  detailPage.value = 1
  detailFilterRows.value = []
  loadRecords()
}

function handleDetailSizeChange() {
  detailPage.value = 1
  loadRecords()
}


loadSources()
</script>

<style scoped>
.statistics-page { max-width: 1600px; margin: 0 auto; }
.toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.page-title { margin: 0; font-size: 17px; color: #303133; }
.page-subtitle { margin: 5px 0 0; font-size: 13px; color: #909399; }
.page-alert { margin-bottom: 16px; }
.selector-card { margin-bottom: 16px; border-color: #e4e7ed; }
.selector-card :deep(.el-card__body) { padding: 16px 18px; }
.selector-grid {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(320px, 1.4fr) minmax(260px, 1fr);
  gap: 16px;
  align-items: end;
}
.builtin-selector-grid { grid-template-columns: minmax(320px, 1.2fr) minmax(260px, 1fr); }
.selector-item label {
  display: block;
  margin-bottom: 7px;
  font-size: 13px;
  color: #606266;
}
.selector-item :deep(.el-select) { width: 100%; }
.option-meta { float: right; margin-left: 24px; color: #909399; font-size: 12px; }
.selector-tip {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
  padding: 7px 12px;
  border-radius: 6px;
  color: #667085;
  background: #f4f7ff;
  font-size: 13px;
}
.selector-tip .el-icon { color: #409eff; }
.metric-row { margin-bottom: 16px; }
.metric-card { border-color: #e4e7ed; }
.metric-card :deep(.el-card__body) {
  display: flex;
  align-items: center;
  gap: 14px;
  min-height: 94px;
}
.metric-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 46px;
  width: 46px;
  height: 46px;
  border-radius: 10px;
  font-size: 22px;
}
.metric-icon.blue { color: #409eff; background: #ecf5ff; }
.metric-icon.green { color: #67c23a; background: #f0f9eb; }
.metric-icon.orange { color: #e6a23c; background: #fdf6ec; }
.metric-icon.purple { color: #7c5cfc; background: #f3efff; }
.metric-label { font-size: 13px; color: #909399; }
.metric-value { margin-top: 6px; font-size: 23px; font-weight: 600; color: #303133; line-height: 1.2; }
.panel-card { margin-bottom: 16px; border-color: #e4e7ed; }
.panel-card :deep(.el-card__header) { padding: 14px 18px; }
.panel-card :deep(.el-card__body) { padding: 16px 18px; }
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 15px;
  font-weight: 600;
  color: #303133;
}
.panel-tip { font-size: 12px; font-weight: 400; color: #909399; }
.query-grid {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) auto;
  gap: 12px;
  margin-bottom: 16px;
}
.query-actions { display: flex; gap: 8px; }
.query-result-alert { margin-bottom: 14px; }
.query-placeholder {
  padding: 34px 12px;
  border: 1px dashed #dcdfe6;
  border-radius: 8px;
  color: #909399;
  text-align: center;
  font-size: 13px;
  background: #fafafa;
}
.school-result {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 10px;
  padding: 14px 16px;
  border-radius: 8px;
  background: #f8fafc;
}
.school-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 17px;
  color: #303133;
}
.school-meta { margin-top: 7px; color: #909399; font-size: 13px; }
.school-result-stats { display: flex; gap: 28px; }
.school-result-stats span { display: block; color: #909399; font-size: 12px; }
.school-result-stats strong { display: block; margin-top: 5px; color: #303133; font-size: 20px; }
.filter-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(120px, 1fr)) minmax(240px, 1.3fr) auto;
  gap: 10px;
  align-items: center;
  margin-bottom: 14px;
}
.range-inputs {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  gap: 8px;
  align-items: center;
  color: #909399;
  font-size: 12px;
}
.filter-actions { display: flex; gap: 8px; }
.dashboard-table { width: 100%; }
.detail-panel :deep(.el-card__body) { padding-bottom: 12px; }
.detail-filter-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.detail-filter-title { font-size: 14px; font-weight: 600; color: #303133; }
.detail-filter-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 12px;
}
.detail-filter-row {
  display: grid;
  grid-template-columns: 220px 140px minmax(260px, 1fr) 40px;
  gap: 10px;
  align-items: center;
}
.filter-column-select,
.filter-kind-select { width: 100%; }
.filter-value { display: flex; align-items: center; gap: 8px; min-width: 0; }
.filter-value > * { flex: 1; min-width: 0; }
.range-separator { flex: 0 0 auto; color: #909399; font-size: 12px; }
.date-range { width: 100%; }
.detail-filter-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}
.detail-tip { margin-left: auto; color: #a8abb2; font-size: 12px; }
.detail-table { margin-bottom: 12px; }
.pagination-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.pagination-total { color: #909399; font-size: 13px; white-space: nowrap; }
:deep(.el-table th.el-table__cell) { background: #f8fafc; color: #606266; }
@media (max-width: 1200px) {
  .selector-grid { grid-template-columns: 1fr 1fr; }
  .selector-tip { grid-column: 1 / -1; }
  .filter-grid { grid-template-columns: repeat(3, minmax(160px, 1fr)); }
  .filter-actions { grid-column: 1 / -1; }
}
@media (max-width: 768px) {
  .toolbar { align-items: stretch; flex-direction: column; }
  .selector-grid,
  .query-grid,
  .filter-grid,
  .detail-filter-row { grid-template-columns: 1fr; }
  .query-actions,
  .filter-actions,
  .detail-filter-actions { flex-wrap: wrap; }
  .school-result { align-items: flex-start; flex-direction: column; }
  .school-result-stats { width: 100%; justify-content: space-between; }
  .pagination-row { align-items: flex-start; flex-direction: column; }
  .detail-tip { width: 100%; margin-left: 0; }
}
</style>









