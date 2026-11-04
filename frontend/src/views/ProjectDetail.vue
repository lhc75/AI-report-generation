<template>
  <div v-loading="pageLoading">
    <div class="toolbar">
      <div class="left">
        <el-button :icon="Back" text @click="$router.push('/projects')">返回</el-button>
        <h3 class="page-title">{{ project?.name }}</h3>
        <el-tag v-if="project" :type="statusType(project.status)">{{ statusText(project.status) }}</el-tag>
      </div>
      <div>
        <el-button
          v-if="project?.parsed_template_path"
          :icon="Download"
          @click="downloadParsed"
        >下载解析模版</el-button>
        <el-button
          v-if="project?.report_path"
          :icon="View"
          :disabled="previewLoading"
          @click="previewReport"
        >预览报告</el-button>
        <el-button
          v-if="project?.report_path"
          :icon="Download"
          @click="downloadReport"
        >下载报告</el-button>
        <el-button
          v-if="(project?.status === 'analyzed' || project?.status === 'assembling') && chapters.length"
          type="warning"
          :icon="FolderChecked"
          :loading="project?.status === 'assembling'"
          :disabled="mutationBlocked || !chapters.some((c) => c.status === 'done' && !c.is_stale)"
          @click="assembleProject"
        >{{ project?.status === 'assembling' ? '导出中…' : '导出报告' }}</el-button>
        <el-button
          v-if="project?.status === 'analyzed' && chapters.length"
          type="success"
          :icon="Promotion"
          :loading="activeOperation?.kind === 'generate' && mutationBlocked"
          :disabled="mutationBlocked"
          @click="generateAll"
        >全部生成</el-button>
        <el-button
          type="primary"
          :icon="MagicStick"
          :loading="project?.status === 'analyzing'"
          :disabled="!hasTemplate || mutationBlocked"
          @click="analyze"
        >{{ project?.status === 'analyzing' ? '分析中…' : '分析' }}</el-button>
      </div>
    </div>

    <div v-if="project?.status === 'assembling' && project?.progress?.label" class="progress-row">
      <el-progress
        :percentage="project.progress.percent || 0"
        :stroke-width="12"
        :show-text="false"
        style="flex: 1"
      />
      <span class="progress-label">{{ progressText }}</span>
    </div>

    <el-alert
      v-if="project?.error_msg"
      type="error"
      :title="project.error_msg"
      show-icon
      style="margin-bottom: 16px"
    />

    <el-alert v-if="loadError" type="error" :closable="false" show-icon class="page-alert">
      <template #title>{{ loadError }}</template>
      <el-button size="small" @click="loadPage">重新加载</el-button>
    </el-alert>
    <el-alert v-if="pollError" :type="pollStopped ? 'error' : 'warning'" :title="pollError"
      :closable="false" show-icon class="page-alert">
      <el-button v-if="pollStopped" size="small" @click="retryPolling">重新连接</el-button>
    </el-alert>
    <el-alert v-if="chapters.some((c) => c.is_stale)" type="warning" :closable="false"
      :title="project?.status === 'created'
        ? '资料或统计设置已变更，旧内容已保留；请重新分析并生成后导出。'
        : '生成指令或内容已更新，旧内容已保留；请重新生成待更新模块后导出。'" class="page-alert" />

    <el-row :gutter="16" class="upload-row">
      <el-col :span="8" v-for="slot in slots" :key="slot.type">
        <div class="upload-card">
          <div class="upload-head">
            <span class="upload-title">{{ slot.title }}</span>
            <span class="upload-tip">{{ slot.tip }}</span>
          </div>
          <el-upload
            :show-file-list="false"
            :http-request="(opt) => upload(slot.type, opt)"
            :accept="slot.accept"
            :multiple="slot.type !== 'template'"
            :disabled="mutationBlocked"
          >
            <el-button :icon="UploadFilled" :disabled="mutationBlocked">上传{{ slot.title }}</el-button>
          </el-upload>
          <div class="file-list">
            <div v-for="f in filesOf(slot.type)" :key="f.id" class="file-item">
              <el-icon><Document /></el-icon>
              <span class="file-name" :title="f.file_name">{{ f.file_name }}</span>
              <el-button v-if="f.file_type === 'data'" link type="primary" :disabled="pageLoading || !!loadError"
                @click="openStatistics(f)">统计设置</el-button>
              <el-button link type="danger" :icon="Delete" :disabled="mutationBlocked" @click="removeFile(f)" />
            </div>
            <div v-if="!filesOf(slot.type).length" class="empty-tip">未上传</div>
          </div>
        </div>
      </el-col>
    </el-row>

    <div class="chapter-section">
      <div class="section-head">
        <h4 class="section-title">章节写作任务</h4>
        <span class="section-tip">解析模版后，每个章节需要编写的内容会保存在这里；点击"生成"逐模块填写内容</span>
      </div>
      <el-table :data="chapterTree" row-key="id" border default-expand-all
        :tree-props="{ children: 'children' }" v-loading="chaptersLoading">
        <el-table-column prop="title" label="标题 / 指令" min-width="380" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tooltip :content="taskDisplay(row).tooltip" placement="top">
              <div class="task-copy">
                <span class="task-primary">{{ taskDisplay(row).primary }}</span>
                <span v-if="taskDisplay(row).secondary" class="task-instruction">{{ taskDisplay(row).secondary }}</span>
              </div>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.task_type !== 'heading'" size="small" :type="taskTag(row.task_type)">
              {{ taskText(row.task_type) }}
            </el-tag>
            <el-tag v-else size="small" type="info" effect="plain">标题</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <template v-if="row.task_type !== 'heading'">
              <el-tooltip v-if="row.latest_error" :content="row.latest_error">
                <span class="task-error">{{ taskStatusText(row.status) }}</span>
              </el-tooltip>
              <span v-else>{{ taskStatusText(row.status) }}</span>
              <el-tag v-if="row.is_stale" type="warning" size="small">待更新</el-tag>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="350">
          <template #default="{ row }">
            <el-button
              link type="primary" size="small" :disabled="mutationBlocked" @click="openEdit(row)"
            >编辑</el-button>
            <template v-if="row.task_type !== 'heading'">
              <el-button
                v-if="!row.has_content || row.is_stale"
                link type="primary" size="small" :disabled="mutationBlocked" @click="generateOne(row)"
              >生成</el-button>
              <el-button
                v-if="row.history_count > 0 || row.has_content"
                link type="primary" size="small" @click="openContent(row)"
              >内容与历史</el-button>
              <el-button
                link type="warning" size="small" :disabled="mutationBlocked" @click="openRewrite(row)"
              >{{ row.has_content ? '重写' : '自定义生成' }}</el-button>
              <el-button
                link size="small" :disabled="mutationBlocked" @click="openInsert(row)"
              >插入</el-button>
              <el-button
                link type="danger" size="small" :disabled="mutationBlocked" @click="removeTask(row)"
              >删除</el-button>
            </template>
          </template>
        </el-table-column>
        <template #empty>点击右上角"分析"解析模版</template>
      </el-table>
    </div>

    <el-dialog v-model="editVisible" :title="editTask?.task_type === 'heading' ? '编辑章节标题' : '编辑模块'"
      width="640px" :close-on-click-modal="!acting" :close-on-press-escape="!acting" :show-close="!acting">
      <div class="dialog-tip">{{ editTask?.task_type === 'heading'
        ? '保存标题后，请重新导出报告。'
        : '保存后请重新生成此模块；已有内容和历史版本会保留。指令中含“生成表格/生成图片”时会自动调整类型。' }}</div>
      <el-alert v-if="editError" type="error" :title="editError" :closable="false" show-icon class="page-alert" />
      <el-form label-position="top">
        <el-form-item label="标题">
          <el-input v-model="editTitle" :disabled="mutationBlocked" maxlength="512" show-word-limit
            placeholder="填写章节或模块标题" />
        </el-form-item>
        <el-form-item v-if="editTask?.task_type !== 'heading'" label="生成指令">
          <el-input v-model="editInstruction" type="textarea" :rows="7" :disabled="mutationBlocked" maxlength="20000" show-word-limit
            placeholder="填写数据来源、生成内容及要求" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="acting" @click="cancelEdit">取消</el-button>
        <el-button type="primary" :loading="acting" :disabled="mutationBlocked" @click="saveEdit">保存修改</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="rewriteVisible" title="AI 重写" width="560px">
      <div class="dialog-tip">填写补充要求（可留空按原指令重新生成），历史版本会保留，可在"查看"中回退。</div>
      <el-input
        v-model="rewritePrompt"
        type="textarea"
        :rows="4"
        placeholder="例如：语言更精炼；突出占比最高的两项；表格增加合计行……"
      />
      <template #footer>
        <el-button @click="rewriteVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" :disabled="mutationBlocked" @click="doRewrite">开始重写</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="insertVisible" title="插入新模块" width="560px">
      <div class="dialog-tip">在「{{ insertAfter?.title }}」之后插入，AI 会根据描述自动生成内容。描述中含"生成表格/生成图片"时按对应类型生成。</div>
      <el-input
        v-model="insertRequirement"
        type="textarea"
        :rows="4"
        placeholder="例如：生成图片（柱状图）：各地区教师问卷提交量对比，数据来自 survey-submissions.xlsx"
      />
      <template #footer>
        <el-button @click="insertVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" :disabled="mutationBlocked" @click="doInsert">插入并生成</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="contentVisible" :title="contentTask?.title" width="720px" @closed="cancelContent">
      <div v-if="versions.length" class="content-head">
        <el-select v-model="selectedContentId" size="small" style="width: 240px" @change="switchVersion">
          <el-option
            v-for="v in versions"
            :key="v.id"
            :value="v.id"
            :label="`v${v.version} · ${v.created_at}${v.is_current ? '（当前）' : ''}${v.status === 'failed' ? '（失败）' : ''}${v.is_stale ? '（待更新）' : ''}`"
          />
        </el-select>
        <el-button
          v-if="contentDetail && !contentDetail.is_current && contentDetail.status === 'ok'"
          size="small" type="warning" :disabled="mutationBlocked || contentLoading || contentDetail.can_rollback === false" @click="rollback"
        >回退到此版本</el-button>
      </div>
      <el-alert v-if="contentDetail?.status === 'ok' && contentDetail.can_rollback === false && contentDetail.is_stale"
        type="warning" title="资料或生成指令已更新，该版本仅可查看，不能回退。" show-icon class="page-alert" />
      <el-alert v-if="contentError" type="error" :title="contentError" show-icon class="page-alert" />
      <el-alert v-if="contentTask?.latest_error && contentDetail?.status !== 'failed'" type="warning"
        :title="`最近一次生成失败：${contentTask.latest_error}`" show-icon class="page-alert" />
      <el-alert
        v-if="contentDetail?.status === 'failed'"
        type="error" :title="contentDetail.error_msg" show-icon style="margin-bottom: 12px"
      />
      <div v-if="contentDetail?.prompt" class="content-prompt">重写要求：{{ contentDetail.prompt }}</div>
      <div v-loading="contentLoading" class="content-body">
        <template v-if="contentDetail?.status === 'ok'">
          <div v-if="contentDetail.content_type === 'text'" class="text-content">{{ displayedContent }}</div>
          <template v-else-if="contentDetail.content_type === 'table'">
            <el-table :data="tableRows" border size="small">
              <el-table-column
                v-for="(col, i) in tableColumns" :key="i" :prop="String(i)" :label="col"
              />
            </el-table>
            <p v-for="(note, i) in tableNotes" :key="i" class="table-note">{{ note }}</p>
          </template>
          <img v-else-if="contentDetail.content_type === 'image'" :src="contentDetail.image_url" class="chart-img" alt="图表" />
        </template>
        <el-empty v-else description="该版本没有内容" />
      </div>
    </el-dialog>

    <el-dialog v-model="statisticsVisible" :title="`统计设置 · ${statisticsFile?.file_name || ''}`"
      width="min(840px, calc(100vw - 32px))">
      <div class="dialog-tip">普通分类列默认不拆分；问卷打包答案按实际多选分隔符统计。分母为该题作答记录数，每份答卷内重复选项去重。这里只保存手动设置的覆盖项。</div>
      <el-alert v-if="statisticsError" type="error" :title="statisticsError" show-icon class="page-alert" />
      <el-table :data="statisticsFields" border max-height="440" v-loading="statisticsLoading">
        <el-table-column prop="sheet" label="工作表" min-width="100" />
        <el-table-column prop="column_name" label="列名" min-width="150">
          <template #default="{ row }">{{ row.column_name }}（第 {{ row.column_index + 1 }} 列）</template>
        </el-table-column>
        <el-table-column label="题型" width="190">
          <template #default="{ row }">
            <el-select v-model="row.question_type" :disabled="mutationBlocked">
              <el-option label="默认单选（不拆分）" value="" />
              <el-option label="单选" value="single" />
              <el-option label="多选" value="multiple" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="多选分隔符" width="160">
          <template #default="{ row }">
            <el-input v-model="row.separator" placeholder="例如 / 或 ;"
              :disabled="mutationBlocked || row.question_type !== 'multiple'" />
          </template>
        </el-table-column>
      </el-table>
      <div class="dialog-tip" style="margin-top: 12px">保存后需要重新分析和生成，旧版本仍可在内容与历史中查看。</div>
      <template #footer>
        <el-button @click="statisticsVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" :disabled="mutationBlocked || statisticsLoading || !!statisticsError"
          @click="saveStatistics">保存设置</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="previewVisible" title="报告预览" fullscreen destroy-on-close @closed="clearPreview">
      <div v-loading="previewLoading" class="preview-body">
        <el-alert v-if="previewError" type="error" :title="previewError" :closable="false" show-icon />
        <div v-show="!previewError" ref="previewBodyRef" class="preview-docx"></div>
      </div>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Back, Delete, Document, Download, FolderChecked, MagicStick, Promotion, UploadFilled, View } from '@element-plus/icons-vue'
