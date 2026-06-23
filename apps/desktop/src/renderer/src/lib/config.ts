/** Backend base URL. Override with VITE_KINORA_API_URL; defaults to local dev. */
export const API_BASE_URL = (
  import.meta.env.VITE_KINORA_API_URL ?? "http://localhost:8000"
).replace(/\/+$/, "");
