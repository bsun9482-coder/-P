<template>
  <el-select
    :model-value="modelValue"
    :placeholder="placeholder"
    class="persona-select"
    size="default"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-option v-for="(desc, name) in PERSONAS" :key="name" :label="name" :value="name">
      <div class="persona-opt">
        <div class="persona-name">{{ name }}</div>
        <div class="persona-desc">{{ desc }}</div>
      </div>
    </el-option>
  </el-select>
</template>

<script setup>
// 面试官人格选择（与后端 prompts.PERSONAS 保持一致）
defineProps({
  modelValue: { type: String, default: '' },
  placeholder: { type: String, default: '面试官风格' },
})
const emit = defineEmits(['update:modelValue'])

const PERSONAS = {
  '一面 · 同级工程师': '语气随和，像技术伙伴一样平等交流，多鼓励少施压',
  '二面 · 资深工程师': '风格严谨，深挖底层原理、边界与异常场景',
  '三面 · 直属经理': '关注项目落地、协作与系统设计，提问偏宏观',
}
</script>

<style scoped>
.persona-select {
  width: 100%;
}
.persona-opt {
  display: flex;
  flex-direction: column;
}
.persona-name {
  font-weight: 600;
  font-size: 13px;
}
.persona-desc {
  font-size: 11px;
  color: var(--muted);
  margin-top: 1px;
}
</style>