import api from '../api'
import { isProjectBusy, statisticsConfig, statisticsRows, summarizeOperation } from '../projectState'
import { createProjectPoller } from '../projectPolling'
import { renderAsync } from 'docx-preview'

const route = useRoute()
const pid = route.params.id
const project = ref(null)
const files = ref([])
const chapters = ref([])
const pageLoading = ref(true)
const loadError = ref('')
const previewVisible = ref(false)
const previewLoading = ref(false)
const previewError = ref('')
const previewBodyRef = ref(null)
const chaptersLoading = ref(false)
const acting = ref(false)
const pendingUploads = ref(0)
const pollError = ref('')
const pollStopped = ref(false)
const activeOperation = ref(null)
const projectBusy = computed(() => isProjectBusy(project.value, chapters.value))
const progressText = computed(() => {
  const progress = project.value?.progress
  if (!progress?.label) return ''
  return progress.total ? `${progress.label} ${progress.done}/${progress.total}` : progress.label
})
const mutationBlocked = computed(() => pageLoading.value || !!loadError.value || !!pollError.value
  || acting.value || pendingUploads.value > 0 || projectBusy.value)
let disposed = false
let snapshotRequest = null
let snapshotController = null
let uploadQueue = Promise.resolve()

const editVisible = ref(false)
const editTask = ref(null)
const editTitle = ref('')
const editInstruction = ref('')
const editError = ref('')
const rewriteVisible = ref(false)
const rewritePrompt = ref('')
const rewriteTask = ref(null)
const insertVisible = ref(false)
const insertRequirement = ref('')
const insertAfter = ref(null)
const contentVisible = ref(false)
const contentTask = ref(null)
const contentDetail = ref(null)
const versions = ref([])
const selectedContentId = ref(null)
const contentLoading = ref(false)
const contentError = ref('')
let contentRequest = null
let contentSequence = 0
const statisticsVisible = ref(false)
const statisticsFile = ref(null)
const statisticsFields = ref([])
const preservedStatisticsFields = ref([])
const statisticsLoading = ref(false)
const statisticsError = ref('')
let statisticsSequence = 0

