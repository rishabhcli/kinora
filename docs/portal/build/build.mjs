#!/usr/bin/env node
/**
 * Build the Kinora docs portal — a dependency-light static site.
 *
 *   node docs/portal/build/build.mjs            # build to docs/portal/dist
 *   node docs/portal/build/build.mjs --check     # build to a temp dir, verify, discard
 *
 * Pipeline: read docs/portal/content/*.md + nav.json, render each via the tiny
 * Markdown engine, wrap in the theme shell, and emit one HTML file per page. The
 * `api-reference` page is generated from the source-of-truth catalog (not a .md
 * file). Zero npm dependencies — no framework, no build-approval churn.
 */
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { mkdirSync, readFileSync, readdirSync, writeFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { renderMarkdown } from "./markdown.mjs";
import { renderPage } from "./theme.mjs";
import { renderApiReference } from "./apiref.mjs";
import { API_VERSION } from "../../../clients/spec/catalog.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const PORTAL = join(HERE, "..");
const CONTENT = join(PORTAL, "content");

function loadNav() {
  return JSON.parse(readFileSync(join(CONTENT, "nav.json"), "utf8"));
}

/** All slugs in the nav, in order. */
function navSlugs(nav) {
  return nav.flatMap((g) => g.items.map((i) => i.slug));
}

function titleForSlug(nav, slug) {
  for (const g of nav) {
    for (const i of g.items) if (i.slug === slug) return i.title;
  }
  return slug;
}

function buildSite(outDir) {
  mkdirSync(outDir, { recursive: true });
  const nav = loadNav();
  const slugs = navSlugs(nav);

  // Validate that every markdown file is in the nav and vice-versa (catches
  // an authored page that was never linked, or a nav entry with no content).
  const mdFiles = readdirSync(CONTENT)
    .filter((f) => f.endsWith(".md"))
    .map((f) => f.replace(/\.md$/, ""));
  const generated = new Set(["api-reference"]);
  const missingContent = slugs.filter((s) => !generated.has(s) && !mdFiles.includes(s));
  if (missingContent.length) {
    throw new Error(`nav references pages with no content: ${missingContent.join(", ")}`);
  }
  const unlinked = mdFiles.filter((s) => !slugs.includes(s));
  if (unlinked.length) {
    throw new Error(`content pages not in nav.json: ${unlinked.join(", ")}`);
  }

  let pageCount = 0;
  for (const slug of slugs) {
    let contentHtml;
    if (slug === "api-reference") {
      contentHtml = renderApiReference();
    } else {
      const md = readFileSync(join(CONTENT, `${slug}.md`), "utf8");
      contentHtml = renderMarkdown(md).html;
    }
    const html = renderPage({
      title: titleForSlug(nav, slug),
      contentHtml,
      nav,
      activeSlug: slug,
      version: API_VERSION,
    });
    writeFileSync(join(outDir, `${slug}.html`), html);
    pageCount++;
  }
  return { pageCount, slugs };
}

function main() {
  const check = process.argv.includes("--check");
  const outDir = check ? join(tmpdir(), `kinora-docs-${Date.now()}`) : join(PORTAL, "dist");
  if (!check && existsSync(outDir)) rmSync(outDir, { recursive: true, force: true });
  const { pageCount, slugs } = buildSite(outDir);
  if (check) {
    // Sanity-check every page rendered to non-trivial HTML, then clean up.
    for (const slug of slugs) {
      const html = readFileSync(join(outDir, `${slug}.html`), "utf8");
      if (!html.includes("<!DOCTYPE html>") || html.length < 400) {
        throw new Error(`page ${slug} rendered suspiciously small`);
      }
    }
    rmSync(outDir, { recursive: true, force: true });
    console.log(`docs build OK — ${pageCount} pages render cleanly (checked, then discarded).`);
  } else {
    console.log(`Built ${pageCount} pages to ${outDir}`);
  }
}

main();
