// 语音通话引擎（从原 voice_page.html 移植为 Vue3 composable）。
//
// 职责：WebSocket 协议处理、ASR 音频流推送、播报播放队列（按 sid 有序零间隙）、
// 开口即打断（ASR 内容级回声过滤 + 手动打断）、edge-tts 失败降级本地语音。
// 音色/VAD 等运行时配置来自后端 /api/config/voice。
//
// 结构约定：
// - 状态机：V.phase 是通话阶段单一事实来源（idle/connecting/listening/speaking），
//   V.speaking 是其派生镜像（高频路径直接读）；
// - 消息分发：MSG_HANDLERS 注册表按 type 分发，不再 if-else 链；
// - 音频采集：AudioWorklet 优先（ScriptProcessor 已废弃），失败回退 ScriptProcessor；
//   上行音频为二进制帧（Int16 LE PCM），控制消息仍为 JSON 文本帧。
import { onUnmounted, reactive } from 'vue'
import { authApi, configApi, customApi } from '../../api'
import { MOCK_START_RE, base64ToBytes, calcRms, echoMatch, normalizeForEcho } from './voiceUtils'
import workletUrl from './pcm-worklet.js?url'

const DEBUG_DEFAULT = false

// 通话阶段：单一事实来源
const PHASE = {
  IDLE: 'idle', // 未接通
  CONNECTING: 'connecting', // 接通中 / 等待回复
  LISTENING: 'listening', // 聆听用户
  SPEAKING: 'speaking', // 播报中（可被打断）
}

