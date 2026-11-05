<template>
  <el-dialog
    :model-value="modelValue"
    title="关于"
    width="640px"
    class="about-dialog"
    @update:model-value="$emit('update:modelValue', $event)"
    @open="load"
  >
    <div class="about-head">
      <div>
        <div class="app-name">{{ about.name || 'AI 报告生成' }}</div>
        <div class="app-version">
          当前版本 <strong>{{ displayVersion(about.version) }}</strong>
          <span v-if="about.platform" class="muted">
            · {{ platformLabel(about.platform) }} {{ about.arch }}
          </span>
          <span v-if="about.desktop" class="muted"> · 桌面客户端</span>
        </div>
      </div>
      <el-tag :type="statusTagType" effect="plain">{{ statusLabel }}</el-tag>
    </div>

    <el-alert
      v-if="updateError"
      type="warning"
      :closable="false"
      show-icon
      title="检查更新失败"
      :description="updateError"
      class="block"
    />

    <div v-else-if="checking" class="block checking">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>正在检查 GitHub 上的新版本…</span>
    </div>

    <template v-else-if="update">
      <el-alert
        v-if="update.has_update"
        type="success"
        :closable="false"
        show-icon
        class="block"
        :title="`发现新版本 ${displayVersion(update.latest_version)}（当前 ${displayVersion(update.current_version)}）`"
        :description="publishedText"
      />
      <el-alert
        v-else
        type="info"
        :closable="false"
        show-icon
        class="block"
        :title="`已是最新版本（${displayVersion(update.current_version)}）`"
        :description="publishedText"
      />

      <div v-if="update.has_update && update.release_notes" class="notes-wrap">
        <div class="notes-title">更新说明</div>
        <pre class="notes">{{ update.release_notes }}</pre>
      </div>

      <div v-if="update.has_update" class="actions">
        <el-button
          v-if="canAutoInstall"
          type="primary"
          :loading="installing"
          :disabled="installing || installed"
          @click="install"
        >
          {{ installed ? '安装程序已启动' : '下载并安装' }}
        </el-button>
        <el-button
          v-else
          type="primary"
          @click="openRelease(update.recommended)"
        >前往下载</el-button>
        <el-button @click="openRelease()">打开发布页</el-button>
        <span v-if="assetHint" class="muted asset-hint">{{ assetHint }}</span>
      </div>

      <el-progress
        v-if="installing && progress.total"
        class="block"
        :percentage="progressPercent"
        :stroke-width="10"
      />
      <div v-if="progressText" class="muted block">{{ progressText }}</div>
      <div v-if="installMessage" class="install-message">{{ installMessage }}</div>
    </template>

    <div class="about-foot">
      <span class="muted">
        项目地址：
        <a :href="about.release_url || repoUrl" target="_blank" rel="noreferrer">{{ repoText }}</a>
      </span>
      <div class="foot-actions">
        <el-button size="small" :loading="checking" @click="refresh(true)">重新检查</el-button>
        <el-button size="small" @click="$emit('update:modelValue', false)">关闭</el-button>
      </div>
    </div>
  </el-dialog>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import { checkUpdate, getAbout } from '../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'update-available'])

const about = ref({})
const update = ref(null)
const updateError = ref('')
const checking = ref(false)
const installing = ref(false)
const installed = ref(false)
const installMessage = ref('')
const progress = ref({ received: 0, total: 0 })
const progressText = ref('')

const canAutoInstall = computed(() => Boolean(window.aiReportDesktop?.installUpdate))
const statusLabel = computed(() => {
  if (checking.value) return '检查中'
  if (updateError.value) return '检查失败'
  if (update.value?.has_update) return '有新版本'
  if (update.value) return '已是最新'
  return '未检查'
})
const statusTagType = computed(() => {
  if (updateError.value) return 'warning'
  if (update.value?.has_update) return 'success'
  if (update.value) return 'info'
  return 'info'
})
const publishedText = computed(() => {
  if (!update.value?.published_at) return '版本信息来自 GitHub Releases'
  return `发布于 ${String(update.value.published_at).replace('T', ' ').replace('Z', '').slice(0, 16)}`
})
const assetHint = computed(() => {
  const asset = update.value?.recommended
  if (!asset) return ''
  const size = asset.size ? `${(asset.size / 1024 / 1024).toFixed(1)} MB` : ''
  return `安装包：${asset.name}${size ? `（${size}）` : ''}`
})
const progressPercent = computed(() => {
  if (!progress.value.total) return 0
  return Math.min(100, Math.round((progress.value.received / progress.value.total) * 100))
})
const repoUrl = `https://github.com/${about.value.update_repo || 'lhc75/AI-report-generation'}/releases`
const repoText = computed(() => about.value.update_repo || 'lhc75/AI-report-generation')

