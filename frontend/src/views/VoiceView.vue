<template>
  <div class="vphone">
    <div class="vtop">
      <span class="back" @click="goBack"
        ><el-icon><Back /></el-icon>文字版</span
      >
      <div class="title">
        <el-icon><Microphone /></el-icon>面试官小P
      </div>
      <div class="vmode">{{ ui.mode }}</div>
    </div>

    <div class="vavatar" :class="{ glow: ui.glowOn }">
      <div class="av-fig">
        <img src="/assets/avatar.png" alt="面试官小P" />
      </div>
    </div>

    <div class="vstatusrow">
      <div
        class="vstatus"
        :class="{ busy: ui.statusBusy, interruptible: ui.statusInterruptible }"
        @click="onStatusClick"
      >
        {{ ui.statusText }}
      </div>
      <div class="vtimer">{{ ui.timerText }}</div>
    </div>

    <div class="vmicrow" :class="{ on: ui.micOn }">
      <span class="miclabel">麦克风</span>
      <div class="vmicbar">
        <i :style="{ width: ui.micLevel + '%' }" />
      </div>
    </div>

    <div class="vwave" :class="{ on: ui.waveOn }"><i /><i /><i /><i /><i /></div>

    <div ref="transcriptRef" class="vtranscript">
      <div v-for="(b, i) in ui.transcript" :key="i" class="vbubble" :class="b.role">
        {{ b.text }}
      </div>
      <div v-if="!ui.transcript.length" class="vbubble hint">
        {{ hintText }}
      </div>
    </div>
  </div>

  <div class="vfooter">
    <button
      id="callBtn"
      :class="{ on: ui.active }"
      :title="ui.active ? '挂断' : '接通'"
      @click="toggle"
    >
      <el-icon><component :is="ui.active ? 'Close' : 'PhoneFilled'" /></el-icon>
      <span class="lbl">{{ ui.active ? '挂断' : '接通' }}</span>
    </button>
  </div>

  <div v-if="ui.debugOn" class="vdbg">{{ ui.debugLog }}</div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useVoiceCall } from '../composables/voice/useVoiceCall'

const router = useRouter()
const transcriptRef = ref(null)

const { ui, connect, disconnect, onStatusClick } = useVoiceCall()

const hintText = computed(() =>
  ui.voiceReady
    ? `已为你准备好「${ui.customJobTitle || '自定义岗位'}」定制面试，接通后小P会直接开始。`
    : '接通后小P会先问候你。直接说话即可提问；说「开始面试」自动切换为模拟面试。小P播报时点击上方状态条可立即打断。',
)

function toggle() {
  if (ui.active) disconnect()
  else connect()
}

function goBack() {
  if (ui.active) disconnect()
  router.push('/')
}

// 新消息后滚动到底部
watch(
  () => ui.transcript.length,
  async () => {
    await nextTick()
    const el = transcriptRef.value
    if (el) el.scrollTop = el.scrollHeight
  },
)
</script>

<style scoped>
/* 布局样式已在全局 main.css 中定义（.vphone/.vtop/... 等） */
</style>
