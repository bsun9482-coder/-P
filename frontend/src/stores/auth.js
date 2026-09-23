// 认证状态：令牌持久化到 localStorage，401 时由 http 拦截器统一处理。
import { defineStore } from 'pinia'
import { authApi } from '../api'
import { getToken, setToken } from '../api/http'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null,
    token: getToken(),
  }),
  getters: {
    isLoggedIn: (s) => !!s.token,
    nickname: (s) => s.user?.nickname || s.user?.username || '面试者',
    persona: (s) => s.user?.persona || '',
  },
  actions: {
    async login(username, password) {
      const { token, user } = await authApi.login({ username, password })
      this.token = token
      setToken(token)
      this.user = user
      return user
    },
    async register(data) {
      const { token, user } = await authApi.register(data)
      this.token = token
      setToken(token)
      this.user = user
      return user
    },
    async fetchMe() {
      if (!this.token) return null
      try {
        this.user = await authApi.me()
        return this.user
      } catch (e) {
        // 仅 401（令牌确实失效）才清除本地凭据；网络抖动/5xx 保留 token，
        // 避免后端瞬时不可达就把用户登出
        if (e?.response?.status === 401) this.clearLocal()
        return null
      }
    },
    async updateMe(data) {
      this.user = await authApi.updateMe(data)
      return this.user
    },
    async logout() {
      try {
        await authApi.logout()
      } catch (e) {
        /* 忽略 */
      }
      this.clearLocal()
    },
    clearLocal() {
      this.user = null
      this.token = ''
      setToken('')
    },
  },
})
