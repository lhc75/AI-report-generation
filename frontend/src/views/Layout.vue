<template>
  <el-container class="layout">
    <el-header class="header">
      <div class="brand">后台管理系统</div>
      <el-menu mode="horizontal" :default-active="$route.path" router class="menu" :ellipsis="false">
        <el-menu-item index="/projects">我的项目</el-menu-item>
        <!-- 暂时关闭数据分析入口 -->
        <!-- <el-menu-item index="/statistics">数据统计</el-menu-item> -->
        <el-menu-item index="/ai-config">AI 配置</el-menu-item>
        <el-menu-item index="/skills">技能库</el-menu-item>
      </el-menu>
      <div class="user">
        <el-badge is-dot :hidden="!updateAvailable" class="about-badge">
          <el-button text class="about-link" @click="aboutVisible = true">关于</el-button>
        </el-badge>
        <span>{{ username }}</span>
        <el-button text type="danger" @click="logout">退出</el-button>
      </div>
      <AboutDialog v-model="aboutVisible" @update-available="onUpdateAvailable" />
    </el-header>
    <el-main class="main">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElNotification } from 'element-plus'
import AboutDialog from '../components/AboutDialog.vue'
import { checkUpdate } from '../api'

const router = useRouter()
const username = computed(() => localStorage.getItem('username') || '')
const aboutVisible = ref(false)
const updateAvailable = ref(false)
const UPDATE_POLL_MS = 6 * 60 * 60 * 1000

let pollTimer = null
let stopOpenAbout = null

function onUpdateAvailable(value) {
  updateAvailable.value = Boolean(value)
}

async function pollUpdate() {
  try {
    const data = await checkUpdate(false)
    if (!data?.ok) return
    updateAvailable.value = Boolean(data.has_update)
    const notified = localStorage.getItem('update-notified-version')
    if (data.has_update && notified !== data.latest_version) {
      localStorage.setItem('update-notified-version', data.latest_version)
      ElNotification({
        title: '发现新版本',
        message: `v${data.latest_version} 已发布，点击右上角“关于”查看并安装。`,
        type: 'success',
        duration: 8000,
      })
    }
  } catch (_) {
    // 检查更新失败不打扰使用；关于弹窗里会展示具体错误。
  }
}

onMounted(() => {
  pollUpdate()
  pollTimer = setInterval(pollUpdate, UPDATE_POLL_MS)
  stopOpenAbout = window.aiReportDesktop?.onOpenAbout?.(() => { aboutVisible.value = true })
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  stopOpenAbout?.()
})

function logout() {
  localStorage.removeItem('token')
  localStorage.removeItem('username')
  router.push('/login')
}
</script>

<style scoped>
.layout { min-height: 100vh; background: #f5f7fa; }
.header {
  display: flex;
  align-items: center;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  padding: 0 24px;
}
.brand { font-size: 17px; font-weight: 600; margin-right: 40px; white-space: nowrap; }
.menu { flex: 1; border-bottom: none; }
.user { display: flex; align-items: center; gap: 8px; }
.about-link { color: #606266; font-weight: 400; }
.about-badge { display: inline-flex; align-items: center; }
.main { padding: 20px 24px; }
</style>

