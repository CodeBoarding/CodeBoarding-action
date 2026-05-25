/**
 * Headless render of the CodeBoarding diff diagram.
 *
 * Boots a static server for the webview-ui dist, opens it in headless
 * Chromium, injects the analysis + commit-diff via postMessage in the same
 * shape `e2e/ui/07-commitDiff.pw.ts` uses, waits for the graph to render
 * with diff styling applied, and writes a PNG.
 */
import { chromium } from 'playwright';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, cur, i, arr) => {
    if (cur.startsWith('--')) acc.push([cur.slice(2), arr[i + 1]]);
    return acc;
  }, [])
);

const required = ['webview-dir', 'analysis', 'diff', 'out'];
for (const k of required) {
  if (!args[k]) {
    console.error(`Missing required arg: --${k}`);
    process.exit(2);
  }
}

const webviewDir = path.resolve(args['webview-dir']);
const analysisPath = path.resolve(args['analysis']);
const diffPath = path.resolve(args['diff']);
const outPath = path.resolve(args['out']);
const port = Number(args['port'] || 4567);
const width = Number(args['width'] || 1600);
const height = Number(args['height'] || 1000);

function serveDir(dir, port, analysisJsonPath) {
  const mime = {
    '.html': 'text/html', '.js': 'application/javascript', '.mjs': 'application/javascript',
    '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.ico': 'image/x-icon',
    '.woff': 'font/woff', '.woff2': 'font/woff2', '.map': 'application/json',
  };
  const server = http.createServer((req, res) => {
    let urlPath = decodeURIComponent(req.url.split('?')[0]);
    if (urlPath === '/' || urlPath === '') urlPath = '/index.html';

    // Intercept /sample-analysis.json — browser-dev-mock fetches this on
    // boot when __BROWSER_DEV__ is set, then postMessages it as
    // 'analysis-loaded'. By serving our real analysis here we let the
    // webview boot the same way it does in dev mode.
    if (urlPath === '/sample-analysis.json' && analysisJsonPath) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      fs.createReadStream(analysisJsonPath).pipe(res);
      return;
    }

    const filePath = path.join(dir, urlPath);
    if (!filePath.startsWith(dir)) { res.writeHead(403).end(); return; }
    fs.stat(filePath, (err, stat) => {
      if (err || !stat.isFile()) {
        const fallback = path.join(dir, 'index.html');
        if (fs.existsSync(fallback)) {
          res.writeHead(200, { 'Content-Type': 'text/html' });
          fs.createReadStream(fallback).pipe(res);
        } else {
          res.writeHead(404).end();
        }
        return;
      }
      const ext = path.extname(filePath).toLowerCase();
      res.writeHead(200, { 'Content-Type': mime[ext] || 'application/octet-stream' });
      fs.createReadStream(filePath).pipe(res);
    });
  });
  return new Promise((resolve) => server.listen(port, () => resolve(server)));
}

