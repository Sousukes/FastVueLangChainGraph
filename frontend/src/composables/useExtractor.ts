import { ref, computed } from 'vue'

export type FieldType = 'string' | 'int' | 'number' | 'bool' | 'array'

export interface ExtractField {
  id: string           // 前端 key，与后端无关
  name: string
  type: FieldType
  description: string
  required: boolean
  /** 仅 string 类型生效，逗号分隔的合法取值列表 */
  enum: string[]
}

export interface ExtractResult {
  data: Record<string, unknown> | null
  raw: string
  attempts: number
  valid: boolean
  model: string
  error?: string | null
  warning?: string | null
}

const newId = () =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`

/** 内置预设：让用户先看到"结构化抽取到底在干什么" */
export const PRESETS: Array<{ name: string; text: string; fields: Omit<ExtractField, 'id'>[] }> = [
  {
    name: '新闻',
    text:
      '新华社北京 2026 年 3 月 15 日电——国家航天局今日宣布，嫦娥七号探测器已于 14 日成功完成环月轨道修正，预计下月初实施近月制动。消息引发广泛关注，多位专家在接受采访时表示，此次任务将为后续月球科研站选址提供关键数据。',
    fields: [
      { name: 'title', type: 'string', description: '一句话新闻标题', required: true, enum: [] },
      { name: 'source', type: 'string', description: '媒体来源', required: false, enum: [] },
      { name: 'date', type: 'string', description: '发布日期 YYYY-MM-DD', required: false, enum: [] },
      { name: 'tags', type: 'array', description: '关键词标签', required: false, enum: [] },
      { name: 'summary', type: 'string', description: '50 字以内的摘要', required: true, enum: [] },
      { name: 'sentiment', type: 'string', description: '情感倾向', required: false, enum: ['positive', 'neutral', 'negative'] },
    ],
  },
  {
    name: '简历',
    text: '李雷，1992 年生于上海，2014 年毕业于复旦大学计算机科学与技术专业，硕士学历。曾任字节跳动高级工程师（2018-2023），熟悉 Python / Go / Kubernetes；2023 年加入某创业公司担任 CTO，主导 AIGC 中台建设。',
    fields: [
      { name: 'name', type: 'string', description: '姓名', required: true, enum: [] },
      { name: 'birth_year', type: 'int', description: '出生年份', required: false, enum: [] },
      { name: 'degree', type: 'string', description: '最高学历', required: false, enum: [] },
      { name: 'companies', type: 'array', description: '工作过的公司', required: false, enum: [] },
      { name: 'skills', type: 'array', description: '关键技能', required: false, enum: [] },
    ],
  },
]

/**
 * 阶段 04：结构化抽取。前端只做三件事——
 *  1. 维护字段定义（FieldSpec[]），调用后端 /api/extract
 *  2. 展示 data：成功 → 表格化 / JSON 折叠；失败 → 原始 raw + 校验错误
 *  3. 状态徽章：attempts / valid / warning
 */
export function useExtractor(options: { apiBase?: string } = {}) {
  const apiBase =
    options.apiBase ?? ((import.meta.env.VITE_API_BASE as string | undefined) || '/api')

  const text = ref('')
  const fields = ref<ExtractField[]>([])
  const loading = ref(false)
  const result = ref<ExtractResult | null>(null)
  const error = ref<string | null>(null)

  /** 把 internal 字段定义 → 后端契约 */
  const payload = computed(() => ({
    text: text.value.trim(),
    fields: fields.value.map((f) => ({
      name: f.name,
      type: f.type,
      description: f.description,
      required: f.required,
      enum: f.type === 'string' && f.enum.length ? f.enum : null,
    })),
  }))

  const canRun = computed(
    () => !!text.value.trim() && fields.value.length > 0 && !loading.value,
  )

  function applyPreset(p: (typeof PRESETS)[number]) {
    text.value = p.text
    fields.value = p.fields.map((f) => ({ id: newId(), ...f, enum: [...(f.enum ?? [])] }))
    result.value = null
    error.value = null
  }

  function addField() {
    fields.value.push({
      id: newId(),
      name: '',
      type: 'string',
      description: '',
      required: true,
      enum: [],
    })
  }

  function removeField(id: string) {
    fields.value = fields.value.filter((f) => f.id !== id)
  }

  function setEnumText(field: ExtractField, csv: string) {
    field.enum = csv.split(',').map((s) => s.trim()).filter(Boolean)
  }

  async function run() {
    if (!canRun.value) return
    loading.value = true
    error.value = null
    try {
      const resp = await fetch(`${apiBase}/extract`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload.value),
      })
      if (!resp.ok) {
        const detail = await resp.text().catch(() => '')
        throw new Error(`HTTP ${resp.status} · ${detail.slice(0, 240)}`)
      }
      result.value = (await resp.json()) as ExtractResult
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  function reset() {
    text.value = ''
    fields.value = []
    result.value = null
    error.value = null
  }

  return {
    text,
    fields,
    loading,
    result,
    error,
    canRun,
    applyPreset,
    addField,
    removeField,
    setEnumText,
    run,
    reset,
  }
}