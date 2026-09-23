<template>
  <div class="welcome">
    <div class="hero">
      <div class="hero-title">开始你的面试训练</div>
      <div class="hero-sub">模拟面试 · 辅导答疑 · 定制练习</div>
    </div>
    <el-row :gutter="14">
      <el-col v-for="card in cards" :key="card.title" :xs="24" :sm="8">
        <div class="qcard">
          <div class="qicon">
            <el-icon><component :is="card.icon" /></el-icon>
          </div>
          <div class="qtitle">{{ card.title }}</div>
          <div class="qdesc">{{ card.desc }}</div>
          <el-button class="qbtn" type="primary" plain @click="emit('action', card.key)">
            {{ card.label }}
          </el-button>
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
const emit = defineEmits(['action'])

const cards = [
  {
    key: 'mock',
    icon: 'Microphone',
    title: '模拟面试',
    desc: '难度递进 · 逐题点评',
    label: '开始面试',
  },
  {
    key: 'coach',
    icon: 'Opportunity',
    title: '辅导答疑',
    desc: '标准回答 · 加分点 · 变式题',
    label: '开始答疑',
  },
  {
    key: 'custom',
    icon: 'Aim',
    title: '定制面试',
    desc: '按岗位与 JD 生成专属题',
    label: '开始定制',
  },
]
</script>

<style scoped>
.hero {
  text-align: center;
  margin: 12px 0 10px;
}
.hero-title {
  font-size: 21px;
  font-weight: 700;
  color: var(--ink-strong);
  letter-spacing: 0.5px;
}
.hero-sub {
  margin-top: 6px;
  font-size: 13px;
  color: var(--muted);
  letter-spacing: 0.5px;
}
.qcard {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 14px 12px 12px;
  text-align: center;
  box-shadow: var(--shadow-card);
  margin-bottom: 10px;
  transition:
    transform 0.15s,
    box-shadow 0.15s;
}
.qcard:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-float);
}
.qicon {
  font-size: 26px;
  line-height: 1;
  color: var(--brand);
}
.qtitle {
  margin-top: 6px;
  font-size: 15px;
  font-weight: 700;
  color: var(--ink);
}
.qdesc {
  margin-top: 4px;
  font-size: 12px;
  color: var(--muted);
  min-height: 34px;
}
.qbtn {
  width: 100%;
  margin-top: 8px;
  border-radius: 10px;
}
</style>
