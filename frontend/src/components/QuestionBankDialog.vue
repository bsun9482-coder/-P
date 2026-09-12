<template>
  <el-dialog
    :model-value="visible"
    :title="'📚 题库'"
    width="min(1080px, 96vw)"
    top="4vh"
    destroy-on-close
    @update:model-value="emit('update:visible', $event)"
  >
    <el-collapse v-model="openPanels" class="bank-collapse">
      <!-- 添加自定义题 -->
      <el-collapse-item name="add">
        <template #title>
          <span>➕ 添加自定义题</span>
        </template>
        <el-form label-width="80px" label-position="left">
          <el-form-item label="题干">
            <el-input v-model="form.title" placeholder="如：如何设计一个限流组件？" />
          </el-form-item>
          <el-form-item label="参考答案">
            <el-input v-model="form.answer" type="textarea" :rows="3" placeholder="选填" />
          </el-form-item>
          <el-row :gutter="10">
            <el-col :span="8">
              <el-form-item label="标签">
                <el-input v-model="form.tags" placeholder="逗号分隔，如 Redis,限流" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="难度">
                <el-select v-model="form.difficulty">
                  <el-option label="简单" value="简单" />
                  <el-option label="中等" value="中等" />
                  <el-option label="困难" value="困难" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="公司">
                <el-input v-model="form.company" placeholder="选填" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item>
            <el-button type="primary" @click="submitAdd">添加到题库</el-button>
          </el-form-item>
        </el-form>
      </el-collapse-item>

      <!-- 批量导入 CSV -->
      <el-collapse-item name="import">
        <template #title>
          <span>📥 批量导入自定义题（CSV）</span>
        </template>
        <el-input
          v-model="csvText"
          type="textarea"
          :rows="4"
          placeholder="每行一题，至少包含「题干」；可选列：答案、标签（逗号分隔）、难度（简单/中等/困难）、公司。支持表头或无表头按列顺序。"
        />
        <el-button
          type="primary"
          class="import-btn"
          :loading="importing"
          @click="submitImport"
        >
          导入
        </el-button>
      </el-collapse-item>
    </el-collapse>

    <!-- 筛选：变更即自动应用；来源/公司无数据时隐藏对应筛选项，避免出现"无数据"死控件 -->
    <div class="filters">
      <el-select v-if="meta.sources.length" v-model="filters.source" placeholder="来源" clearable class="f-item" @change="load()">
        <el-option v-for="s in meta.sources" :key="s.key" :label="s.label" :value="s.key" />
      </el-select>
      <el-select v-model="filters.difficulty" placeholder="难度" clearable class="f-item" @change="load()">
        <el-option label="简单" value="简单" />
        <el-option label="中等" value="中等" />
        <el-option label="困难" value="困难" />
      </el-select>
      <el-select v-if="meta.companies.length" v-model="filters.company" placeholder="公司" clearable filterable class="f-item" @change="load()">
        <el-option v-for="c in meta.companies" :key="c" :label="c" :value="c" />
      </el-select>
      <el-input v-model="filters.keyword" placeholder="关键词，如 Redis / 索引" clearable class="f-item" @keyup.enter="load()" @clear="load()" />
    </div>
    <div class="tags-row">
      <el-select v-model="filters.tags" multiple collapse-tags collapse-tags-tooltip placeholder="标签筛选" class="tags-select" @change="load()">
        <el-option v-for="t in meta.tags" :key="t.name" :label="`${t.name} (${t.count})`" :value="t.name" />
      </el-select>
      <el-checkbox v-model="filters.favoriteOnly" label="⭐ 仅看收藏" @change="load()" />
      <el-button size="small" :loading="loading" @click="load()">查询</el-button>
    </div>

    <!-- 题目列表 + 已选 -->
    <div class="bank-body">
      <div class="qlist">
        <el-empty v-if="loaded && !error && !rows.length" description="没有找到匹配的题，换个关键词或筛选条件试试～" :image-size="80" />
        <div v-else-if="error" class="bank-error">
          <p>{{ error }}</p>
          <el-button size="small" type="primary" @click="load()">重试</el-button>
        </div>
        <div v-else-if="!loaded" class="skeleton">
          <div v-for="i in 4" :key="i" class="skeleton-row"></div>
        </div>
        <div v-for="q in rows" :key="q.id" class="qrow" :class="{ selected: isSelected(q.id) }">
          <div class="qinfo">
            <div class="qtitle-text">{{ q.title }}</div>
            <div class="qmeta">
              <el-tag size="small" :type="tagType(q.difficulty)">{{ q.difficulty }}</el-tag>
              <el-tag size="small" effect="plain">{{ q.source_label }}</el-tag>
              <span v-if="q.company" class="qcompany">{{ q.company }}</span>
            </div>
          </div>
          <div class="qops">
            <el-button
              size="small"
              :type="isSelected(q.id) ? 'primary' : 'default'"
              @click="toggleSelected(q)"
            >
              {{ isSelected(q.id) ? '✓ 已加入' : '加入面试' }}
            </el-button>
            <el-button size="small" :type="isFav(q.id) ? 'warning' : 'default'" class="fav-btn" @click="toggleFav(q)">
              {{ isFav(q.id) ? '★ 已收藏' : '☆ 收藏' }}
            </el-button>
          </div>
        </div>
      </div>

      <div v-if="selected.length" class="qsel">
        <div class="sel-title">🎯 已选题目（{{ selected.length }}）</div>
        <div v-for="(s, i) in selected" :key="s.id" class="sel-row">
          <span class="sel-idx">{{ i + 1 }}.</span>
          <span class="sel-title-text">{{ s.title }}</span>
          <el-button size="small" text type="danger" @click="removeSelected(s.id)">✕</el-button>
        </div>
        <el-button
          type="primary"
          class="sel-start"
          @click="emit('start', selected.map((s) => s.title))"
        >
          🚀 开始综合面试（{{ selected.length }} 题）
        </el-button>
      </div>
    </div>

    <template #footer>
      <el-button @click="emit('update:visible', false)">关闭</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { bankApi } from '../api'

