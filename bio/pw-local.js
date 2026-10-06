// Preload (node -r ./pw-local.js): point Playwright at the Chromium build installed
// on this machine instead of the revision bundled with the npm package.
const pw = require('playwright');
const launch = pw.chromium.launch.bind(pw.chromium);
pw.chromium.launch = (o = {}) => launch({ executablePath: process.env.CHROME_PATH || '/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell', ...o });
// The skill's scripts open a 1920x1080 page; size it to the clip instead (VIEWPORT=720x1280).
if (process.env.VIEWPORT) {
  const [vw, vh] = process.env.VIEWPORT.split('x').map(Number);
  const l2 = pw.chromium.launch;
  pw.chromium.launch = async (o) => { const b = await l2(o); const np = b.newPage.bind(b);
    b.newPage = (po = {}) => np({ ...po, viewport: { width: vw, height: vh } }); return b; };
}
