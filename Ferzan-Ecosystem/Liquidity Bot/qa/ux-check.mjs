#!/usr/bin/env node
/**
 * Ferzan UX check: loads a page in clean, isolated browser contexts (optionally through regional
 * proxies, several in parallel), scrolls it so lazy layout renders, clicks one element, and reports.
 *
 *   node "Liquidity Bot/qa/ux-check.mjs" --url https://ferzan-factory.com --target "Launch a coin"
 *   node "Liquidity Bot/qa/ux-check.mjs" --url https://ferzan-factory.com --target "#launch-btn" --runs 3
 *   node "Liquidity Bot/qa/ux-check.mjs" --url ... --target ... --proxy '{"server":"http://host:8080","username":"u","password":"p"}'
 *   node "Liquidity Bot/qa/ux-check.mjs" --url ... --target ... --proxies proxies.json     # a JSON array of those objects
 *
 * --target is a CSS/XPath selector (starts with # . [ // or contains > : =) or the visible text of a
 * button/link. Exit code 0 only if every run passed. Failures save a screenshot under Liquidity Bot/qa/out/.
 *
 * Scope guard: this is a test of OUR pages. It refuses hosts that are not Ferzan's own, uses the stock browser
 * identity (no spoofing), and caps parallel runs at 5. To check a staging host of yours, set QA_ALLOWED_HOSTS=host1,host2.
 */
import { chromium } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const OUT = path.join(path.dirname(fileURLToPath(import.meta.url)), "out");
const MAX_PARALLEL = 5;
const DEFAULT_HOSTS = ["ferzan-factory.com", "ferzan-factory.grok.me"];

function args(argv) {
  const o = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--")) o[argv[i].slice(2)] = argv[i + 1]?.startsWith("--") || argv[i + 1] === undefined ? "true" : argv[++i];
  }
  return o;
}

function allowedHosts() {
  const extra = (process.env.QA_ALLOWED_HOSTS || "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  return [...DEFAULT_HOSTS, ...extra];
}

function hostAllowed(url) {
  const h = new URL(url).hostname.toLowerCase();
  return allowedHosts().some((a) => h === a || h.endsWith("." + a));
}

function normProxy(p) {
  if (!p) return null;
  if (typeof p === "string") p = JSON.parse(p);
  if (!p.server) throw new Error('proxy needs a "server" like http://host:port');
  const out = { server: /^\w+:\/\//.test(p.server) ? p.server : `http://${p.server}` };
  if (p.username) out.username = p.username;
  if (p.password) out.password = p.password;
  return out;
}

const mask = (p) => (p ? new URL(p.server).host : "direct");
const looksLikeSelector = (t) => /^(#|\.|\[|\/\/)/.test(t) || /[>:=]/.test(t);

async function scrollThrough(page) {
  await page.evaluate(async () => {
    const step = Math.max(200, Math.floor(window.innerHeight * 0.8));
    for (let y = 0; y < document.body.scrollHeight; y += step) {
      window.scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 150));
    }
    window.scrollTo(0, 0);
  });
}

async function runOne(browser, i, { url, target, proxy }) {
  const label = `run ${i + 1} [${mask(proxy)}]`;
  const t0 = Date.now();
  const problems = [];
  let context;
  let page;
  try {
    // A brand-new context per run: no cookies, storage or cache carried over from any other run.
    context = await browser.newContext({
      proxy: proxy || undefined,
      viewport: { width: 1366, height: 800 },
      serviceWorkers: "block",
    });
    page = await context.newPage();
    page.setDefaultTimeout(20000);
    page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
    page.on("requestfailed", (r) => problems.push(`request failed: ${r.url().slice(0, 100)} (${r.failure()?.errorText})`));

    const resp = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
    if (!resp || resp.status() >= 400) throw new Error(`page answered ${resp ? resp.status() : "nothing"}`);
    await page.waitForLoadState("load", { timeout: 15000 }).catch(() => problems.push("load event slow (15s)"));
    await scrollThrough(page);

    const loc = looksLikeSelector(target)
      ? page.locator(target).first()
      : page.getByRole("button", { name: target }).or(page.getByRole("link", { name: target })).or(page.getByText(target, { exact: false })).first();
    await loc.waitFor({ state: "visible", timeout: 15000 });
    await loc.scrollIntoViewIfNeeded();
    await loc.click();
    await page.waitForTimeout(500);
    return { label, ok: true, ms: Date.now() - t0, finalUrl: page.url(), problems };
  } catch (err) {
    let shot = "";
    try {
      fs.mkdirSync(OUT, { recursive: true });
      shot = path.join(OUT, `fail-${Date.now()}-${i + 1}.png`);
      if (page) await page.screenshot({ path: shot, fullPage: true });
    } catch { shot = ""; }
    return { label, ok: false, ms: Date.now() - t0, error: String(err.message || err).split("\n")[0], screenshot: shot, problems };
  } finally {
    await context?.close().catch(() => {});
  }
}

async function main() {
  const a = args(process.argv.slice(2));
  const url = a.url;
  const target = a.target;
  if (!url || !target) {
    console.error('usage: node "Liquidity Bot/qa/ux-check.mjs" --url <https://...> --target "<text or selector>" [--runs N] [--proxy JSON | --proxies file.json]');
    process.exit(2);
  }
  if (!hostAllowed(url)) {
    console.error(`refused: ${new URL(url).hostname} is not one of our hosts (${allowedHosts().join(", ")}). Set QA_ALLOWED_HOSTS for a staging host of yours.`);
    process.exit(2);
  }
  let proxies = [null];
  if (a.proxies) proxies = JSON.parse(fs.readFileSync(a.proxies, "utf8")).map(normProxy);
  else if (a.proxy) proxies = [normProxy(a.proxy)];
  const runs = Math.min(MAX_PARALLEL, Math.max(1, parseInt(a.runs || String(proxies.length), 10) || 1));

  const browser = await chromium.launch({ headless: true });
  let results = [];
  try {
    results = await Promise.all(
      Array.from({ length: runs }, (_, i) => runOne(browser, i, { url, target, proxy: proxies[i % proxies.length] })),
    );
  } finally {
    await browser.close().catch(() => {});
  }

  for (const r of results) {
    console.log(`${r.ok ? "PASS" : "FAIL"} ${r.label} ${r.ms}ms${r.ok ? ` -> ${r.finalUrl}` : ` :: ${r.error}${r.screenshot ? ` (screenshot ${r.screenshot})` : ""}`}`);
    for (const p of r.problems.slice(0, 5)) console.log(`   - ${p}`);
  }
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error("fatal:", e.message || e);
  process.exit(1);
});
