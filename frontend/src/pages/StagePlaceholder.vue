<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { implementedStages, stageById } from '../stages'

// 未实现阶段的占位页：进度轨上任何一格都点得动，但不会给一个空白页面。
const route = useRoute()
const id = computed(() => Number(route.params.id ?? 0))
const meta = computed(() => stageById(id.value))
const ready = implementedStages()
const pad = (n: number) => String(n).padStart(2, '0')
</script>

<template>
  <section class="placeholder">
    <p class="eyebrow">阶段 {{ pad(id) }}</p>
    <h1 class="title">{{ meta?.label ?? '未知阶段' }}</h1>
    <p class="lead">
      这一阶段的界面还没有实现——它会在撰写该阶段教程时一起落地。
      进度轨上的每一格都能点，因为课程本身就是按阶段推进的。
    </p>

    <div class="ready">
      <p class="ready-label">现在就能用的能力</p>
      <ul class="links">
        <li v-for="s in ready" :key="s.id">
          <RouterLink class="link" :to="s.path!">
            <span class="link-no">{{ pad(s.id) }}</span>
            <span class="link-label">{{ s.label }}</span>
          </RouterLink>
        </li>
      </ul>
    </div>
  </section>
</template>

<style scoped>
.placeholder {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-width: 62ch;
  padding: 26px 0;
}
.eyebrow {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.14em;
  color: var(--signal);
  text-transform: uppercase;
}
.title {
  margin: 0;
  font-family: var(--font-display);
  font-size: 30px;
  line-height: 1.2;
  color: var(--paper);
}
.lead {
  margin: 0;
  font-size: 14px;
  line-height: 1.8;
  color: var(--muted);
}

.ready { margin-top: 10px; }
.ready-label {
  margin: 0 0 10px;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: uppercase;
}
.links {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.link {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 10px 14px;
  text-decoration: none;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  transition: border-color 0.15s ease;
}
.link:hover { border-color: var(--data); }
.link-no {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--data);
}
.link-label { font-size: 13.5px; color: var(--paper); }
</style>
