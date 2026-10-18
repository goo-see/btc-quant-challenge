// Responsive check against the live dashboard (read-only GETs). node scripts_viewport_check.mjs
import { chromium } from 'playwright';
const widths = [360, 390, 430, 768, 1024, 1440, 1920];
const tabs = ['홈', '거래', '연구', '시스템'];
const browser = await chromium.launch();
let allOk = true;
for (const w of widths) {
  const page = await browser.newPage({ viewport: { width: w, height: 900 }, deviceScaleFactor: 1 });
  await page.goto('http://127.0.0.1:8787/', { waitUntil: 'networkidle' });
  const results = [];
  for (const t of tabs) {
    await page.getByRole('button', { name: t, exact: true }).first().click();
    await page.waitForTimeout(700);
    await page.waitForLoadState('networkidle');
    const r = await page.evaluate(() => {
      const sw = document.documentElement.scrollWidth, vw = window.innerWidth;
      const probs = [];
      const within = (el, parent, tag) => {
        const a = el.getBoundingClientRect(), b = parent.getBoundingClientRect();
        if (a.right > b.right + 1 || a.left < b.left - 1) probs.push(`${tag} overflow: ${Math.round(a.right - b.right)}px (${(el.textContent || '').slice(0, 30)})`);
      };
      document.querySelectorAll('.cands .cand').forEach((c) => within(c, c.parentElement, 'cand'));
      document.querySelectorAll('.scard .kv .v, .scard .big .v, .card .v, .rung, .scard-head').forEach((v) => { const card = v.closest('.scard, .card'); if (card) within(v, card, v.className); });
      document.querySelectorAll('.scard, .card, .panel, .hero2').forEach((c) => within(c, document.body, 'card-vs-body'));
      return { sw, vw, probs: probs.slice(0, 5), cands: document.querySelectorAll('.cands .cand').length };
    });
    const ok = r.sw <= r.vw && r.probs.length === 0;
    if (!ok) allOk = false;
    results.push(`${t}: scroll ${r.sw}/${r.vw} ${ok ? 'OK' : 'FAIL'}${r.cands ? ` cands=${r.cands}` : ''}${r.probs.length ? ' ' + r.probs.join(' | ') : ''}`);
  }
  await page.getByRole('button', { name: '연구', exact: true }).first().click();
  await page.waitForTimeout(500);
  await page.screenshot({ path: `/tmp/dash_${w}_research.png`, fullPage: true });
  await page.getByRole('button', { name: '홈', exact: true }).first().click();
  await page.waitForTimeout(500);
  await page.screenshot({ path: `/tmp/dash_${w}_home.png`, fullPage: false });
  console.log(`${w}px -> ${results.every((x) => x.includes(' OK')) ? 'PASS' : 'FAIL'} | ${results.join(' || ')}`);
  await page.close();
}
await browser.close();
console.log(allOk ? 'ALL_PASS' : 'SOME_FAIL');