const slots = [
  { type: 'template', title: '模版', tip: 'DOCX（点击“分析”后由 AI 自动生成红色指令，也可自行标红）', accept: '.docx' },
  { type: 'reference', title: '参考资料', tip: 'DOCX / PDF，可上传多份', accept: '.docx,.pdf' },
  { type: 'data', title: '数据', tip: 'Excel / CSV，可上传多份', accept: '.xlsx,.xls,.csv' },
]
const hasTemplate = computed(() => files.value.some((file) => file.file_type === 'template'))
const chapterTree = computed(() => {
  const map = new Map(chapters.value.map((chapter) => [chapter.id, { ...chapter, children: [] }]))
  const roots = []
  for (const node of map.values()) {
    if (node.parent_id && map.has(node.parent_id)) map.get(node.parent_id).children.push(node)
    else roots.push(node)
  }
  const clean = (list) => list.forEach((node) => {
    if (!node.children.length) delete node.children
    else clean(node.children)
  })
  clean(roots)
  return roots
})
const displayedContent = computed(() => contentDetail.value?.display_content ?? contentDetail.value?.content ?? '')
const tableColumns = computed(() => {
  try { return JSON.parse(displayedContent.value || '{}').columns || [] }
  catch { return [] }
})
const tableRows = computed(() => {
  try {
    const rows = JSON.parse(displayedContent.value || '{}').rows || []
    return rows.map((row) => Object.fromEntries(row.map((value, i) => [String(i), value])))
  } catch { return [] }
})
const tableNotes = computed(() => {
  try {
    const table = JSON.parse(displayedContent.value || '{}')
    return [...new Set([...(typeof table.note === 'string' ? [table.note] : []),
      ...(Array.isArray(table.notes) ? table.notes.filter((note) => typeof note === 'string') : [])]
      .map((note) => note.trim()).filter(Boolean))]
  } catch { return [] }
})

