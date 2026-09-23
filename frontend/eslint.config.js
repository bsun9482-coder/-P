// ESLint 扁平配置（ESLint 9+）。
//
// 定位：只抓「改坏了」这类真问题 —— 未定义变量、拼错的全局名、未使用的导入、
// Vue 模板里的结构性错误（v-if 与 v-for 同元素、缺 :key、插槽用法错）等。
// **不放风格规则**：引号/分号/换行由 Prettier 负责（npm run format:check），
// 两边职责不重叠，避免同一处报两遍、也避免为了过 lint 去改格式。
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'

export default [
  // 构建产物与依赖不进检查
  { ignores: ['dist/**', 'node_modules/**'] },

  // JS 基础规则集（no-undef / no-unused-vars / no-dupe-keys 等）
  js.configs.recommended,

  // Vue3 官方「必要」规则：只管会出错的写法，不管风格
  // （不用 recommended，它含风格类规则，会和 Prettier 打架）
  ...pluginVue.configs['flat/essential'],

  {
    files: ['**/*.{js,vue}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: {
        ...globals.browser,
      },
    },
    rules: {
      // ESLint 9 把 no-unused-vars 的 caughtErrors 默认值收紧成了 'all'，
      // 于是 `try { … } catch (e) { }` 里那个只为语法存在的 e 会报错。
      // 这类绑定删不删都不影响行为（本项目 26 处），故关掉这一项；
      // 其余未使用变量（导入、局部变量）照旧报错。
      'no-unused-vars': ['error', { caughtErrors: 'none' }],
    },
  },

  {
    // AudioWorklet 处理器跑在 AudioWorkletGlobalScope 里，不是 window：
    // 这两个全局名只在 worklet 文件中可见
    files: ['src/composables/voice/pcm-worklet.js'],
    languageOptions: {
      globals: {
        AudioWorkletProcessor: 'readonly',
        registerProcessor: 'readonly',
      },
    },
  },

  {
    // 构建/工具配置跑在 Node 里（process、__dirname 等）
    files: ['vite.config.js', 'eslint.config.js'],
    languageOptions: {
      globals: {
        ...globals.node,
      },
    },
  },
]
