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

function serveDir(dir, port) {
  const mime = {
    '.html': 'text/html', '.js': 'application/javascript', '.mjs': 'application/javascript',
    '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.ico': 'image/x-icon',
    '.woff': 'font/woff', '.woff2': 'font/woff2', '.map': 'application/json',
  };
  const server = http.createServer((req, res) => {
    let urlPath = decodeURIComponent(req.url.split('?')[0]);
    if (urlPath === '/' || urlPath === '') urlPath = '/index.html';
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
  const analysis = JSON.parse(fs.readFileSync(analysisPath, 'utf8'));
  const diff = JSON.parse(fs.readFileSync(diffPath, 'utf8'));

  if (diff && diff.error) {
    console.error(`Diff has no base data (${diff.error}); render aborted.`);
    process.exit(3);
  }

  const server = await serveDir(webviewDir, port);
  console.log(`Serving ${webviewDir} on http://127.0.0.1:${port}`);

  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  try {
    const context = await browser.newContext({
      viewport: { width, height },
      deviceScaleFactor: 2,
    });
    const page = await context.newPage();

    // Pre-define acquireVsCodeApi so the index.html inline script's
    // ``typeof acquireVsCodeApi === 'undefined'`` check is false. That
    // skips both the mock stub AND the __BROWSER_DEV__ flag, so the
    // dev-mode sample-analysis fetch never fires and overwrites our data.
    await page.addInitScript(() => {
      // eslint-disable-next-line no-undef
      window.acquireVsCodeApi = () => ({
        postMessage: () => {},
        getState: () => ({}),
        setState: () => {},
      });
    });

    page.on('console', (msg) => console.log(`[browser ${msg.type()}]`, msg.text()));
    page.on('pageerror', (err) => console.log('[browser pageerror]', err.message));

    await page.goto(`http://127.0.0.1:${port}/index.html`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#root', { timeout: 10_000 });
    await page.waitForTimeout(500);

    // 1. Load the analysis (the "after" / PR-head state)
    await page.evaluate((data) => {
      window.postMessage({
        type: 'analysis-loaded',
        data,
        isDemoAnalysis: false,
        isOutdatedAnalysis: false,
      }, '*');
    }, analysis);

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
      await page.waitForTimeout(1500);
    }

    // Always screenshot — even when nodes never showed we want to see
    // what state the webview ended up in.
    const target = (await page.$('.react-flow')) || (await page.$('#root')) || (await page.$('body'));
    await target.screenshot({ path: outPath, omitBackground: false, fullPage: false });
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
