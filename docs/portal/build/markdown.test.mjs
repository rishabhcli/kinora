/**
 * Tests for the tiny Markdown renderer. Run with `node --test docs/portal/build`.
 * Uses the built-in node:test runner — zero dependencies.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { renderMarkdown, renderInline, slugify, escapeHtml } from "./markdown.mjs";

test("escapeHtml escapes the dangerous set", () => {
  assert.equal(escapeHtml('<a href="x">&'), "&lt;a href=&quot;x&quot;&gt;&amp;");
});

test("slugify produces github-style slugs", () => {
  assert.equal(slugify("Errors & Retries"), "errors--retries".replace("--", "-"));
  assert.equal(slugify("The six-agent architecture"), "the-six-agent-architecture");
});

test("headings get id anchors and are collected", () => {
  const { html, headings } = renderMarkdown("# Title\n\n## Section one");
  assert.match(html, /<h1 id="title">Title<\/h1>/);
  assert.match(html, /<h2 id="section-one">Section one<\/h2>/);
  assert.equal(headings.length, 2);
  assert.equal(headings[1].id, "section-one");
});

test("inline code, bold, italic, and links", () => {
  const out = renderInline("call `foo()` then **bold** and *italic* see [docs](a.html)");
  assert.match(out, /<code>foo\(\)<\/code>/);
  assert.match(out, /<strong>bold<\/strong>/);
  assert.match(out, /<em>italic<\/em>/);
  assert.match(out, /<a href="a\.html">docs<\/a>/);
});

test("inline code content is not double-parsed", () => {
  const out = renderInline("`a < b && c`");
  assert.match(out, /<code>a &lt; b &amp;&amp; c<\/code>/);
});

test("fenced code block with language class", () => {
  const { html } = renderMarkdown("```ts\nconst x = 1 < 2;\n```");
  assert.match(html, /<pre><code class="language-ts">const x = 1 &lt; 2;<\/code><\/pre>/);
});

test("unordered and ordered lists", () => {
  const ul = renderMarkdown("- a\n- b").html;
  assert.match(ul, /<ul><li>a<\/li><li>b<\/li><\/ul>/);
  const ol = renderMarkdown("1. one\n2. two").html;
  assert.match(ol, /<ol><li>one<\/li><li>two<\/li><\/ol>/);
});

test("GFM pipe table", () => {
  const md = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |";
  const { html } = renderMarkdown(md);
  assert.match(html, /<table>/);
  assert.match(html, /<th>A<\/th><th>B<\/th>/);
  assert.match(html, /<td>1<\/td><td>2<\/td>/);
  assert.match(html, /<td>3<\/td><td>4<\/td>/);
});

test("blockquote", () => {
  const { html } = renderMarkdown("> a wise note\n> continued");
  assert.match(html, /<blockquote>a wise note continued<\/blockquote>/);
});

test("horizontal rule", () => {
  assert.match(renderMarkdown("---").html, /<hr \/>/);
});

test("paragraphs separated by blank lines", () => {
  const { html } = renderMarkdown("first para\n\nsecond para");
  assert.match(html, /<p>first para<\/p>/);
  assert.match(html, /<p>second para<\/p>/);
});

test("a heading does not get swallowed into a table check", () => {
  // A line with a pipe but no separator row stays a paragraph.
  const { html } = renderMarkdown("a | b is not a table");
  assert.match(html, /<p>a \| b is not a table<\/p>/);
});
