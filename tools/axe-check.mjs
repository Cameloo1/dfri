import { createServer } from "node:http";
import { readFile, readdir, stat, writeFile } from "node:fs/promises";
import { extname, join, normalize, relative, resolve, sep } from "node:path";
import process from "node:process";

import AxeBuilder from "@axe-core/playwright";
import { chromium } from "playwright-core";

const publicationRoot = resolve(process.argv[2] ?? "published/public");
const outputPath = resolve(process.argv[3] ?? ".local/evidence/m4-axe.json");
const browserChannel = process.env.DFRI_AXE_BROWSER_CHANNEL ?? "chrome";
const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".parquet": "application/octet-stream",
};

function safePath(urlPath) {
  const decoded = decodeURIComponent(urlPath.split("?", 1)[0]);
  const requested = decoded.endsWith("/") ? `${decoded}index.html` : decoded;
  const candidate = resolve(publicationRoot, `.${normalize(requested)}`);
  const inside = relative(publicationRoot, candidate);
  if (inside.startsWith(`..${sep}`) || inside === "..") {
    throw new Error("Request escaped the publication root");
  }
  return candidate;
}

async function routes() {
  const companies = await readdir(join(publicationRoot, "companies"), { withFileTypes: true });
  return [
    "/",
    "/scoreboard/",
    "/methodology/",
    "/methodology/coverage/",
    "/methodology/sensitivity/",
    "/changelog/",
    ...companies
      .filter((entry) => entry.isDirectory())
      .map((entry) => `/companies/${entry.name}/`)
      .sort(),
  ];
}

const server = createServer(async (request, response) => {
  try {
    const path = safePath(request.url ?? "/");
    const metadata = await stat(path);
    if (!metadata.isFile()) throw new Error("Not a file");
    response.writeHead(200, {
      "Content-Type": contentTypes[extname(path)] ?? "application/octet-stream",
      "Cache-Control": "no-store",
    });
    response.end(await readFile(path));
  } catch {
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("Not found");
  }
});

await new Promise((resolveListening) => server.listen(0, "127.0.0.1", resolveListening));
const address = server.address();
if (address == null || typeof address === "string") throw new Error("No local server port");
const baseUrl = `http://127.0.0.1:${address.port}`;
const pageRoutes = await routes();
const browser = await chromium.launch({ channel: browserChannel, headless: true });
const results = [];
try {
  const context = await browser.newContext();
  const page = await context.newPage();
  for (const route of pageRoutes) {
    await page.goto(`${baseUrl}${route}`, { waitUntil: "load" });
    const audit = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();
    results.push({
      route,
      violations: audit.violations.map((violation) => ({
        id: violation.id,
        impact: violation.impact,
        nodes: violation.nodes.length,
        help: violation.help,
        helpUrl: violation.helpUrl,
      })),
    });
  }
  await context.close();

  const noJsContext = await browser.newContext({ javaScriptEnabled: false });
  const noJsPage = await noJsContext.newPage();
  for (const route of pageRoutes) {
    const response = await noJsPage.goto(`${baseUrl}${route}`, { waitUntil: "load" });
    const text = await noJsPage.locator("main").innerText();
    if (response == null || response.status() !== 200 || text.replace(/\s+/g, " ").trim().length < 100) {
      throw new Error(`No-JavaScript content gate failed for ${route}`);
    }
  }
  await noJsContext.close();
} finally {
  await browser.close();
  await new Promise((resolveClosed, rejectClosed) =>
    server.close((error) => (error == null ? resolveClosed() : rejectClosed(error))),
  );
}

const critical = results.flatMap((result) =>
  result.violations
    .filter((violation) => violation.impact === "critical")
    .map((violation) => ({ route: result.route, ...violation })),
);
const receipt = {
  status: critical.length === 0 ? "PASS" : "FAIL",
  axeVersion: "4.12.1",
  browserChannel,
  pageCount: pageRoutes.length,
  noJavaScriptPageCount: pageRoutes.length,
  criticalViolationCount: critical.length,
  criticalViolations: critical,
  results,
};
await writeFile(outputPath, `${JSON.stringify(receipt, null, 2)}\n`, { encoding: "utf8" });
process.stdout.write(`${JSON.stringify(receipt)}\n`);
if (critical.length > 0) process.exitCode = 1;
