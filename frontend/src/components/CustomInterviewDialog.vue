<template>
  <el-dialog
    :model-value="visible"
    title="🎯 定制面试"
    width="min(560px, 94vw)"
    destroy-on-close
    @update:model-value="emit('update:visible', $event)"
  >
    <template v-if="!generating">
      <el-form label-position="top">
        <el-form-item label="目标岗位">
          <el-input v-model="jobTitle" placeholder="如：Python 后端工程师" />
        </el-form-item>
        <el-form-item label="招聘信息（选填）">
          <el-input v-model="jd" type="textarea" :rows="4" placeholder="粘贴岗位职责与任职要求…" />
        </el-form-item>
      </el-form>
    </template>

    <template v-else>
      <div class="gen-box">
        <el-progress :percentage="progressPct" :stroke-width="8" :show-text="false" />
        <p class="gen-status">
          <span class="spinner" />
          {{ progressMsg || '正在准备定制面试…' }}
        </p>
      </div>
    </template>

    <template #footer>
      <el-button v-if="!generating" @click="emit('update:visible', false)">取消</el-button>
      <el-button v-if="!generating" type="primary" :loading="generating" @click="generate">
        生成面试题
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatStore } from '../stores/chat'

defineProps({
  visible: { type: Boolean, default: false },
})
const emit = defineEmits(['update:visible', 'done'])

const chat = useChatStore()
const jobTitle = ref('')
const jd = ref('')
const generating = ref(false)
const progressMsg = ref('')
const progressPct = computed(() => (progressMsg.value ? 100 : 0))

async function generate() {
  if (!jobTitle.value.trim() && !jd.value.trim()) {
    ElMessage.warning('请填写目标岗位或招聘信息')
    return
  }
  generating.value = true
  progressMsg.value = '正在识别岗位技术栈…'
  await chat.startCustom(jobTitle.value, jd.value, {
    onProgress: (m) => {
      progressMsg.value = m
    },
    onError: (m) => {
      generating.value = false
      progressMsg.value = ''
      ElMessage.error(m || '生成失败，请稍后重试')
    },
    onDone: (ev) => {
      generating.value = false
      progressMsg.value = ''
      ElMessage.success('题目已就绪 ✅')
      emit('update:visible', false)
      emit('done', ev)
    },
  })
}
</script>

<style scoped>
.gen-box {
  padding: 20px 8px;
  text-align: center;
}
.gen-status {
  margin-top: 16px;
  color: var(--text);
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--border);
  border-top-color: var(--brand);
  border-radius: 50%;
  animation: rot 0.8s linear infinite;
}
@keyframes rot {
  to {
    transform: rotate(360deg);
  }
}
</style>