function filesOf(type) { return files.value.filter((file) => file.file_type === type) }
function statusType(status) {
  return { analyzed: 'success', analyzing: 'warning', generating: 'warning', assembling: 'warning', failed: 'danger' }[status] || 'info'
}
function statusText(status) {
  return { created: '待分析', analyzing: '分析中', generating: '生成中', assembling: '导出中', analyzed: '已分析', failed: '分析失败' }[status] || status
}
function taskStatusText(status) {
  return { pending: '待生成', queued: '排队中', generating: '生成中…', done: '已生成', failed: '失败' }[status] || status
}
function taskTag(type) { return { text: 'info', table: 'warning', image: 'success' }[type] || 'info' }
function taskText(type) { return { text: '文字', table: '表格', image: '图片' }[type] || type }
function taskDisplay(row) {
  const title = (row.title || '').trim()
  const instruction = row.task_type === 'heading' ? '' : (row.instruction || '').trim()
  const titlePrefix = title.replace(/(?:…|\.{3})$/, '').trimEnd()
  const titleIsInstruction = !title || title === instruction
    || (titlePrefix && instruction.startsWith(titlePrefix))
  const primary = instruction && titleIsInstruction ? instruction : title
  const secondary = instruction && !titleIsInstruction ? instruction : ''
  return { primary, secondary, tooltip: secondary ? `${primary}\n${secondary}` : primary }
}
function errorText(error) {
  const detail = error.response?.data?.detail
  const message = Array.isArray(detail) ? detail.map((item) => typeof item === 'string' ? item : item.msg)
    .filter(Boolean).join('；') : detail
  return typeof message === 'string' && message ? message : error.message || '请求失败'
}
function isCancelled(error) { return error.code === 'ERR_CANCELED' || error.name === 'AbortError' }
function guardMutation() {
  if (!mutationBlocked.value && !disposed) return true
  ElMessage.warning('项目正在处理或状态尚未确认，请稍后再操作')
  return false
}

