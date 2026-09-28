/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DEEPSEEK_BASE_URL?: string
  readonly VITE_DEEPSEEK_MODEL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