let stopProgress = null
let stopOpenAbout = null

function displayVersion(value) {
  const text = String(value || '').trim()
  if (!text || text === '0.0.0') return '开发版'
  return text.startsWith('v') ? text : `v${text}`
}

function platformLabel(platform) {
  return { mac: 'macOS', win: 'Windows', linux: 'Linux' }[platform] || platform
}

function openRelease(asset) {
  const url = asset?.url || update.value?.release_url || repoUrl
  window.open(url, '_blank')
}

async function load() {
  installed.value = false
  installMessage.value = ''
  progress.value = { received: 0, total: 0 }
  progressText.value = ''
  if (!about.value.version) {
    try {
      about.value = await getAbout()
    } catch (_) {
      about.value = {}
    }
  }
  await refresh(false)
}

async function refresh(force) {
  checking.value = true
  updateError.value = ''
  try {
    const data = await checkUpdate(force)
    if (data?.ok) {
      update.value = data
      emit('update-available', data.has_update)
    } else {
      update.value = null
      updateError.value = data?.error || '无法获取版本信息'
    }
  } catch (error) {
    update.value = null
    updateError.value = error?.message || '无法获取版本信息'
  } finally {
    checking.value = false
  }
}

async function install() {
  const asset = update.value?.recommended
  if (!asset || !window.aiReportDesktop?.installUpdate) return
  installing.value = true
  installMessage.value = ''
  try {
    const result = await window.aiReportDesktop.installUpdate(asset)
    installed.value = true
    installMessage.value = result?.message || '安装程序已启动'
    if (result?.mode !== 'auto') installing.value = false
  } catch (error) {
    installing.value = false
    ElMessage.error(error?.message || '安装更新失败')
  }
}

function handleProgress(payload) {
  if (!payload) return
  if (payload.phase === 'downloading') {
    progress.value = { received: payload.received || 0, total: payload.total || 0 }
    progressText.value = payload.total
      ? `正在下载更新包… ${progressPercent.value}%`
      : `正在下载更新包… ${((payload.received || 0) / 1024 / 1024).toFixed(1)} MB`
  } else if (payload.phase === 'installing') {
    progressText.value = '下载完成，正在准备安装…'
  }
}

onMounted(() => {
  stopProgress = window.aiReportDesktop?.onUpdateProgress?.(handleProgress)
  stopOpenAbout = window.aiReportDesktop?.onOpenAbout?.(() => emit('update:modelValue', true))
})

onUnmounted(() => {
  stopProgress?.()
  stopOpenAbout?.()
})
</script>

<style scoped>
.about-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.app-name { font-size: 18px; font-weight: 600; }
.app-version { margin-top: 6px; color: #606266; font-size: 13px; }
.muted { color: #909399; font-size: 12px; }
.block { margin: 12px 0; }
.checking { display: flex; align-items: center; gap: 8px; color: #606266; font-size: 13px; }
.notes-wrap {
  margin-top: 12px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  overflow: hidden;
}
.notes-title {
  padding: 8px 12px;
  background: #f5f7fa;
  font-size: 13px;
  font-weight: 600;
}
.notes {
  margin: 0;
  padding: 10px 12px;
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.6;
  color: #606266;
}
.actions { display: flex; align-items: center; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
.asset-hint { margin-left: 4px; }
.install-message { margin-top: 10px; color: #67c23a; font-size: 13px; }
.about-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 18px;
  padding-top: 12px;
  border-top: 1px solid #f0f2f5;
}
.foot-actions { display: flex; gap: 8px; }
</style>