// All refreshes share one request so a manual reload cannot overlap a poll.
async function readSnapshot(signal) {
  if (snapshotRequest) return snapshotRequest
  snapshotController = new AbortController()
  const controller = snapshotController
  const abort = () => controller.abort()
  if (signal?.aborted) controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  const options = { signal: controller.signal, silentError: true }
  snapshotRequest = Promise.allSettled([
    api.get(`/projects/${pid}`, options),
    api.get(`/projects/${pid}/chapters`, options),
    api.get(`/projects/${pid}/files`, options),
  ]).then((results) => {
    const failure = results.find((result) => result.status === 'rejected')
    if (failure) throw failure.reason
    const [p, ch, f] = results.map((result) => result.value)
    return { project: p.data, chapters: ch.data, files: f.data }
  })
  try { return await snapshotRequest }
  finally {
    signal?.removeEventListener('abort', abort)
    snapshotRequest = null
    if (snapshotController === controller) snapshotController = null
  }
}
function applySnapshot(snapshot) {
  if (disposed) return
  project.value = snapshot.project
  chapters.value = snapshot.chapters
  files.value = snapshot.files
  loadError.value = ''
}
function finishOperation() {
  if (!activeOperation.value) return
  const result = summarizeOperation(project.value, chapters.value, activeOperation.value)
  ElMessage[result.type](result.message)
  activeOperation.value = null
}
const poller = createProjectPoller({
  read: readSnapshot,
  onSnapshot(snapshot) {
    if (disposed) return false
    applySnapshot(snapshot)
    pollError.value = ''
    pollStopped.value = false
    if (projectBusy.value) return true
    finishOperation()
    return false
  },
  onError(error) {
    if (isCancelled(error) || disposed) return false
    const status = error.response?.status
    pollStopped.value = status === 401 || status === 403 || status === 404
    pollError.value = pollStopped.value
      ? `无法继续查询项目：${errorText(error)}`
      : '项目状态暂时无法获取，正在自动重试；确认状态后可继续操作。'
    return !pollStopped.value
  },
})
function startPolling() { poller.start() }
function retryPolling() {
  pollStopped.value = false
  poller.start()
}
async function refreshSnapshot() {
  try {
    const snapshot = await readSnapshot()
    if (disposed) return
    applySnapshot(snapshot)
    pollError.value = ''
    if (projectBusy.value || activeOperation.value) startPolling()
  } catch (error) {
    if (isCancelled(error) || disposed) return
    loadError.value = `项目状态加载失败：${errorText(error)}`
    if (activeOperation.value || projectBusy.value) startPolling()
    throw error
  }
}
async function loadPage() {
  pageLoading.value = true
  chaptersLoading.value = true
  try {
    await refreshSnapshot()
    if (projectBusy.value && !activeOperation.value) {
      const kind = project.value.status === 'analyzing' ? 'analyze'
        : project.value.status === 'assembling' ? 'assemble' : 'generate'
      activeOperation.value = { kind, runId: project.value.operation_token }
      startPolling()
    }
  } catch { /* The persistent error alert includes a retry action. */ }
  finally {
    if (!disposed) {
      pageLoading.value = false
      chaptersLoading.value = false
    }
  }
}

