/// <reference types="vite/client" />

// 显式声明使用到的环境变量，避免 import.meta.env 退化为 any
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
