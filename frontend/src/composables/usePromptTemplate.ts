import { ref, computed, reactive } from 'vue'

export interface PromptPreset {
  name: string
  hint: string
  body: string
}

// 阶段 02 示范：三种典型 Prompt 技法。变量用 {{name}} 占位。
export const PRESETS: PromptPreset[] = [
  {
    name: '角色设定',
    hint: '用 system 式人设约束输出口吻与受众',
    body: '你是一位资深的{{领域}}科普作者，面向{{读者}}。\n请用{{语气}}的口吻解释下面的概念：\n\n{{主题}}',
  },
  {
    name: '少样本',
    hint: '给 1–2 个输入输出样例，让模型照章办事',
    body: '下面是「{{任务}}」的示例：\n示例1：{{示例输入1}} → {{示例输出1}}\n示例2：{{示例输入2}} → {{示例输出2}}\n\n现在请按同样格式处理：\n{{待处理}}',
  },
  {
    name: '结构化约束',
    hint: '强制 JSON 等机器可读格式，减少自由发挥',
    body: '请把「{{主题}}」整理为 JSON，字段为 title、points（字符串数组）、summary。\n只输出 JSON，不要任何解释文字。\n内容：{{内容}}',
  },
]

const VAR_RE = /\{\{\s*([\w一-龥]+)\s*\}\}/g

/** 从模板中提取去重后的变量名列表 */
export function extractVariables(template: string): string[] {
  const found = new Set<string>()
  let m: RegExpExecArray | null
  VAR_RE.lastIndex = 0
  while ((m = VAR_RE.exec(template))) found.add(m[1])
  return [...found]
}

/** 用 values 渲染模板；未填值的变量保留 {{name}} 占位，便于发现遗漏 */
export function renderTemplate(
  template: string,
  values: Record<string, string>,
): string {
  return template.replace(VAR_RE, (_, name: string) => {
    const v = values[name]
    return v !== undefined && v.trim() !== '' ? v : `{{${name}}}`
  })
}

/**
 * 阶段 02：模板实验台的状态核心。
 * template 为可编辑模板；values 按变量名存取值；rendered 为实时渲染结果。
 */
export function usePromptTemplate(initial = '') {
  const template = ref(initial)
  const values = reactive<Record<string, string>>({})
  const variables = computed(() => extractVariables(template.value))
  const rendered = computed(() => renderTemplate(template.value, values))
  return { template, values, variables, rendered }
}