async function main() {
  const diff = JSON.parse(fs.readFileSync(diffPath, 'utf8'));

  if (diff && diff.error) {
    console.error(`Diff has no base data (${diff.error}); render aborted.`);
    process.exit(3);
  }

  const server = await serveDir(webviewDir, port, analysisPath);
  console.log(`Serving ${webviewDir} on http://127.0.0.1:${port} (analysis at /sample-analysis.json)`);

  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  try {
    const context = await browser.newContext({
      viewport: { width, height },
      deviceScaleFactor: 2,
    });
    const page = await context.newPage();

    page.on('console', (msg) => console.log(`[browser ${msg.type()}]`, msg.text()));
    page.on('pageerror', (err) => console.log('[browser pageerror]', err.message));

    // No init script — let the index.html inline stub define the vscode API
    // AND set __BROWSER_DEV__=true. The browser-dev-mock will then fetch
    // /sample-analysis.json (which we serve as our real analysis) and post
    // it as 'analysis-loaded' through the normal dev-mode path.
    await page.goto(`http://127.0.0.1:${port}/index.html`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#root', { timeout: 10_000 });

    // 2. Wait for React Flow nodes — short timeout, then capture DOM
    //    state for debugging instead of dying silently.
    let nodesAppeared = false;
    try {
      await page.waitForSelector('.react-flow__node', { timeout: 15_000 });
      nodesAppeared = true;
      console.log('React Flow nodes appeared.');
    } catch {
      console.log('::warning::No .react-flow__node within 15s — dumping DOM for diagnosis.');
      const dom = await page.evaluate(() => {
        const root = document.getElementById('root');
        return {
          rootHTMLLen: root ? root.innerHTML.length : 0,
          rootHTMLHead: root ? root.innerHTML.slice(0, 3000) : '(no #root)',
          hasReactFlow: !!document.querySelector('.react-flow'),
          knownSelectors: {
            welcome: !!document.querySelector('[data-testid^="welcome"], .welcome-card, [class*="Welcome"]'),
            demoBanner: !!document.querySelector('[class*="demo"]'),
            outdated: !!document.querySelector('[class*="outdated"]'),
          },
        };
      });
      console.log('DOM diagnosis:', JSON.stringify(dom, null, 2));
    }

    if (nodesAppeared) {
      // 3. Inject the diff result — this applies the diff_status classes
      await page.evaluate((diffResult) => {
        window.postMessage({ type: 'commit-diff-result', diffResult }, '*');
      }, diff);
      try {
        await page.waitForFunction(() => {
          const sels = ['.commit-diff-added', '.commit-diff-deleted', '.commit-diff-modified', '.commit-diff-unchanged'];
          return sels.some((s) => document.querySelector(s) !== null);
        }, null, { timeout: 10_000 });
      } catch {
        console.log('No diff classes appeared — proceeding with screenshot anyway (diff may be empty).');
      }

      // Hide UI chrome that obscures the graph in a static screenshot:
      // welcome overlay, demo banner, help/tour overlay, side controls.
      await page.addStyleTag({
        content: `
          [data-testid="welcome-overlay"],
          .welcome-overlay__backdrop,
          [data-testid="demo-banner"],
          .demo-banner,
          .analysis-controls,
          .view-controls,
          .help-tour-button,
          .help-h-indicator,
          .help-instructions,
          [data-tour-target],
          .commit-picker,
          .commit-timeline,
          .branch-state-banner,
          .outdated-banner { display: none !important; }
          .react-flow { background: transparent !important; }
        `,
      });

      // Re-fit so the graph fills the viewport for the screenshot.
      // React Flow exposes its store on .react-flow via a known internal,
      // but the simplest approach is to dispatch a custom event the app
      // already wires up, or just click any present fit button. Fallback:
      // do nothing and trust the auto-layout.
      await page.evaluate(() => {
        const btn = document.querySelector('[aria-label*="fit" i], [title*="fit" i]');
        if (btn) btn.click();
      });

      await page.waitForTimeout(1500);
    }

    if (nodesAppeared) {
      // Crop the screenshot to the union of node bounding rects + padding,
      // so the PNG is just the diagram (no empty canvas, no chrome).
      // Playwright clip is in CSS px and respects deviceScaleFactor.
      const pad = 80;
      const rect = await page.evaluate((pad) => {
        const nodes = document.querySelectorAll('.react-flow__node');
        if (nodes.length === 0) return null;
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (const n of nodes) {
          const r = n.getBoundingClientRect();
          if (r.left < minX) minX = r.left;
          if (r.top < minY) minY = r.top;
          if (r.right > maxX) maxX = r.right;
          if (r.bottom > maxY) maxY = r.bottom;
        }
        const vw = window.innerWidth, vh = window.innerHeight;
        const x = Math.max(0, Math.floor(minX - pad));
        const y = Math.max(0, Math.floor(minY - pad));
        const width = Math.min(vw - x, Math.ceil(maxX - minX + pad * 2));
        const height = Math.min(vh - y, Math.ceil(maxY - minY + pad * 2));
        return { x, y, width, height };
      }, pad);

      if (rect && rect.width > 0 && rect.height > 0) {
        console.log(`Cropping to node bounds + ${pad}px pad: ${rect.width}x${rect.height} at (${rect.x},${rect.y})`);
        await page.screenshot({ path: outPath, clip: rect, omitBackground: false });
      } else {
        console.log('Could not compute node bounds; falling back to full .react-flow screenshot.');
        const target = (await page.$('.react-flow')) || (await page.$('body'));
        await target.screenshot({ path: outPath, omitBackground: false });
      }
    } else {
      // No nodes — capture whatever state the page ended up in for debug.
      const target = (await page.$('#root')) || (await page.$('body'));
      await target.screenshot({ path: outPath, omitBackground: false });
    }
    console.log(`Wrote ${outPath} (nodes_appeared=${nodesAppeared})`);
    if (!nodesAppeared) process.exit(4);
  } finally {
    await browser.close();
    server.close();
  }
}

main().catch((e) => {
  console.error('Render failed:', e);
  process.exit(1);
});
