/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_KINORA_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
