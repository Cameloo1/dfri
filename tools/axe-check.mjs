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
  const predictions = await readdir(join(publicationRoot, "scoreboard", "predictions"), {
    withFileTypes: true,
  });
  return [
    "/",
    "/scoreboard/",
    "/companies/",
    "/methodology/",
    "/methodology/coverage/",
    "/methodology/sensitivity/",
    "/changelog/",
    ...companies
      .filter((entry) => entry.isDirectory())
      .map((entry) => `/companies/${entry.name}/`)
      .sort(),
    ...predictions
      .filter((entry) => entry.isDirectory())
      .map((entry) => `/scoreboard/predictions/${entry.name}/`)
      .sort(),
  ];
}

async function semanticAudit(page, route) {
  return page.evaluate((currentRoute) => {
    const failures = [];
    const headings = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")];
    const headingLevels = headings.map((heading) => Number(heading.tagName.slice(1)));
    if (headingLevels.filter((level) => level === 1).length !== 1) {
      failures.push("page must expose exactly one H1");
    }
    for (let index = 1; index < headingLevels.length; index += 1) {
      if (headingLevels[index] > headingLevels[index - 1] + 1) {
        failures.push(`heading level skips from H${headingLevels[index - 1]} to H${headingLevels[index]}`);
        break;
      }
    }
    if (document.querySelectorAll("main#main-content").length !== 1) {
      failures.push("page must expose one main-content landmark");
    }
    const skip = document.querySelector('a.skip-link[href="#main-content"]');
    if (skip == null || skip.textContent.trim() !== "Skip to main content") {
      failures.push("page lacks the named main-content skip link");
    }
    const unnamedLinks = [...document.querySelectorAll("a[href]")].filter(
      (link) => !(link.getAttribute("aria-label") || link.textContent || "").trim(),
    );
    if (unnamedLinks.length > 0) failures.push(`${unnamedLinks.length} link(s) lack a name`);
    const unnamedImages = [...document.querySelectorAll('svg[role="img"]')].filter(
      (svg) => !(svg.getAttribute("aria-label") || svg.querySelector("title")?.textContent || "").trim(),
    );
    if (unnamedImages.length > 0) failures.push(`${unnamedImages.length} SVG image(s) lack a name`);
    const expectedCurrent =
      currentRoute === "/scoreboard/"
        ? "Scoreboard"
        : currentRoute === "/companies/" || currentRoute.startsWith("/companies/")
          ? "Companies"
          : currentRoute.startsWith("/methodology/")
            ? "Methodology"
            : currentRoute === "/changelog/"
              ? "Changelog"
              : null;
    const currentLinks = [...document.querySelectorAll('nav a[aria-current="page"]')].map((link) =>
      link.textContent.trim(),
    );
    if (expectedCurrent == null && currentLinks.length > 0) {
      failures.push("navigation exposes a false current-page state");
    }
    if (expectedCurrent != null && (currentLinks.length !== 1 || currentLinks[0] !== expectedCurrent)) {
      failures.push(`navigation does not identify ${expectedCurrent} as the current page`);
    }
    return { failures, headingLevels };
  }, route);
}

async function keyboardAudit(page) {
  const expectedCount = await page.evaluate(() => {
    const candidates = [...document.querySelectorAll(
      'a[href],button,summary,input,select,textarea,[tabindex]:not([tabindex="-1"])',
    )];
    return candidates.filter((element) => {
      const style = getComputedStyle(element);
      const tabIndex = element.tabIndex;
      const closedDetails = element.closest("details:not([open])");
      const hiddenByDisclosure =
        closedDetails != null && element !== closedDetails.querySelector(":scope > summary");
      return (
        tabIndex >= 0 &&
        !element.matches(":disabled") &&
        !hiddenByDisclosure &&
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        element.getClientRects().length > 0
      );
    }).length;
  });
  const failures = [];
  for (let expectedIndex = 0; expectedIndex < expectedCount; expectedIndex += 1) {
    await page.keyboard.press("Tab");
    const state = await page.evaluate(() => {
      const candidates = [...document.querySelectorAll(
        'a[href],button,summary,input,select,textarea,[tabindex]:not([tabindex="-1"])',
      )].filter((element) => {
        const style = getComputedStyle(element);
        const closedDetails = element.closest("details:not([open])");
        const hiddenByDisclosure =
          closedDetails != null && element !== closedDetails.querySelector(":scope > summary");
        return (
          element.tabIndex >= 0 &&
          !element.matches(":disabled") &&
          !hiddenByDisclosure &&
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          element.getClientRects().length > 0
        );
      });
      const active = document.activeElement;
      const style = active == null ? null : getComputedStyle(active);
      return {
        index: candidates.indexOf(active),
        label: (active?.getAttribute("aria-label") || active?.textContent || "").replace(/\s+/g, " ").trim(),
        outlineStyle: style?.outlineStyle ?? "none",
        outlineWidth: Number.parseFloat(style?.outlineWidth ?? "0"),
      };
    });
    if (state.index !== expectedIndex) {
      failures.push(`tab order diverges at item ${expectedIndex + 1}`);
      break;
    }
    if (!state.label) failures.push(`focus item ${expectedIndex + 1} lacks an accessible name`);
    if (state.outlineStyle === "none" || state.outlineWidth <= 0) {
      failures.push(`focus item ${expectedIndex + 1} lacks a visible focus outline`);
    }
  }
  await page.goto(page.url(), { waitUntil: "load" });
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  const skipResult = await page.evaluate(() => ({
    hash: window.location.hash,
    activeId: document.activeElement?.id ?? "",
  }));
  if (skipResult.hash !== "#main-content" || skipResult.activeId !== "main-content") {
    failures.push("skip link does not move location and focus to main-content");
  }
  return { failures, focusableCount: expectedCount };
}

