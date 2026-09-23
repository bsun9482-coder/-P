// 聊天会话状态：历史、模式、流式发送、定制面试生成。
import { markRaw } from 'vue'
import { defineStore } from 'pinia'
import { chatStream, customApi, sessionApi } from '../api'

export const useChatStore = defineStore('chat', {
  state: () => ({
    active: false,
    mode: '',
    history: [], // [{role:'assistant'|'user', content, streaming}]
    finished: false,
    report: null,
    reportData: null,
    persona: '',
    sending: false,
    voiceReady: false,
    customJobTitle: '',
    // 会话代际与流取消：reset/start/load 重建会话后，旧 SSE 流的回调必须失效，
    // 否则旧流的 delta/报告会污染新会话。AbortController 用 markRaw
    // 避免 Pinia 把实例做响应式代理。
    gen: 0,
    abort: null,
  }),
  actions: {
    _abortStream() {
      if (this.abort) {
        try {
          this.abort.abort()
        } catch (e) {
          /* 忽略 */
        }
        this.abort = null
      }
      this.gen++
    },
    _setHistory(history) {
      this.history = (history || []).map(([role, content]) => ({
        role,
        content,
        streaming: false,
      }))
    },
    async load() {
      this._abortStream()
      const s = await sessionApi.get()
      this.active = s.active
      this.mode = s.mode
      this.finished = s.finished
      this.report = s.report
      this.reportData = s.report_data || null
      this.persona = s.persona || ''
      this._setHistory(s.history)
    },
    async start(body) {
      this._abortStream()
      const r = await sessionApi.start(body)
      this.active = true
      this.mode = r.mode
      this.finished = false
      this.report = null
      this.reportData = null
      this._setHistory(r.history)
    },
    async reset() {
      this._abortStream()
      await sessionApi.reset()
      this.$reset()
    },
    /** 发送一条消息（SSE 流式），返回 Promise（done 事件或错误）。 */
    send(message) {
      return new Promise((resolve) => {
        // 新流开始前取消上一条未完成流；代际失效旧回调
        this._abortStream()
        const gen = this.gen
        const ac = markRaw(new AbortController())
        this.abort = ac
        const stale = () => gen !== this.gen
        this.history.push({ role: 'user', content: message, streaming: false })
        this.history.push({ role: 'assistant', content: '', streaming: true })
        this.sending = true // 进入即置位：防止 await 前快速双击产生双流
        chatStream(message, {
          signal: ac.signal,
          onDelta: (d) => {
            if (stale()) return
            const last = this.history[this.history.length - 1]
            last.content += d
          },
          onDone: (ev) => {
            if (stale()) return
            this.abort = null
            const last = this.history[this.history.length - 1]
            last.streaming = false
            this.sending = false
            this.finished = !!ev.finished
            this.mode = ev.mode
            if (ev.finished) {
              this.report = ev.report
              this.reportData = ev.report_data || null
            }
            resolve(ev)
          },
          onError: (msg) => {
            if (stale()) return
            this.abort = null
            const last = this.history[this.history.length - 1]
            last.content = last.content || '小P暂时无法回答，请稍后重试。'
            last.streaming = false
            this.sending = false
            resolve({ error: msg })
          },
        })
      })
    },
    /** 生成定制面试（SSE 进度），完成后进入该会话。 */
    async startCustom(jobTitle, jd, handlers = {}) {
      this._abortStream()
      const gen = this.gen
      const stale = () => gen !== this.gen
      await customApi.generate(jobTitle, jd, {
        onProgress: (msg) => {
          if (!stale()) handlers.onProgress?.(msg)
        },
        onError: (msg) => {
          if (!stale()) handlers.onError?.(msg)
        },
        onDone: (ev) => {
          if (stale()) return
          this.active = true
          this.mode = ev.mode
          this.finished = false
          this.report = null
          this.reportData = null
          this._setHistory(ev.history)
          this.voiceReady = !!ev.custom_voice_ready
          this.customJobTitle = ev.job_title || ''
          handlers.onDone?.(ev)
        },
      })
    },
    async refreshVoiceStatus() {
      try {
        const s = await customApi.status()
        this.voiceReady = !!s.ready
        this.customJobTitle = s.job_title || ''
      } catch (e) {
        /* 忽略 */
      }
    },
    async clearVoice() {
      await customApi.clear()
      this.voiceReady = false
      this.customJobTitle = ''
    },
  },
})
