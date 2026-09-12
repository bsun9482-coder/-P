<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="brand brand-center">
        <div class="brand-avatar">
          <img src="/assets/avatar_small.png" alt="小P" @error="onAvatarError" />
        </div>
        <div>
          <div class="brand-name">面试官小P</div>
          <div class="brand-status"><i></i>模拟面试 · 辅导答疑</div>
        </div>
      </div>

      <el-tabs v-model="tab" class="login-tabs" stretch>
        <el-tab-pane label="登录" name="login">
          <el-form @submit.prevent="onSubmit" label-position="top">
            <el-form-item label="用户名">
              <el-input
                v-model="form.username"
                placeholder="请输入用户名"
                :prefix-icon="User"
                autocomplete="username"
              />
            </el-form-item>
            <el-form-item label="密码">
              <el-input
                v-model="form.password"
                type="password"
                placeholder="请输入密码"
                :prefix-icon="Lock"
                show-password
                autocomplete="current-password"
                @keyup.enter="onSubmit"
              />
            </el-form-item>
            <el-button type="primary" class="login-btn" :loading="loading" @click="onSubmit">
              登 录
            </el-button>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="注册" name="register">
          <el-form @submit.prevent="onSubmit" label-position="top">
            <el-form-item label="用户名">
              <el-input
                v-model="form.username"
                placeholder="3-32 个字符（登录名）"
                :prefix-icon="User"
                autocomplete="username"
              />
            </el-form-item>
            <el-form-item label="昵称（选填）">
              <el-input v-model="form.nickname" placeholder="显示名，默认与用户名相同" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input
                v-model="form.password"
                type="password"
                placeholder="至少 6 位"
                :prefix-icon="Lock"
                show-password
                autocomplete="new-password"
                @keyup.enter="onSubmit"
              />
            </el-form-item>
            <el-button type="primary" class="login-btn" :loading="loading" @click="onSubmit">
              注册并登录
            </el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>

      <p class="login-tip">登录后，你的面试记录与收藏将按账号保存，跨设备同步。</p>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lock, User } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const tab = ref('login')
const loading = ref(false)
const form = reactive({ username: '', password: '', nickname: '' })

function onAvatarError(e) {
  e.target.src = '' // 回退为空，由 CSS 兜底
}

async function onSubmit() {
  const username = form.username.trim()
  if (!username) {
    ElMessage.warning('请输入用户名')
    return
  }
  // 与后端 Pydantic 约束对齐（3-32），避免 422 结构错误原文弹出（bug #27）
  if (username.length < 3 || username.length > 32) {
    ElMessage.warning('用户名需 3-32 个字符')
    return
  }
  if (form.password.length < 6) {
    ElMessage.warning('密码至少 6 位')
    return
  }
  if (tab.value === 'register' && form.nickname && form.nickname.length > 32) {
    ElMessage.warning('昵称最多 32 个字符')
    return
  }
  loading.value = true
  try {
    if (tab.value === 'login') {
      await auth.login(username, form.password)
      ElMessage.success(`欢迎回来，${auth.nickname}`)
    } else {
      await auth.register({
        username,
        password: form.password,
        nickname: form.nickname.trim(),
      })
      ElMessage.success(`注册成功，欢迎 ${auth.nickname}`)
    }
    router.push(route.query.redirect || '/')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '操作失败，请重试')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  min-height: 100vh; /* 旧浏览器回退（bug #31） */
  min-height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.login-card {
  width: min(400px, 100%);
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 30px 30px 22px;
  box-shadow: var(--shadow-float);
}
.login-tabs {
  margin-top: 18px;
}
.login-btn {
  width: 100%;
  margin-top: 4px;
  min-height: 40px; /* 触控基准高度（规范 §9.2） */
  border-radius: 10px;
}
.login-tip {
  margin-top: 14px;
  font-size: 12px;
  color: var(--muted);
  text-align: center;
}
</style>