async function mobileLayoutAudit(page) {
  return page.evaluate(() => {
    const tolerance = 1;
    const viewportWidth = window.innerWidth;
    const documentWidth = document.documentElement.scrollWidth;
    const failures = [];
    if (documentWidth > viewportWidth + tolerance) {
      failures.push(`document overflows by ${documentWidth - viewportWidth}px`);
    }
    const overflowingSvgs = [...document.querySelectorAll('svg[role="img"]')].filter((svg) => {
      const bounds = svg.getBoundingClientRect();
      return bounds.left < -tolerance || bounds.right > viewportWidth + tolerance;
    });
    if (overflowingSvgs.length > 0) {
      failures.push(`${overflowingSvgs.length} SVG image(s) leave the mobile viewport`);
    }
    return { failures, viewportWidth, documentWidth };
  });
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
    const semantics = await semanticAudit(page, route);
    const keyboard = await keyboardAudit(page);
    results.push({
      route,
      semantics,
      keyboard,
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
    if (route === "/") {
      const disclosure = noJsPage.locator("details.baseline-disclosure");
      if ((await disclosure.count()) !== 1 || (await disclosure.getAttribute("open")) != null) {
        throw new Error("No-JavaScript baseline disclosure does not begin as one collapsed group");
      }
      await disclosure.locator("summary").click();
      if ((await disclosure.getAttribute("open")) == null) {
        throw new Error("No-JavaScript baseline disclosure did not expand natively");
      }
      const baselineRows = await disclosure.locator('tr[data-lift-status="baseline-only"]').count();
      if (baselineRows !== 38) {
        throw new Error(`No-JavaScript baseline disclosure exposes ${baselineRows} rows, not 38`);
      }
    }
  }
  await noJsContext.close();

  const mobileContext = await browser.newContext({
    javaScriptEnabled: false,
    viewport: { width: 390, height: 844 },
  });
  const mobilePage = await mobileContext.newPage();
  for (const result of results) {
    await mobilePage.goto(`${baseUrl}${result.route}`, { waitUntil: "load" });
    result.mobileLayout = await mobileLayoutAudit(mobilePage);
  }
  await mobileContext.close();
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
const semanticFailures = results.flatMap((result) =>
  result.semantics.failures.map((failure) => ({ route: result.route, failure })),
);
const keyboardFailures = results.flatMap((result) =>
  result.keyboard.failures.map((failure) => ({ route: result.route, failure })),
);
const mobileLayoutFailures = results.flatMap((result) =>
  result.mobileLayout.failures.map((failure) => ({ route: result.route, failure })),
);
const receipt = {
  status:
    critical.length === 0 &&
    semanticFailures.length === 0 &&
    keyboardFailures.length === 0 &&
    mobileLayoutFailures.length === 0
      ? "PASS"
      : "FAIL",
  axeVersion: "4.12.1",
  browserChannel,
  pageCount: pageRoutes.length,
  noJavaScriptPageCount: pageRoutes.length,
  criticalViolationCount: critical.length,
  criticalViolations: critical,
  semanticFailureCount: semanticFailures.length,
  semanticFailures,
  keyboardFailureCount: keyboardFailures.length,
  keyboardFailures,
  mobileLayoutFailureCount: mobileLayoutFailures.length,
  mobileLayoutFailures,
  worstFinding:
    critical[0] ??
    semanticFailures[0] ??
    keyboardFailures[0] ??
    mobileLayoutFailures[0] ??
    "none",
  results,
};
await writeFile(outputPath, `${JSON.stringify(receipt, null, 2)}\n`, { encoding: "utf8" });
process.stdout.write(`${JSON.stringify(receipt)}\n`);
if (receipt.status !== "PASS") process.exitCode = 1;
