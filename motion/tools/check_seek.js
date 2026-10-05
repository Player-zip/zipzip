// Compares every overlay's visual state under sequential seeking vs. the
// strided seeking each parallel render worker does. Any mismatch = flicker.
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell' });
  const mk = async () => { const p = await b.newPage({ viewport: { width: 1920, height: 1080 } }); await p.goto('file://' + process.cwd() + '/hf/index.html'); await p.waitForTimeout(400); return p; };
  const snap = (p, frames) => p.evaluate((frames) => {
    const tl = window.__timelines.main;
    const els = [...document.querySelectorAll('#root *')].filter(e => e.id && !['base','root'].includes(e.id));
    const res = {};
    for (const f of frames) {
      tl.seek(f / 30, false);
      res[f] = els.map(e => { const c = getComputedStyle(e); return e.id + ':' + c.visibility[0] + (+c.opacity).toFixed(2) + ':' + c.transform + ':' + c.clipPath; }).join('|');
    }
    return res;
  }, frames);
  const N = 2060, all = [...Array(N).keys()];
  const pa = await mk(); const seq = await snap(pa, all);
  let bad = 0;
  for (const off of [1, 2, 3]) {
    const pb = await mk(); const st = await snap(pb, all.filter(f => f % 4 === off));
    for (const f of Object.keys(st)) {
      if (st[f] !== seq[f]) {
        const A = seq[f].split('|'), B = st[f].split('|');
        A.forEach((a, i) => { if (a !== B[i] && bad < 12) { bad++; console.log('frame', f, '\n  seq   ', a.slice(0, 140), '\n  stride', B[i].slice(0, 140)); } });
        if (bad >= 12) break;
      }
    }
  }
  console.log(bad ? 'MISMATCHES' : 'OK: strided seeking matches sequential');
  await b.close();
})();
