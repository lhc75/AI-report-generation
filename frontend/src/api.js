import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from './router'

const api = axios.create({ baseURL: '/api', timeout: 60000 })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (resp) => resp,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      router.push('/login')
    }
    if (!err.config?.silentError) {
      const detail = err.response?.data?.detail
      const message = Array.isArray(detail) ? detail.map((item) => item.msg).filter(Boolean).join('；') : detail
      ElMessage.error(typeof message === 'string' && message ? message : err.message || '请求失败')
    }
    return Promise.reject(err)
  }
)

export default api