async function mutate(action, { kind, taskIds, message, onSuccess } = {}) {
  if (!guardMutation()) return false
  acting.value = true
  try {
    // A final poll may still be returning when the previous run releases its
    // lock. Settle that read before starting a new run so its old snapshot
    // cannot be mistaken for the new run's completion.
    if (snapshotRequest) {
      applySnapshot(await snapshotRequest)
      if (disposed) return false
      if (projectBusy.value) {
        ElMessage.warning('项目正在处理，请等待完成后再操作')
        return false
      }
    }
    const { data } = await action()
    if (disposed) return false
    if (kind) {
      activeOperation.value = { kind, runId: data.run_id, taskIds }
      // Keep the page locked until the server snapshot arrives, even for a fast run.
      if (data.run_id) project.value.operation_token = data.run_id
    }
    onSuccess?.(data)
    if (message) ElMessage.success(message)
    await refreshSnapshot()
    if (kind) startPolling()
    return true
  } catch (error) {
    if (!isCancelled(error) && !disposed) {
      // A conflict may come from another browser: refresh before enabling edits.
      if ([400, 409, 423].includes(error.response?.status)) {
        try { await refreshSnapshot() } catch { /* refreshSnapshot records the error. */ }
      }
    }
    return false
  } finally { if (!disposed) acting.value = false }
}

