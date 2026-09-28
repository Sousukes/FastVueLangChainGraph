<script setup lang="ts">
// 路由页面是薄包装：真正的能力在组件里（阶段 06 起项目并入主线，一个壳 + 多路由）。
import StreamChat from '../components/StreamChat.vue'
</script>

<template>
  <StreamChat :stage="3" title="多轮对话与流式" />
</template>
