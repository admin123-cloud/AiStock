import axios from 'axios'
import { ElMessage } from 'element-plus'

const request = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

request.interceptors.request.use(
  config => config,
  error => Promise.reject(error)
)

request.interceptors.response.use(
  response => response.data,
  error => {
    if (error?.config?.skipErrorHandler) {
      return Promise.reject(error)
    }
    const message = error.response?.data?.message || error.message || '请求失败'
    ElMessage.error(message)
    console.error('响应错误:', error)
    return Promise.reject(error)
  }
)

export default request