function upload(type, options) {
  // Element Plus starts each selected file separately. Queue the selection and
  // keep every other mutation locked until all uploads and snapshots complete.
  if (disposed || projectBusy.value || acting.value || loadError.value || pollError.value) {
    ElMessage.warning('项目正在处理或状态尚未确认，暂时无法上传')
    return Promise.reject(new Error('项目暂时无法上传'))
  }
  pendingUploads.value += 1
  const pending = uploadQueue.then(async () => {
    if (disposed || projectBusy.value) throw new Error('项目正在处理，暂时无法上传')
    const form = new FormData()
    form.append('file', options.file)
    try {
      const response = await api.post(`/projects/${pid}/files`, form, {
        params: { file_type: type }, silentError: true,
      })
      if (!disposed) ElMessage.success({ message: `「${options.file.name}」上传成功`, grouping: true })
      await refreshSnapshot()
      return response.data
    } catch (error) {
      if (!disposed) ElMessage.error(`「${options.file.name}」上传失败：${errorText(error)}`)
      throw error
    }
  }).finally(() => { if (!disposed) pendingUploads.value -= 1 })
  uploadQueue = pending.catch(() => {})
  return pending
}
async function confirmAction(text) {
  try {
    await ElMessageBox.confirm(text, '提示', { type: 'warning' })
    return true
  } catch { return false }
}
async function removeFile(file) {
  if (!guardMutation() || !await confirmAction(`确定删除「${file.file_name}」？`)) return
  await mutate(() => api.delete(`/projects/${pid}/files/${file.id}`), { message: '文件已删除' })
}
async function analyze() {
  await mutate(() => api.post(`/projects/${pid}/analyze`), {
    kind: 'analyze', message: '已开始分析模版',
  })
}
async function generateAll() {
  const taskIds = chapters.value.filter((task) => task.task_type !== 'heading'
    && (task.is_stale || ['pending', 'failed'].includes(task.status))).map((task) => task.id)
  await mutate(() => api.post(`/projects/${pid}/generate`), {
    kind: 'generate', taskIds, message: '已开始批量生成内容',
  })
}
async function generateOne(row) {
  await mutate(() => api.post(`/projects/${pid}/tasks/${row.id}/generate`), {
    kind: 'generate', taskIds: [row.id], message: '已开始生成',
  })
}
function openEdit(row) {
  if (!guardMutation()) return
  editTask.value = row
  editTitle.value = row.title || ''
  editInstruction.value = row.instruction || ''
  editError.value = ''
  editVisible.value = true
}
function cancelEdit() {
  if (acting.value) return
  editVisible.value = false
  editError.value = ''
}
async function saveEdit() {
  if (!editTask.value || !guardMutation()) return
  const title = editTitle.value.trim()
  const instruction = editInstruction.value.trim()
  if (!title || (editTask.value.task_type !== 'heading' && !instruction)) {
    editError.value = !title ? '请填写标题' : '请填写生成指令'
    return
  }
  const task = editTask.value
  const body = task.task_type === 'heading' ? { title } : { title, instruction }
  editError.value = ''
  await mutate(async () => {
    try {
      return await api.patch(`/projects/${pid}/tasks/${task.id}`, body, { silentError: true })
    } catch (error) {
      if (!isCancelled(error) && !disposed) editError.value = `保存失败：${errorText(error)}`
      throw error
    }
  }, {
    message: task.task_type === 'heading' ? '标题已保存，请重新导出报告' : '修改已保存，请重新生成此模块',
    onSuccess: () => { editVisible.value = false },
  })
}
function openRewrite(row) {
  if (!guardMutation()) return
  rewriteTask.value = row
  rewritePrompt.value = ''
  rewriteVisible.value = true
}
async function doRewrite() {
  await mutate(() => api.post(`/projects/${pid}/tasks/${rewriteTask.value.id}/rewrite`, { prompt: rewritePrompt.value }), {
    kind: 'generate', taskIds: [rewriteTask.value.id], message: '已开始重写',
    onSuccess: () => { rewriteVisible.value = false },
  })
}
function openInsert(row) {
  if (!guardMutation()) return
  insertAfter.value = row
  insertRequirement.value = ''
  insertVisible.value = true
}
async function doInsert() {
  if (!insertRequirement.value.trim()) {
    ElMessage.warning('请填写新模块的需求描述')
    return
  }
  await mutate(() => api.post(`/projects/${pid}/tasks/insert`, {
    after_task_id: insertAfter.value.id, requirement: insertRequirement.value.trim(),
  }), {
    kind: 'generate', message: '已插入，开始生成',
    onSuccess: (data) => {
      insertVisible.value = false
      activeOperation.value.taskIds = [data.task_id]
    },
  })
}
async function removeTask(row) {
  if (!guardMutation() || !await confirmAction(`确定删除「${row.title}」？其生成内容会一并删除。`)) return
  await mutate(() => api.delete(`/projects/${pid}/tasks/${row.id}`), { message: '模块已删除' })
}

function cancelContent() {
  contentSequence += 1
  contentRequest?.abort()
  contentRequest = null
  contentLoading.value = false
}
async function openContent(row, contentId = null) {
  cancelContent()
  const sequence = contentSequence
  contentRequest = new AbortController()
  const newTask = contentTask.value?.id !== row.id
  if (newTask) versions.value = []
  contentTask.value = row
  contentDetail.value = null
  selectedContentId.value = contentId
  contentError.value = ''
  contentVisible.value = true
  contentLoading.value = true
  try {
    const { data } = await api.get(`/projects/${pid}/tasks/${row.id}/contents`, {
      params: contentId !== null ? { content_id: contentId } : {},
      signal: contentRequest.signal, silentError: true,
    })
    if (disposed || sequence !== contentSequence) return
    versions.value = data.versions
    contentDetail.value = data.content
    selectedContentId.value = data.content?.id ?? null
  } catch (error) {
    if (!disposed && sequence === contentSequence && !isCancelled(error)) {
      contentError.value = `内容加载失败：${errorText(error)}`
    }
  } finally {
    if (!disposed && sequence === contentSequence) contentLoading.value = false
  }
}
function switchVersion(contentId) { openContent(contentTask.value, contentId) }
async function rollback() {
  if (!contentDetail.value || contentDetail.value.status !== 'ok'
    || contentDetail.value.is_current || contentDetail.value.can_rollback === false) return
  const row = contentTask.value
  const success = await mutate(() => api.post(`/projects/${pid}/tasks/${row.id}/rollback`, {
    content_id: contentDetail.value.id,
  }), { message: '已回退到此版本' })
  if (success) await openContent(row)
}

