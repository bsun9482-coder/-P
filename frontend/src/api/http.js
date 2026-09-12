// HTTP 客户端：统一注入 Bearer 令牌，401 自动跳登录；提供 SSE 流式解析工具。
import axios from 'axios'

export const TOKEN_KEY = 'xiaop_token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}
export function setToken(t) {
  if (t) localStorage.setItem(TOKEN_KEY, t)
  else localStorage.removeItem(TOKEN_KEY)
}

export const http = axios.create({
  baseURL: '',
  // 数组查询参数序列化为 tags=a&tags=b（与 FastAPI list Query 契约匹配）。
  // axios 默认输出 tags[]=a，键名不匹配会导致后端收到 None、筛选被静默忽略。
  paramsSerializer: { indexes: null },
})

http.interceptors.request.use((cfg) => {
  const t = getToken()
  if (t) cfg.headers.Authorization = `Bearer ${t}`
  return cfg
})

http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    if (err.response && err.response.status === 401) {
      setToken('')
      const p = window.location.pathname
      if (!p.startsWith('/login')) {
        // 携带来源页，登录后回到原页面（bug #28，与路由守卫行为一致）
        window.location.href = `/login?redirect=${encodeURIComponent(p + window.location.search)}`
      }
    }
    return Promise.reject(err)
  },
)

/**
 * 读取 SSE 流（fetch ReadableStream），逐条解析 `data: {...}` 事件。
 * @param {ReadableStreamDefaultReader} reader
 * @param {(ev: object) => void} onEvent  每条 data 事件
 * @param {() => void} onDone             流结束
 * @param {(err: Error) => void} onError
 */
export function readSse(reader, onEvent, onDone, onError) {
  const decoder = new TextDecoder()
  let buffer = ''

  function handleLines(lines) {
    for (const line of lines) {
      if (line.startsWith('data:')) {
        const payload = line.slice(5).trim()
        if (!payload) continue
        try {
          onEvent(JSON.parse(payload))
        } catch (e) {
          /* 忽略非 JSON 行 */
        }
      }
    }
  }

  function pump() {
    return reader.read().then(({ done, value }) => {
      if (done) {
        onDone()
        return
      }
      buffer += decoder.decode(value, { stream: true })
      const parts = buffer.split('\n\n')
      buffer = parts.pop() || ''
      handleLines(parts)
      return pump()
    })
  }

  return pump().catch((e) => onError && onError(e))
}

export async function safeText(res) {
  try {
    return await res.text()
  } catch (e) {
    return String(e)
  }
}
