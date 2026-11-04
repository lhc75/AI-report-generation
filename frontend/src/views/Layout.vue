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
        <span>{{ username }}</span>
        <el-button text type="danger" @click="logout">退出</el-button>
      </div>
    </el-header>
    <el-main class="main">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const username = computed(() => localStorage.getItem('username') || '')

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
.main { padding: 20px 24px; }
</style>

