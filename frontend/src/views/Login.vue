<template>
  <div class="login-page">
    <div class="login-panel">
      <h2 class="title">后台管理系统</h2>
      <el-tabs v-model="mode" stretch>
        <el-tab-pane label="登录" name="login" />
        <el-tab-pane label="注册" name="register" />
      </el-tabs>
      <el-form @submit.prevent>
        <el-form-item>
          <el-input v-model="username" placeholder="用户名" :prefix-icon="User" size="large" />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="password"
            type="password"
            placeholder="密码（至少6位）"
            show-password
            :prefix-icon="Lock"
            size="large"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button type="primary" size="large" style="width: 100%" :loading="loading" @click="submit">
          {{ mode === 'login' ? '登 录' : '注 册' }}
        </el-button>
      </el-form>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import api from '../api'

const router = useRouter()
const mode = ref('login')
const username = ref('')
const password = ref('')
const loading = ref(false)

async function submit() {
  if (!username.value || password.value.length < 6) {
    ElMessage.warning('请输入用户名和至少6位密码')
    return
  }
  loading.value = true
  try {
    const { data } = await api.post(`/auth/${mode.value}`, {
      username: username.value,
      password: password.value,
    })
    localStorage.setItem('token', data.token)
    localStorage.setItem('username', data.username)
    router.push('/projects')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f2f4f8;
}
.login-panel {
  width: 380px;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 28px 32px 32px;
}
.title {
  text-align: center;
  margin: 0 0 16px;
  font-size: 20px;
  color: #1f2d3d;
}
</style>