async function openStatistics(file) {
  const sequence = ++statisticsSequence
  statisticsFile.value = file
  statisticsFields.value = []
  preservedStatisticsFields.value = []
  statisticsError.value = ''
  statisticsLoading.value = true
  statisticsVisible.value = true
  try {
    const { data } = await api.get(`/projects/${pid}/files/${file.id}/statistics-config`, { silentError: true })
    if (!disposed && sequence === statisticsSequence) {
      statisticsFields.value = statisticsRows(data.sheets, data.config)
      preservedStatisticsFields.value = (data.config?.fields || []).filter((field) => field.question_key)
    }
  } catch (error) {
    if (!disposed && sequence === statisticsSequence) statisticsError.value = errorText(error)
  } finally {
    if (!disposed && sequence === statisticsSequence) statisticsLoading.value = false
  }
}
async function saveStatistics() {
  const config = statisticsConfig(statisticsFields.value, preservedStatisticsFields.value)
  if (config.fields.some((field) => field.question_type === 'multiple' && !field.separator.trim())) {
    ElMessage.warning('请填写每个多选列的选项分隔符')
    return
  }
  await mutate(() => api.put(`/projects/${pid}/files/${statisticsFile.value.id}/statistics-config`, config), {
    message: '统计设置已保存，请重新分析和生成',
    onSuccess: () => { statisticsVisible.value = false },
  })
}
async function downloadParsed() {
  const { data } = await api.get(`/projects/${pid}/parsed-template`)
  window.open(data.url, '_blank')
}
async function downloadReport() {
  const { data } = await api.get(`/projects/${pid}/report`)
  window.open(data.url, '_blank')
}async function blobErrorMessage(error) {
  const data = error?.response?.data
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text())
      if (typeof parsed?.detail === 'string' && parsed.detail) return parsed.detail
    } catch { /* 非 JSON 错误体，走通用提示 */ }
  }
  return '报告预览失败'
}
async function previewReport() {
  previewVisible.value = true
  previewLoading.value = true
  previewError.value = ''
  if (previewBodyRef.value) previewBodyRef.value.innerHTML = ''
  try {
    const { data } = await api.get(`/projects/${pid}/report/preview`, { responseType: 'blob', silentError: true })
    await nextTick()
    await renderAsync(data, previewBodyRef.value)
  } catch (error) {
    previewError.value = await blobErrorMessage(error)
    ElMessage.error(previewError.value)
  } finally {
    previewLoading.value = false
  }
}
function clearPreview() {
  previewError.value = ''
  if (previewBodyRef.value) previewBodyRef.value.innerHTML = ''
}
async function assembleProject() {
  await mutate(() => api.post(`/projects/${pid}/assemble`), {
    kind: 'assemble', message: '已开始组装导出',
  })
}

onMounted(loadPage)
onBeforeUnmount(() => {
  disposed = true
  poller.stop()
  snapshotController?.abort()
  cancelContent()
  statisticsSequence += 1
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.left { display: flex; align-items: center; gap: 12px; }
.page-title { margin: 0; font-size: 17px; }
.progress-row { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.progress-label { color: #606266; font-size: 13px; white-space: nowrap; }
.upload-row { margin-bottom: 20px; }
.upload-card {
  background: #fff; border: 1px solid #e4e7ed; border-radius: 8px; padding: 14px 16px;
}
.upload-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 10px; }
.upload-title { font-weight: 600; }
.upload-tip { font-size: 12px; color: #909399; }
.file-list { margin-top: 10px; }
.file-item { display: flex; align-items: center; gap: 6px; padding: 3px 0; font-size: 13px; }
.file-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty-tip { font-size: 12px; color: #c0c4cc; padding: 4px 0; }
.chapter-section { background: #fff; border: 1px solid #e4e7ed; border-radius: 8px; padding: 14px 16px; }
.section-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }
.section-title { margin: 0; font-size: 15px; }
.section-tip { font-size: 12px; color: #909399; }
.page-alert { margin-bottom: 16px; }
.task-copy { display: inline-block; max-width: 100%; vertical-align: middle; }
.task-primary, .task-instruction { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task-instruction { margin-top: 3px; color: #909399; font-size: 12px; }
.task-error { color: #f56c6c; cursor: help; }
.dialog-tip { font-size: 12px; color: #909399; margin-bottom: 10px; }
.content-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.content-prompt { font-size: 12px; color: #909399; margin-bottom: 8px; }
.content-body { min-height: 120px; }
.text-content { white-space: pre-wrap; line-height: 1.8; font-size: 14px; }
.table-note { white-space: pre-wrap; line-height: 1.7; color: #606266; font-size: 13px; }
.chart-img { max-width: 100%; border: 1px solid #ebeef5; }
</style>

.preview-body { min-height: 60vh; }
.preview-docx { max-width: 960px; margin: 0 auto; }
