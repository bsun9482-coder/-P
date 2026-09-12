<template>
  <div class="chat-page">
    <!-- 顶部：品牌 + 用户菜单 -->
    <header class="chat-header">
      <div class="brand">
        <div class="brand-avatar">
          <img src="/assets/avatar_small.png" alt="小P" />
        </div>
        <div>
          <div class="brand-name">面试官小P</div>
          <div class="brand-status"><i></i>在线</div>
        </div>
      </div>
      <div class="header-right">
        <el-button size="small" class="bank-btn" @click="bankVisible = true">
          <el-icon><Collection /></el-icon>
          题库
        </el-button>
        <!-- 窄屏抽屉入口：仅 @media(max-width:820px) 时显示 -->
        <el-button size="small" text class="side-toggle" @click="sideOpen = !sideOpen">
          <el-icon><Histogram /></el-icon>
          历史
        </el-button>
        <el-dropdown trigger="click" @command="onCommand">
          <span class="user-chip">
            <el-icon><User /></el-icon>
            {{ auth.nickname }}
            <el-icon><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="profile">个人资料</el-dropdown-item>
              <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>

    <!-- 窄屏抽屉遮罩：点击关闭 -->
    <div v-if="sideOpen" class="side-mask" @click="sideOpen = false"></div>

    <div class="chat-layout">
      <!-- 侧栏：题库统计 + 历史（窄屏经 side-toggle 拉出抽屉） -->
      <aside class="chat-side" :class="{ open: sideOpen }">
        <div class="side-title">题库统计</div>
        <el-row :gutter="8">
          <el-col v-for="s in stats" :key="s.key" :span="12">
            <div class="stat-card">
              <div class="stat-num">{{ s.count }}</div>
              <div class="stat-label">{{ s.label }}</div>
            </div>
          </el-col>
        </el-row>
        <el-button class="side-btn" size="small" @click="bankVisible = true">
          <el-icon><Collection /></el-icon>
          浏览题库 / 选题
        </el-button>

        <div class="side-title" style="margin-top: 18px">历史记录</div>
        <div v-if="!history.length" class="side-note">还没有面试记录，来一场热身吧</div>
        <div v-for="h in history" :key="h.id" class="rev-row">
          <span>{{ h.job_title || (h.mode === 'mock' ? '模拟面试' : '辅导答疑') }}</span>
          <span class="rev-meta">{{ fmtDate(h.started_at) }}</span>
          <span v-if="h.score != null" class="rev-score" :class="scoreTone(h.score)">{{ h.score }}</span>
        </div>

        <div class="side-note">数据按账号隔离 · 收藏/历史云端同步</div>
      </aside>

      <!-- 主区 -->
      <main class="chat-main">
        <div class="persona-row">
          <div class="persona-box">
            <span class="persona-label">面试官风格</span>
            <PersonaSelect v-model="persona" />
          </div>
          <div v-if="chat.active" class="mode-chip">
            {{ chat.finished ? '已结束 · 可开始新一轮' : `当前模式：${modeLabel}` }}
          </div>
          <el-button
            v-if="chat.active"
            size="small"
            text
            type="primary"
            @click="backHome"
          >
            <el-icon><HomeFilled /></el-icon>
            返回首页
          </el-button>
        </div>

        <div v-if="chat.voiceReady" class="voice-ready-row">
          <div class="voice-ready">
            📞 定制面试已就绪：{{ chat.customJobTitle || '自定义' }}，接通电话即可开始语音面试
            <div class="voice-actions">
              <el-button size="small" type="primary" @click="goVoice">开始语音面试</el-button>
              <el-button size="small" @click="cancelVoice">取消</el-button>
            </div>
          </div>
        </div>

        <div ref="scrollRef" class="chat-scroll">
          <WelcomeCards v-if="!chat.active && !chat.history.length" @action="onWelcomeAction" />
          <template v-for="(m, i) in chat.history" :key="i">
            <ChatBubble
              v-if="!isReportBubble(m, i)"
              :role="m.role"
              :content="m.content"
              :streaming="m.streaming"
            />
          </template>
          <ReportPanel
            v-if="chat.finished && chat.report"
            :report="chat.report"
            :report-data="chat.reportData"
            @download="downloadReport"
          />
        </div>

        <div class="chat-input-bar">
          <el-input
            v-model="input"
            type="textarea"
            :rows="2"
            resize="none"
            placeholder="输入你的回答或问题…（Enter 发送，Shift+Enter 换行）"
            :disabled="chat.sending"
            @keydown.enter.exact.prevent="send"
          />
          <el-button
            type="primary"
            class="send-btn"
            :loading="chat.sending"
            :disabled="!input.trim()"
            @click="send"
          >
            发送
          </el-button>
        </div>
      </main>
    </div>

    <!-- 语音入口 -->
    <a class="vc-float" title="打开语音通话" @click.prevent="goVoice"><el-icon><PhoneFilled /></el-icon></a>

    <!-- 对话框 -->
    <QuestionBankDialog v-model:visible="bankVisible" @start="startComprehensive" />
    <CustomInterviewDialog
      v-model:visible="customVisible"
      @done="onCustomDone"
    />

    <!-- 个人资料 -->
    <el-dialog v-model="profileVisible" title="个人资料" width="min(420px, 92vw)">
      <el-form label-width="70px">
        <el-form-item label="用户名">
          <el-input :model-value="auth.user?.username" disabled />
        </el-form-item>
        <el-form-item label="昵称">
          <el-input v-model="profile.nickname" />
        </el-form-item>
        <el-form-item label="默认人格">
          <PersonaSelect v-model="profile.persona" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="profileVisible = false">取消</el-button>
        <el-button type="primary" @click="saveProfile">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowDown, Histogram, User } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'