export function useVoiceCall() {
  // ---------- UI 状态（响应式） ----------
  const ui = reactive({
    active: false,
    statusText: '未连接 · 点击下方按钮接通',
    statusBusy: false,
    statusInterruptible: false,
    waveOn: false,
    glowOn: false,
    micOn: false,
    micLevel: 0,
    timerText: '00:00',
    mode: '辅导答疑',
    transcript: [],
    debugOn: DEBUG_DEFAULT,
    debugLog: '',
    voiceReady: false,
    customJobTitle: '',
  })

  // ---------- 引擎状态（非响应式，音频帧高频写入） ----------
  const V = {
    ws: null,
    buf: '',
    phase: PHASE.IDLE,
    speaking: false, // phase 的派生镜像（高频路径直接读）
    audioCtx: null,
    playingAudio: false,
    replyEnded: false,
    suppressAudio: false,
    sidData: {},
    playQueue: [],
    activeSources: [],
    nextPlaySid: 0,
    playTime: 0,
    pendingSources: 0,
    skippedSids: {},
    fallbackBySid: {},
    fallbackQueue: [],
    liveBubbleIndex: -1,
    timerStart: 0,
    timerInt: null,
    fallbackActive: 0,
    lastSend: 0,
    lastSendText: '',
    mic: null,
    micProc: null,
    asrReady: false,
    lastAsrEvent: 0,
    vadAvg: 0,
    speakingText: '',
    prevSpeechText: '',
    echoUntil: 0,
    vadHits: 0,
    micLevel: 0,
    voicesWaiting: false,
  }

  // VAD 配置（后端下发，替代原 HTML 模板替换）
  const VAD = { threshold: 0.08, hits: 5, quietFrames: 3, noiseMargin: 1.6 }

  // ---------- 状态机 ----------
  function setPhase(p) {
    V.phase = p
    V.speaking = p === PHASE.SPEAKING
    setSpeakingUI(V.speaking)
    if (V.speaking) {
      // 每次进入播报：重置 VAD 判定，避免上一轮残留误触发打断
      V.vadAvg = 0
      V.vadHits = 0
    }
  }

  // ---------- 调试面板 ----------
  function dbg(msg) {
    if (!ui.debugOn) return
    const d = new Date()
    const ts =
      ('0' + d.getHours()).slice(-2) +
      ':' +
      ('0' + d.getMinutes()).slice(-2) +
      ':' +
      ('0' + d.getSeconds()).slice(-2)
    const head =
      'phase:' + V.phase +
      ' mic:' + (V.mic ? 'Y' : 'N') +
      ' asr:' + (V.asrReady ? 'Y' : 'N') +
      ' lv:' + Math.round((V.micLevel || 0) * 260)
    ui.debugLog = head + '\n' + (ui.debugLog + '\n' + ts + ' ' + msg).split('\n').slice(-16).join('\n')
  }

  // ---------- 状态/UI ----------
  function setStatus(t, busy) {
    ui.statusText = t
    ui.statusBusy = !!busy
  }

  function setSpeakingUI(on) {
    ui.waveOn = on
    ui.glowOn = on
    ui.statusInterruptible = on // 播报中点击状态条 = 手动打断
  }

  function updateTimer() {
    if (!ui.active) return
    const s = Math.max(0, Math.floor((Date.now() - V.timerStart) / 1000))
    const m = Math.floor(s / 60)
    ui.timerText = ('0' + m).slice(-2) + ':' + ('0' + (s % 60)).slice(-2)
  }

  function addBubble(role, text) {
    ui.transcript.push({ role, text })
    if (role === 'assistant') V.liveBubbleIndex = ui.transcript.length - 1
    scrollTranscript()
    return ui.transcript.length - 1
  }

  function updateLive(text) {
    if (V.liveBubbleIndex < 0) V.liveBubbleIndex = addBubble('assistant', '')
    ui.transcript[V.liveBubbleIndex].text = text
    scrollTranscript()
  }

  function scrollTranscript() {
    // 交由视图层处理（通过 nextTick），这里不做 DOM
  }

  function setMode(m) {
    ui.mode = m
  }

  async function fetchCustomStatus() {
    try {
      const data = await customApi.status()
      ui.voiceReady = !!data.ready
      ui.customJobTitle = data.job_title || ''
      if (data.ready) {
        setMode('定制面试')
        const hint = ui.transcript.find((b) => b.role === 'hint')
        if (hint) hint.text = `已为你准备好「${data.job_title || '自定义岗位'}」定制面试，接通后小P会直接开始。`
      }
    } catch (e) {
      /* 忽略 */
    }
  }

  // ---------- 音频上下文 / 麦克风 ----------
  function ensureAudio() {
    if (!V.audioCtx) {
      try {
        const AC = window.AudioContext || window.webkitAudioContext
        V.audioCtx = new AC()
      } catch (e) {
        /* 忽略 */
      }
    }
  }

  // 采集回调（AudioWorklet / ScriptProcessor 共用）：
  // 电平表 → VAD 打断检测 → 转 Int16 PCM → 二进制帧上行
  function handleAudioChunk(f32) {
    const rms = calcRms(f32)
    V.micLevel = rms
    updateMicMeter(rms)
    if (V.speaking) vadCheck(rms)
    if (!V.ws || V.ws.readyState !== 1 || !V.asrReady) return
    const pcm = new Int16Array(f32.length)
    for (let i = 0; i < f32.length; i++) {
      const s = Math.max(-1, Math.min(1, f32[i]))
      pcm[i] = (s < 0 ? s * 0x8000 : s * 0x7fff) | 0
    }
    V.ws.send(pcm.buffer) // 二进制帧：Int16 LE PCM（省 base64 ~33% 开销）
  }

  async function startAudioStream() {
    if (V.mic) return
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus('请用 Chrome / Edge 浏览器')
      return
    }
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
    } catch (e) {
      setStatus('麦克风不可用: ' + ((e && e.name) || e), true)
      return
    }
    const AC = window.AudioContext || window.webkitAudioContext
    const ctx = V.audioCtx || new AC()
    if (ctx.state === 'suspended') {
      try {
        ctx.resume()
      } catch (e) {
        /* 忽略 */
      }
    }
    V.audioCtx = ctx
    const src = ctx.createMediaStreamSource(stream)
    const g = ctx.createGain()
    g.gain.value = 0 // 静音输出，避免把采集声音播出来
    g.connect(ctx.destination)
    // AudioWorklet 优先（ScriptProcessor 是已废弃 API，主线程跑会卡顿），
    // 不支持/注册失败时回退 ScriptProcessor
    let viaWorklet = false
    if (ctx.audioWorklet) {
      try {
        await ctx.audioWorklet.addModule(workletUrl)
        const node = new AudioWorkletNode(ctx, 'pcm-capture', { numberOfOutputs: 0 })
        node.port.onmessage = (e) => handleAudioChunk(e.data)
        src.connect(node)
        V.micProc = node
        viaWorklet = true
      } catch (e) {
        dbg('AudioWorklet 注册失败，回退 ScriptProcessor: ' + e)
      }
    }
    if (!viaWorklet) {
      const proc = ctx.createScriptProcessor(4096, 1, 1)
      proc.onaudioprocess = (e) => handleAudioChunk(e.inputBuffer.getChannelData(0))
      proc.connect(g)
      V.micProc = proc
    }
    V.mic = stream
    ui.micOn = true
    if (V.ws && V.ws.readyState === 1) {
      V.ws.send(JSON.stringify({ type: 'asr_start', sample_rate: ctx.sampleRate }))
      dbg('音频采集启动 sampleRate=' + ctx.sampleRate + (viaWorklet ? ' (worklet)' : ' (legacy)'))
    }
  }

  function updateMicMeter(rms) {
    const lv = Math.min(100, Math.round(rms * 260))
    ui.micLevel = lv
  }

  function stopAudioStream() {
    if (V.micProc) {
      try {
        V.micProc.disconnect()
        if (V.micProc.port) V.micProc.port.onmessage = null
      } catch (e) {
        /* 忽略 */
      }
      V.micProc = null
    }
    if (V.mic) {
      try {
        V.mic.getTracks().forEach((t) => t.stop())
      } catch (e) {
        /* 忽略 */
      }
      V.mic = null
    }
    V.asrReady = false
    V.lastAsrEvent = 0
    ui.micOn = false
    ui.micLevel = 0
  }

  // 音量打断（兜底）：ASR 静默超 1.5s 时启用（实际由内容级打断承担主防线）
  function vadCheck(rms) {
    if (V.lastAsrEvent && Date.now() - V.lastAsrEvent < 1500) return
    V.vadAvg = (V.vadAvg > 0 ? V.vadAvg : rms) * 0.995 + rms * 0.005
    const thr = Math.max(VAD.threshold, V.vadAvg * VAD.noiseMargin)
    if (rms > thr) V.vadHits++
    else V.vadHits = 0
    if (V.vadHits >= VAD.hits) {
      V.vadHits = 0
      dbg('音量打断 rms=' + rms.toFixed(3) + ' thr=' + thr.toFixed(3))
      bargeIn()
    }
  }

  // ---------- 发送 / ASR 处理 ----------
  function sendText(t) {
    if (!V.ws || V.ws.readyState !== 1) return
    const now = Date.now()
    if (V.lastSendText === t && now - V.lastSend < 2000) return
    V.lastSend = now
    V.lastSendText = t
    dbg('sendText: ' + t.slice(0, 20))
    stopAudio()
    setPhase(PHASE.CONNECTING)
    V.echoUntil = Date.now() + 2500
    V.replyEnded = false
    V.buf = ''
    V.liveBubbleIndex = -1
    addBubble('user', t)
    if (MOCK_START_RE.test(t)) setMode('模拟面试')
    V.ws.send(JSON.stringify({ type: 'text', content: t }))
    setStatus('小P思考中…', true)
  }

  function handleAsrText(text) {
    text = (text || '').trim()
    if (!text) return
    V.lastAsrEvent = Date.now()
    dbg('ASR句子: ' + text.slice(0, 16))
    if (V.prevSpeechText && Date.now() >= V.echoUntil) V.prevSpeechText = ''
    if (V.speaking || Date.now() < V.echoUntil) {
      if (isEchoLike(text)) {
        dbg('回声忽略(整句): ' + text.slice(0, 20))
        return
      }
      dbg('插话识别: ' + text.slice(0, 20))
    }
    sendText(text)
  }

  function isEchoLike(text) {
    const t = normalizeForEcho(text)
    if (!t) return false
    const b = normalizeForEcho(V.speakingText)
    if (echoMatch(t, b)) return true
    if (V.prevSpeechText && Date.now() < V.echoUntil) {
      const pb = normalizeForEcho(V.prevSpeechText)
      if (echoMatch(t, pb)) return true
    }
    return false
  }

  // ---------- 音频播放队列（按 sid 有序、零间隙） ----------
  function onAudioStart(sid, text) {
    if (V.suppressAudio) return
    V.replyEnded = false
    V.fallbackBySid[sid] = text || ''
    if (V.nextPlaySid === 0) V.nextPlaySid = sid
    if (!V.speaking) {
      dbg('播报开始: ' + (text || '').slice(0, 22))
      setPhase(PHASE.SPEAKING)
      setStatus('播报中…', true)
    } else {
      V.vadAvg = 0
      V.vadHits = 0
    }
  }

  function onAudioFrame(sid, b64) {
    if (V.suppressAudio) return
    ensureAudio()
    if (!V.audioCtx) return
    const bytes = base64ToBytes(b64)
    const prev = V.sidData[sid]
    const merged = new Uint8Array((prev ? prev.length : 0) + bytes.length)
    if (prev) merged.set(prev, 0)
    merged.set(bytes, prev ? prev.length : 0)
    V.sidData[sid] = merged
  }

  function onAudioEnd(sid) {
    const bytes = V.sidData[sid]
    delete V.sidData[sid]
    delete V.fallbackBySid[sid]
    if (!bytes || !bytes.length) {
      if (!V.suppressAudio) {
        V.skippedSids[sid] = true
        scheduleReady()
      }
      return
    }
    ensureAudio()
    V.audioCtx.decodeAudioData(
      bytes.buffer.slice(0),
      (buf) => {
        if (V.suppressAudio) return
        V.playQueue.push({ sid, buf })
        V.playQueue.sort((a, b) => a.sid - b.sid)
        scheduleReady()
      },
      () => {
        if (!V.suppressAudio) {
          V.skippedSids[sid] = true
          scheduleReady()
        }
      },
    )
  }

  function scheduleReady() {
    if (V.suppressAudio) return
    if (V.fallbackActive > 0) return
    while (true) {
      if (V.skippedSids[V.nextPlaySid]) {
        delete V.skippedSids[V.nextPlaySid]
        V.nextPlaySid++
        continue
      }
      if (!V.playQueue.length || V.playQueue[0].sid !== V.nextPlaySid) break
      const item = V.playQueue.shift()
      playBuffer(item.buf)
      V.nextPlaySid++
    }
  }

  function playBuffer(buf) {
    const src = V.audioCtx.createBufferSource()
    src.buffer = buf
    src.connect(V.audioCtx.destination)
    const now = V.audioCtx.currentTime
    const t = Math.max(now + 0.02, V.playTime)
    src.start(t)
    V.activeSources.push(src)
    V.playTime = t + buf.duration
    V.pendingSources++
    V.playingAudio = true
    src.onended = () => {
      const i = V.activeSources.indexOf(src)
      if (i >= 0) V.activeSources.splice(i, 1)
      if (V.pendingSources > 0) V.pendingSources--
      if (V.pendingSources <= 0) {
        V.playingAudio = false
        audioDone()
      }
    }
  }

  function audioDone() {
    if (V.suppressAudio) return
    if (V.pendingSources > 0 || V.fallbackActive > 0) return
    if (V.fallbackQueue.length) {
      drainFallback()
      return
    }
    dbg('播报结束')
    setPhase(PHASE.LISTENING)
    V.echoUntil = Date.now() + 2500
    if (V.replyEnded && ui.active) setStatus('聆听中…')
  }

  function stopAudio() {
    V.playingAudio = false
    V.suppressAudio = true
    V.pendingSources = 0
    V.playQueue = []
    V.sidData = {}
    V.nextPlaySid = 0
    V.playTime = 0
    V.skippedSids = {}
    V.fallbackActive = 0
    V.fallbackBySid = {}
    V.fallbackQueue = []
    V.vadHits = 0
    V.vadAvg = 0
    setSpeakingUI(false)
    while (V.activeSources.length) {
      const s = V.activeSources.pop()
      try {
        s.stop()
        s.disconnect()
      } catch (e) {
        /* 忽略 */
      }
    }
    try {
      window.speechSynthesis.cancel()
    } catch (e) {
      /* 忽略 */
    }
  }

  // ---------- 打断 ----------
  function bargeIn() {
    if (!V.speaking) return
    dbg('打断触发')
    stopAudio()
    setPhase(PHASE.LISTENING)
    V.buf = ''
    V.replyEnded = false
    V.liveBubbleIndex = -1
    V.echoUntil = Date.now() + 2500
    if (V.ws && V.ws.readyState === 1) V.ws.send(JSON.stringify({ type: 'stop' }))
    setStatus('已打断，请继续')
  }

  // ---------- 降级本地语音 ----------
  function pickVoice() {
    const vs = window.speechSynthesis.getVoices()
    if (!vs || !vs.length) return null
    const prefs = ['Xiaoxiao', 'Xiaoyi', 'YunxiNeural', 'Xiaoyan', 'Huihui', 'Yaoyao', 'XiaoYun', 'Lili', 'zh-CN']
    for (const p of prefs) {
      for (const v of vs) {
        if ((v.name || '').indexOf(p) >= 0) return v
      }
    }
    for (const v of vs) {
      if ((v.lang || '').indexOf('zh') === 0) return v
    }
    return null
  }

  function speakFallback(text) {
    if (!text) return
    if (V.suppressAudio) return
    if (V.pendingSources > 0 || V.fallbackActive > 0) {
      V.fallbackQueue.push(text)
      return
    }
    startFallbackSpeech(text)
  }

  function drainFallback() {
    if (V.suppressAudio) return
    if (V.fallbackQueue.length) {
      startFallbackSpeech(V.fallbackQueue.shift())
      return
    }
    scheduleReady()
    audioDone()
  }

  function startFallbackSpeech(text) {
    if (V.suppressAudio) return // 已挂断/打断：放弃残留播报（bug #26）
    if (!V.speaking) {
      setPhase(PHASE.SPEAKING)
      setStatus('播报中…', true)
    }
    V.fallbackActive++
    const fallbackEnd = () => {
      V.fallbackActive = Math.max(0, V.fallbackActive - 1)
      drainFallback()
    }
    let guard = false // 幂等护栏：事件与超时只触发一次 doSpeak（bug #20）
    const doSpeak = () => {
      if (guard) return
      guard = true
      if (V.suppressAudio) {
        fallbackEnd()
        return
      }
      const u = new SpeechSynthesisUtterance(text)
      u.lang = 'zh-CN'
      u.rate = 0.95
      u.pitch = 1.1
      const voice = pickVoice()
      if (voice) u.voice = voice
      u.onend = fallbackEnd
      u.onerror = fallbackEnd
      try {
        window.speechSynthesis.speak(u)
      } catch (e) {
        fallbackEnd()
      }
    }
    const vs = window.speechSynthesis.getVoices()
    if ((!vs || !vs.length) && !V.voicesWaiting) {
      V.voicesWaiting = true
      const once = () => {
        V.voicesWaiting = false
        window.speechSynthesis.onvoiceschanged = null
        doSpeak()
      }
      window.speechSynthesis.onvoiceschanged = once
      setTimeout(once, 1500)
    } else {
      doSpeak()
    }
  }

  // ---------- WebSocket 消息注册表（按 type 分发） ----------
  const MSG_HANDLERS = {
    // 一轮回复开始：重置播放队列起点
    reply_start(m) {
      stopAudio()
      setPhase(PHASE.CONNECTING)
      V.prevSpeechText = V.speakingText || ''
      V.speakingText = ''
      V.replyEnded = false
      V.suppressAudio = false
      V.nextPlaySid = m.first_sid || 0
      setStatus('正在合成语音…', true)
    },
    // 文本增量（实时字幕）
    delta(m) {
      if (V.suppressAudio) return
      V.buf += m.content
      V.speakingText += m.content
      updateLive(V.buf)
    },
    audio_start(m) {
      onAudioStart(m.sid, m.text)
    },
    audio(m) {
      onAudioFrame(m.sid, m.data)
    },
    audio_end(m) {
      onAudioEnd(m.sid)
    },
    // 该段在线 TTS 失败：跳过并回退浏览器本地语音
    tts_error(m) {
      V.skippedSids[m.sid] = true
      scheduleReady()
      speakFallback(V.fallbackBySid[m.sid] || '')
    },
    done() {
      if (V.suppressAudio) return
      V.replyEnded = true
      setTimeout(audioDone, 300)
    },
    cancelled() {
      if (!V.suppressAudio) return
      V.buf = ''
      setStatus('已打断，请继续')
    },
    asr_ready() {
      V.asrReady = true
      V.lastAsrEvent = Date.now()
      setPhase(PHASE.LISTENING)
      setStatus('聆听中…')
      dbg('ASR 就绪')
    },
    asr_error(m) {
      V.asrReady = false
      V.lastAsrEvent = 0
      dbg('ASR错误: ' + (m.message || ''))
      setStatus('语音识别中断，自动重连中…', true)
    },
    // ASR 整句结果：当用户输入处理
    asr_text(m) {
      if (m.content) handleAsrText(m.content)
    },
    // ASR 中间结果：仅播报中处理，用于"开口即打断"
    asr_partial(m) {
      if (V.phase !== PHASE.SPEAKING) return
      const pt = (m.content || '').trim()
      if (!pt) return
      V.lastAsrEvent = Date.now()
      dbg('ASR中间: ' + pt.slice(0, 16))
      if (isEchoLike(pt)) {
        dbg('回声忽略(中间): ' + pt.slice(0, 14))
        return
      }
      dbg('开口打断(中间结果): ' + pt.slice(0, 18))
      bargeIn()
    },
    // 结构化错误：code 区分限流与内部错误
    error(m) {
      if (m.code === 'rate_limit') {
        setStatus('请求过于频繁，请稍候…', true)
      } else {
        setStatus('小P出错了: ' + (m.message || '未知错误'), true)
      }
    },
  }

  function onMessage(ev) {
    let m
    try {
      m = JSON.parse(ev.data)
    } catch (e) {
      return
    }
    const handler = MSG_HANDLERS[m.type]
    if (handler) handler(m)
  }

  // ---------- WebSocket ----------
  async function loadConfig() {
    try {
      const cfg = await configApi.voice()
      VAD.threshold = cfg.vad_threshold
      VAD.hits = cfg.vad_hits
      VAD.quietFrames = cfg.vad_quiet_frames
      VAD.noiseMargin = cfg.vad_noise_margin
    } catch (e) {
      /* 使用默认值 */
    }
  }

  async function connect() {
    if (ui.active) return
    // 先置接通状态防取票期间重入；失败路径各自复位
    ui.active = true
    setPhase(PHASE.CONNECTING)
    setStatus('正在接通…', true)
    // 长效令牌不出 Bearer 头：先经 REST 换一次性短时票据，URL 只带 ticket（bug #23）
    let ticket
    try {
      ticket = (await authApi.createWsTicket()).ticket
    } catch (e) {
      // 401 已由 http 拦截器统一处理（跳登录）；其余网络错误就地提示
      ui.active = false
      setPhase(PHASE.IDLE)
      setStatus('接通失败，请重试', true)
      return
    }
    ensureAudio()
    try {
      window.speechSynthesis.getVoices()
    } catch (e) {
      /* 忽略 */
    }
    const proto = window.location.protocol === 'https:' ? 'wss://' : 'ws://'
    const url =
      proto + window.location.host + '/ws/voice?ticket=' + encodeURIComponent(ticket)
    let ws
    try {
      ws = new WebSocket(url)
    } catch (e) {
      setStatus('无法连接语音服务', true)
      ui.active = false
      return
    }
    V.ws = ws
    V.lastSend = 0
    V.lastSendText = ''
    V.timerStart = Date.now()
    if (V.timerInt) clearInterval(V.timerInt)
    V.timerInt = setInterval(updateTimer, 1000)
    updateTimer()

    ws.onopen = () => {
      setStatus('正在接通…')
      startAudioStream()
    }

    ws.onmessage = onMessage

    ws.onclose = (ev) => {
      // 主动挂断不提示；被互踢（4409）单独提示；异常断开提示可重连
      if (ev && ev.code === 4409) {
        ui.active = false
        setStatus('账号已在其他页面接通', true)
      } else if (ui.active) {
        setStatus('连接已断开，点击可重连', true)
      }
      cleanupAudio()
    }

    ws.onerror = () => {
      setStatus('连接出错', true)
    }
  }

  function cleanupAudio() {
    stopAudio()
    stopAudioStream()
    setPhase(PHASE.IDLE)
    if (V.timerInt) {
      clearInterval(V.timerInt)
      V.timerInt = null
    }
    // 关闭并释放 AudioContext，避免反复进出语音页泄漏到上限后静默无声（bug #13）
    if (V.audioCtx) {
      try {
        V.audioCtx.close()
      } catch (e) {
        /* 忽略 */
      }
      V.audioCtx = null
    }
  }

  function disconnect() {
    ui.active = false
    if (V.ws) {
      try {
        V.ws.close()
      } catch (e) {
        /* 忽略 */
      }
      V.ws = null
    }
    cleanupAudio()
    setStatus('未连接 · 点击下方按钮接通')
    ui.timerText = '00:00'
  }

  function onStatusClick() {
    // 播报中点击状态条 = 手动打断
    if (ui.statusInterruptible) bargeIn()
  }

  // 组件卸载清理
  onUnmounted(() => {
    disconnect()
  })

  // 初始化：加载运行时配置 + 定制面试状态
  loadConfig()
  fetchCustomStatus()

  return {
    ui,
    connect,
    disconnect,
    onStatusClick,
    fetchCustomStatus,
  }
}