const props = defineProps({
  visible: { type: Boolean, default: false },
})
const emit = defineEmits(['update:visible', 'start'])

const openPanels = ref([])
const meta = reactive({ sources: [], companies: [], tags: [] })
const filters = reactive({
  source: '',
  difficulty: '',
  company: '',
  keyword: '',
  tags: [],
  favoriteOnly: false,
})
const rows = ref([])
const loaded = ref(false) // 首次加载完成前显示 loading 而非"暂无题目"空态（bug #30）
const loading = ref(false) // 筛选请求进行中：查询按钮转圈，避免重复点击
const error = ref('') // 加载失败提示；非空时列表区显示错误态 + 重试（bug #30 补）
const favoriteIds = ref(new Set())
let loadSeq = 0 // 请求代际号：丢弃过期响应，防止慢响应覆盖新筛选结果（查询竞态）
const selected = ref([])
const form = reactive({ title: '', answer: '', tags: '', difficulty: '中等', company: '' })
const csvText = ref('')
const importing = ref(false)

const isSelected = (id) => selected.value.some((s) => s.id === id)
const isFav = (id) => favoriteIds.value.has(id)

function tagType(d) {
  if (d === '简单') return 'success'
  if (d === '困难') return 'danger'
  return 'warning'
}

async function loadMeta() {
  try {
    const m = await bankApi.meta()
    meta.sources = m.sources
    meta.companies = m.companies
    meta.tags = m.tags
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '加载筛选选项失败，请检查网络后重试')
  }
}