import { useChatStore } from '../stores/chat'
import { bankApi, sessionApi } from '../api'
import PersonaSelect from '../components/PersonaSelect.vue'
import WelcomeCards from '../components/WelcomeCards.vue'
import ChatBubble from '../components/ChatBubble.vue'
import QuestionBankDialog from '../components/QuestionBankDialog.vue'
import CustomInterviewDialog from '../components/CustomInterviewDialog.vue'
import ReportPanel from '../components/ReportPanel.vue'

const auth = useAuthStore()
const chat = useChatStore()
const router = useRouter()

const input = ref('')
const scrollRef = ref(null)
const bankVisible = ref(false)
const customVisible = ref(false)
const profileVisible = ref(false)
const sideOpen = ref(false) // 窄屏侧栏抽屉开关
const profile = reactive({ nickname: '', persona: '' })

const persona = computed({
  get: () => chat.persona || profile.persona,
  set: (v) => {
    chat.persona = v
  },
})

const modeLabel = computed(() =>
  chat.mode === 'mock' ? '模拟面试' : '辅导答疑',
)

const stats = ref([])
const history = ref([])

function fmtDate(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(
    d.getMinutes(),
  ).padStart(2, '0')}`
}

// 历史分数徽章档位：≥80 优秀(绿) / ≥60 及格(琥珀) / <60 待提升(砖红)
function scoreTone(s) {
  if (s == null) return ''
  if (s >= 80) return 'good'
  if (s >= 60) return 'mid'
  return 'low'
}

async function loadStats() {
  try {
    const m = await bankApi.meta()
    stats.value = m.sources.map((s) => ({ key: s.key, label: s.label, count: s.count }))
  } catch (e) {
    /* 忽略 */
  }
}

async function loadHistory() {
  try {
    const r = await sessionApi.history()
    history.value = r.items
  } catch (e) {
    /* 忽略 */
  }
}

async function scrollBottom() {
  await nextTick()
  const el = scrollRef.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(
  () => chat.history.length,
  () => scrollBottom(),
)
watch(() => chat.sending, (s) => s && scrollBottom())

/** 仅隐藏真正的报告消息（历史中最后一条内容与 chat.report 一致的助手消息），
 *  避免误藏同内容的早期回答；报告气泡由 ReportPanel 替代展示。 */
const reportIndex = computed(() => {
  if (!chat.finished || !chat.report) return -1
  for (let i = chat.history.length - 1; i >= 0; i--) {
    const m = chat.history[i]
    if (m.role === 'assistant' && !m.streaming && m.content === chat.report) return i
  }
  return -1
})

function isReportBubble(m, i) {
  return i === reportIndex.value
}

onMounted(async () => {
  try {
    await chat.load()
  } catch (e) {
    ElMessage.error('加载会话失败，请检查网络后重试')
  }
  if (!chat.active) {
    try {
      await chat.refreshVoiceStatus()
    } catch (e) {
      /* 忽略 */
    }
  }
  loadStats()
  loadHistory()
  if (auth.user?.persona) chat.persona = chat.persona || auth.user.persona
})

function onWelcomeAction(key) {
  if (key === 'mock') startMock()
  else if (key === 'coach') startCoach()
  else customVisible.value = true
}

async function startMock() {
  try {
    await chat.start({ mode: 'mock', persona: persona.value })
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '开始模拟面试失败，请稍后重试')
  }
  await nextTick()
  scrollBottom()
}

async function startCoach() {
  try {
    await chat.start({ mode: 'coach', persona: persona.value })
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '开始辅导失败，请稍后重试')
  }
  await nextTick()
  scrollBottom()
}

async function startComprehensive(titles) {
  bankVisible.value = false
  try {
    await chat.start({
      mode: 'mock',
      questions: titles,
      job_title: '综合练习',
      persona: persona.value,
    })
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '开始综合面试失败，请稍后重试')
    return
  }
  ElMessage.success(`已挑选 ${titles.length} 道题开始综合面试`)
  await nextTick()
  scrollBottom()
}

async function onCustomDone() {
  await chat.refreshVoiceStatus()
  await nextTick()
  scrollBottom()
}

async function backHome() {
  try {
    await chat.reset()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '返回首页失败，请稍后重试')
  }
  input.value = ''
}

async function send() {
  const msg = input.value.trim()
  if (!msg || chat.sending) return
  if (!chat.active) {
    // 无会话时默认进入辅导答疑
    await chat.start({ mode: 'coach', persona: persona.value })
  }
  input.value = ''
  await chat.send(msg)
}

function downloadReport() {
  if (!chat.report) return
  const blob = new Blob([chat.report], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '')
  a.href = URL.createObjectURL(blob)
  a.download = `面试报告_${stamp}.md`
  a.click()
  URL.revokeObjectURL(a.href)
}

function goVoice() {
  router.push('/voice')
}

async function cancelVoice() {
  await chat.clearVoice()
}

async function onCommand(cmd) {
  if (cmd === 'logout') {
    // 先中止进行中的 SSE 流，避免后端继续生成、聊天锁被占用（bug #15）
    chat._abortStream()
    await auth.logout()
    // 重置聊天状态：否则下一账号登录首帧会闪现上一账号的对话/报告（bug #20）
    chat.$reset()
    router.push('/login')
  } else if (cmd === 'profile') {
    profile.nickname = auth.user?.nickname || ''
    profile.persona = auth.user?.persona || ''
    profileVisible.value = true
  }
}

async function saveProfile() {
  // 昵称超长直接在前端拦截，避免依赖后端 422（bug #27）
  if (profile.nickname && profile.nickname.length > 32) {
    ElMessage.warning('昵称最多 32 个字符')
    return
  }
  if (profile.persona && profile.persona.length > 64) {
    ElMessage.warning('人格描述最多 64 个字符')
    return
  }
  try {
    await auth.updateMe({ nickname: profile.nickname, persona: profile.persona })
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存失败，请稍后重试')
    return
  }
  if (profile.persona) chat.persona = profile.persona
  ElMessage.success('已保存')
  profileVisible.value = false
}
</script>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: 100vh; /* 旧浏览器回退（Safari <15.4 不支持 dvh，bug #31） */
  height: 100dvh;
  max-width: 1180px;
  margin: 0 auto;
  padding: 0 16px;
}
.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 4px 10px;
  flex: 0 0 auto;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
  padding: 6px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: #fff;
}
.user-chip:hover {
  border-color: var(--brand-light);
  color: var(--brand);
}
.bank-btn {
  border-radius: 999px;
}
.chat-layout {
  display: flex;
  gap: 16px;
  flex: 1 1 auto;
  min-height: 0;
}
.chat-side {
  width: 220px;
  flex: 0 0 220px;
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 16px 14px;
}
.side-btn {
  width: 100%;
  margin-top: 12px;
}
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 14px 18px 16px;
  box-shadow: var(--shadow-card);
}
.persona-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 0 0 auto;
  margin-bottom: 10px;
}
.persona-box {
  display: flex;
  align-items: center;
  gap: 8px;
}
.persona-box :deep(.el-select) {
  width: 180px;
}
.persona-label {
  font-size: 13px;
  color: var(--muted);
  white-space: nowrap;
}
.mode-chip {
  font-size: 13px;
  color: var(--muted);
  letter-spacing: 0.4px;
}
.voice-ready-row {
  flex: 0 0 auto;
  margin-bottom: 10px;
}
.voice-actions {
  display: inline-flex;
  gap: 8px;
  margin-left: 12px;
}
.chat-scroll {
  flex: 1 1 auto;
  overflow-y: auto;
  min-height: 0;
  padding: 4px 4px 8px;
}
.chat-input-bar {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  flex: 0 0 auto;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
.chat-input-bar :deep(.el-textarea) {
  flex: 1;
}
.send-btn {
  width: 84px;
  height: 46px;
  border-radius: 12px;
}
.vc-float {
  position: fixed;
  bottom: 104px;
  right: 20px;
  z-index: 9999;
  width: 54px;
  height: 54px;
  border-radius: 50%;
  background: var(--brand);
  border: none;
  cursor: pointer;
  box-shadow: 0 4px 18px rgba(var(--brand-rgb), 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  transition: all 0.25s;
  color: #fff;
  text-decoration: none;
}
.vc-float:hover {
  transform: scale(1.08);
  box-shadow: 0 6px 24px rgba(var(--brand-rgb), 0.5);
}

.side-toggle {
  display: none; /* 仅在窄屏 @media 显示 */
}

@media (max-width: 820px) {
  .chat-side {
    display: none;
  }
  /* 窄屏抽屉：经 header 的"历史"按钮拉出（bug #30） */
  .chat-side.open {
    display: flex;
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: 78%;
    max-width: 320px;
    z-index: 40;
    overflow-y: auto;
    box-shadow: var(--shadow-float);
  }
  /* 抽屉遮罩：置于抽屉下方 */
  .side-mask {
    position: fixed;
    inset: 0;
    z-index: 39;
    background: rgba(43, 38, 30, 0.32);
  }
  .side-toggle {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
  .chat-page {
    padding: 0 8px;
  }
  .persona-box :deep(.el-select) {
    width: 140px;
  }
}
</style>
