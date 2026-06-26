// Reading preferences moved to the a11y layer (it is reading-accessibility state,
// owned by Agent 06). This shim keeps existing importers (`@/lib/readingPrefs`)
// working unchanged. Prefer importing from `@/a11y/readingPrefs` in new code.
export * from "@/a11y/readingPrefs";
