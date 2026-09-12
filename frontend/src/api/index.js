// 后端 API 统一封装。
import { getToken, http, readSse, safeText, setToken } from './http'

/**
 * 解析 SSE 接口错误响应的可读文案。
 * 优先取 JSON 的 detail 字段（FastAPI），否则回退原始文本。
 * 统一前端对后端错误结构的解析，避免把 {"detail": ...} 原文直接抛给用户（bug #29）。
 */
async function sseErrorText(res) {
  try {
    const j = await res.json()
    const d = j?.detail
    if (typeof d === 'string') return d
    return Array.isArray(d) ? d.map((x) => x.msg).join('；') : JSON.stringify(j)
  } catch (e) {
    /* fallthrough */
  }
  return safeText(res)
}

// ---- 认证 ----
export const authApi = {
  register: (data) => http.post('/api/auth/register', data),
  login: (data) => http.post('/api/auth/login', data),
  logout: () => http.post('/api/auth/logout'),
  me: () => http.get('/api/auth/me'),
  updateMe: (data) => http.put('/api/auth/me', data),
  // 语音 WS 一次性连接票据：长效令牌不出 Bearer 头，URL 只带短时票据（bug #23）
  createWsTicket: () => http.post('/api/auth/ws-ticket'),
}

// ---- 会话 / 聊天 ----
export const sessionApi = {
  start: (data) => http.post('/api/session/start', data),
  get: () => http.get('/api/session'),
  reset: () => http.post('/api/session/reset'),
  history: () => http.get('/api/session/history'),
}

/**
 * SSE 流式聊天。
 * @param {string} message
 * @param {{signal?: AbortSignal, onDelta?: (s:string)=>void, onDone?: (ev:object)=>void, onError?: (s:string)=>void}} handlers
 */
export async function chatStream(message, handlers = {}) {
  let res
  try {
    res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getToken()}` },
      body: JSON.stringify({ message }),
      signal: handlers.signal,
    })
  } catch (e) {
    if (e?.name === 'AbortError') return // 主动取消：静默
    // fetch 本身失败（断网/后端不可达）也必须进 onError，否则上层 sending 永久卡死
    handlers.onError?.('网络异常，请检查连接后重试。')
    return
  }
  if (!res.ok || !res.body) {
    // 401：与 REST 拦截器一致，清令牌并跳登录（bug #2），携带来源页便于回跳（bug #28）
    if (res.status === 401) {
      setToken('')
      const p = window.location.pathname
      if (!p.startsWith('/login')) {
        window.location.href = `/login?redirect=${encodeURIComponent(p + window.location.search)}`
      }
      return
    }
    handlers.onError?.(await sseErrorText(res))
    return
  }
  await readSse(
    res.body.getReader(),
    (ev) => {
      if (ev.type === 'delta') handlers.onDelta?.(ev.content)
      else if (ev.type === 'done') handlers.onDone?.(ev)
      else if (ev.type === 'error') handlers.onError?.(ev.message)
    },
    () => {},
    (e) => {
      if (e?.name === 'AbortError') return
      handlers.onError?.(String(e))
    },
  )
}

// ---- 题库 / 收藏 ----
export const bankApi = {
  browse: (params) => http.get('/api/questions', { params }),
  meta: () => http.get('/api/questions/meta'),
  add: (data) => http.post('/api/questions', data),
  importCsv: (content) => http.post('/api/questions/import', { content }),
  favoriteIds: () => http.get('/api/favorites'),
  addFavorite: (qid) => http.post(`/api/favorites/${qid}`),
  removeFavorite: (qid) => http.delete(`/api/favorites/${qid}`),
}

// ---- 定制面试 ----
export const customApi = {
  /** SSE 生成定制面试题 */
  generate(jobTitle, jd, handlers = {}) {
    return (async () => {
      let res
      try {
        res = await fetch('/api/custom/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getToken()}` },
          body: JSON.stringify({ job_title: jobTitle, jd }),
          signal: handlers.signal,
        })
      } catch (e) {
        if (e?.name === 'AbortError') return
        handlers.onError?.('网络异常，请检查连接后重试。')
        return
      }
      if (!res.ok || !res.body) {
        // 401：与 REST 拦截器一致，清令牌并跳登录（bug #2），携带来源页便于回跳（bug #28）
        if (res.status === 401) {
          setToken('')
          const p = window.location.pathname
          if (!p.startsWith('/login')) {
            window.location.href = `/login?redirect=${encodeURIComponent(p + window.location.search)}`
          }
          return
        }
        handlers.onError?.(await sseErrorText(res))
        return
      }
      await readSse(
        res.body.getReader(),
        (ev) => {
          if (ev.type === 'progress') handlers.onProgress?.(ev.message)
          else if (ev.type === 'done') handlers.onDone?.(ev)
          else if (ev.type === 'error') handlers.onError?.(ev.message)
        },
        () => {},
        (e) => {
          if (e?.name === 'AbortError') return
          handlers.onError?.(String(e))
        },
      )
    })()
  },
  status: () => http.get('/api/custom/status'),
  clear: () => http.delete('/api/custom'),
}

// ---- 语音页运行时配置 ----
export const configApi = {
  voice: () => http.get('/api/config/voice'),
}