async function load() {
  const seq = ++loadSeq
  loading.value = true
  error.value = ''
  try {
    const r = await bankApi.browse({
      keyword: filters.keyword || undefined,
      source: filters.source || undefined,
      difficulty: filters.difficulty || undefined,
      company: filters.company || undefined,
      tags: filters.tags,
      favorite_only: filters.favoriteOnly || undefined,
      limit: 60,
    })
    if (seq !== loadSeq) return // 已有更新的请求，丢弃本次过期结果（竞态守卫）
    rows.value = r.items
    favoriteIds.value = new Set(r.favorite_ids)
    loaded.value = true
  } catch (e) {
    if (seq !== loadSeq) return
    error.value = '加载题库失败，请检查网络后重试'
    ElMessage.error(e?.response?.data?.detail || error.value)
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

watch(
  () => props.visible,
  (v) => {
    if (v) {
      loadMeta()
      load()
    }
  },
)

onMounted(() => {
  if (props.visible) {
    loadMeta()
    load()
  }
})

function toggleSelected(q) {
  const i = selected.value.findIndex((s) => s.id === q.id)
  if (i >= 0) selected.value.splice(i, 1)
  else selected.value.push({ id: q.id, title: q.title })
}
function removeSelected(id) {
  selected.value = selected.value.filter((s) => s.id !== id)
}

async function toggleFav(q) {
  const fav = favoriteIds.value.has(q.id)
  try {
    if (fav) {
      await bankApi.removeFavorite(q.id)
      favoriteIds.value.delete(q.id)
      ElMessage.success('已取消收藏')
    } else {
      await bankApi.addFavorite(q.id)
      favoriteIds.value.add(q.id)
      ElMessage.success(`已收藏「${q.title.slice(0, 12)}」`)
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '操作失败，请稍后重试')
  }
}

async function submitAdd() {
  if (!form.title.trim()) {
    ElMessage.warning('请填写题干')
    return
  }
  await bankApi.add({
    title: form.title.trim(),
    answer: form.answer,
    tags: form.tags.split(',').map((t) => t.trim()).filter(Boolean),
    difficulty: form.difficulty,
    company: form.company.trim(),
  })
  ElMessage.success('已添加到题库')
  form.title = form.answer = form.tags = form.company = ''
  loadMeta()
  load()
}

async function submitImport() {
  if (!csvText.value.trim()) {
    ElMessage.warning('请粘贴 CSV 内容')
    return
  }
  importing.value = true
  try {
    const stats = await bankApi.importCsv(csvText.value)
    ElMessage.success(`导入成功 ${stats.new} 条${stats.skipped ? `，跳过重复 ${stats.skipped} 条` : ''}`)
    csvText.value = ''
    loadMeta()
    load()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '导入失败，请检查格式')
  } finally {
    importing.value = false
  }
}
</script>

<style scoped>
.bank-collapse {
  margin-bottom: 12px;
}
.import-btn {
  margin-top: 10px;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 10px;
}
/* 筛选项：等宽自适应；窄屏自动换行；来源/公司无数据隐藏后其余项自然补位 */
.f-item {
  flex: 1 1 190px;
  min-width: 0;
}
.filters :deep(.el-select),
.filters :deep(.el-input) {
  width: 100%;
}
/* 加载失败错误态 */
.bank-error {
  padding: 26px 0;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
}
.bank-error p {
  margin: 0 0 12px;
}
.tags-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.tags-select {
  width: 280px;
}
.bank-body {
  display: flex;
  gap: 14px;
  min-height: 320px;
}
.qlist {
  flex: 1;
  overflow-y: auto;
  max-height: 52vh;
  min-height: 160px; /* 首次加载中保持高度，loading 遮罩可见（bug #30） */
  padding-right: 4px;
}
/* 骨架屏：题库加载占位 */
.skeleton {
  padding-right: 4px;
}
.skeleton-row {
  height: 58px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: linear-gradient(90deg, var(--surface) 25%, #faf3e8 37%, var(--surface) 63%);
  background-size: 400% 100%;
  animation: sk 1.2s ease infinite;
  margin-bottom: 8px;
}
@keyframes sk {
  0% {
    background-position: 100% 50%;
  }
  100% {
    background-position: 0% 50%;
  }
}
/* 收藏星：已收藏琥珀、hover 放大 */
.fav-btn {
  transition: transform 0.15s ease;
}
.qrow:hover .fav-btn:not(.is-warning) {
  transform: scale(1.06);
}
.fav-btn.is-warning {
  font-weight: 600;
}
.qrow {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 8px 10px;
  margin-bottom: 8px;
  gap: 10px;
  background: #fff;
}
.qrow.selected {
  background: #f9e6d8;
  border-left: 3px solid var(--brand);
}
.qinfo {
  min-width: 0;
}
.qtitle-text {
  font-weight: 600;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.qmeta {
  margin-top: 3px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.qcompany {
  font-size: 12px;
  color: var(--muted);
}
.qops {
  display: flex;
  gap: 6px;
  flex: 0 0 auto;
}
.qsel {
  flex: 0 0 260px;
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 10px;
  background: var(--surface);
  max-height: 52vh;
  overflow-y: auto;
}
.sel-title {
  font-weight: 700;
  margin-bottom: 8px;
  font-size: 13px;
}
.sel-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 0;
  font-size: 13px;
}
.sel-idx {
  color: var(--muted);
}
.sel-title-text {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sel-start {
  width: 100%;
  margin-top: 10px;
}

/* 窄屏：题目/已选面板纵向堆叠、qsel 全宽（筛选区 flex 布局已自动换行，无需单独处理） */
@media (max-width: 600px) {
  .bank-body {
    flex-direction: column;
  }
  .qsel {
    flex: none;
    width: 100%;
    max-height: 220px;
  }
}
</style>
