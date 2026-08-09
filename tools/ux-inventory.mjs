import { chromium } from "playwright-core";
import { writeFile } from "node:fs/promises";

const [output, baseUrlArgument, sourceReference, phase] = process.argv.slice(2);
if (!output || !baseUrlArgument || !sourceReference || !["before", "after"].includes(phase)) {
  throw new Error(
    "Usage: node tools/ux-inventory.mjs OUTPUT BASE_URL SOURCE_REFERENCE before|after",
  );
}

const baseUrl = new URL(baseUrlArgument).href;
const [companyFeedResponse, scoreboardFeedResponse] = await Promise.all([
  fetch(new URL("v2/feeds/dfri_companies.json", baseUrl)),
  fetch(new URL("v1/feeds/scoreboard.json", baseUrl)),
]);
if (!companyFeedResponse.ok || !scoreboardFeedResponse.ok) {
  throw new Error("Unable to load the route registries");
}

const companyFeed = await companyFeedResponse.json();
const scoreboardFeed = await scoreboardFeedResponse.json();
const companyRows = companyFeed.data ?? companyFeed.rows ?? companyFeed.companies;
const scoreboardRows = scoreboardFeed.data ?? scoreboardFeed.rows ?? scoreboardFeed.predictions;
if (!Array.isArray(companyRows) || !Array.isArray(scoreboardRows)) {
  throw new Error("Route registries do not expose the expected rows");
}

const routes = [
  "",
  "scoreboard/",
  "companies/",
  "methodology/",
  "methodology/coverage/",
  "methodology/sensitivity/",
  "changelog/",
  ...companyRows.map((row) => `companies/${String(row.ticker).toLowerCase()}/`).sort(),
  ...scoreboardRows
    .map((row) => `scoreboard/predictions/${String(row.prediction_id)}/`)
    .sort(),
];
if (new Set(routes).size !== routes.length) {
  throw new Error("The route inventory contains duplicate pages");
}

const browser = await chromium.launch({ channel: "chrome", headless: true });
const pages = [];
try {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  for (const route of routes) {
    const expected = new URL(route, baseUrl).href;
    const response = await page.goto(expected, { waitUntil: "load" });
    if (response?.status() !== 200 || page.url() !== expected) {
      throw new Error(`Route did not resolve directly: ${expected}`);
    }
    const inventory = await page.evaluate(
      ({ siteRoot }) => {
        const lines = (document.body?.innerText ?? "")
          .split("\n")
          .map((line) => line.replace(/\s+/g, " ").trim())
          .filter(Boolean);
        const headings = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")].map(
          (heading) => ({
            level: Number(heading.tagName.slice(1)),
            text: (heading.innerText ?? "").replace(/\s+/g, " ").trim(),
          }),
        );
        const links = [...document.querySelectorAll("a[href]")]
          .map((link) => ({
            label: (link.innerText ?? "").replace(/\s+/g, " ").trim() || "(unlabeled)",
            url: link.href,
          }))
          .filter((link) => link.url)
          .filter(
            (link, index, all) =>
              all.findIndex(
                (candidate) => candidate.label === link.label && candidate.url === link.url,
              ) === index,
          );
        return {
          title: document.title,
          headings,
          lines,
          internalLinks: links.filter((link) => link.url.startsWith(siteRoot)),
          outboundLinks: links.filter((link) => !link.url.startsWith(siteRoot)),
        };
      },
      { siteRoot: baseUrl },
    );
    pages.push({ route: `/${route}`, url: expected, ...inventory });
  }
  await context.close();
} finally {
  await browser.close();
}

const escapeText = (value) =>
  String(value)
    .replaceAll("\\", "\\\\")
    .replaceAll("`", "\\`")
    .replaceAll("*", "\\*")
    .replaceAll("_", "\\_")
    .replaceAll("[", "\\[")
    .replaceAll("]", "\\]")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");

const capturedAt = new Date().toISOString();
const allInternalTargets = [
  ...new Set(pages.flatMap((page) => page.internalLinks.map((link) => link.url))),
].sort();
const allOutboundTargets = [
  ...new Set(pages.flatMap((page) => page.outboundLinks.map((link) => link.url))),
].sort();
const markdown = [
  `# UX inventory ${phase} the information-architecture overhaul`,
  "",
  `Status: **${phase === "before" ? "BASELINE CAPTURED" : "AFTER CAPTURED"}**`,
  "",
  `- Rendered site: <${baseUrl}>`,
  `- Source reference: \`${sourceReference}\``,
  `- Captured at: \`${capturedAt}\``,
  `- HTML routes: ${pages.length}`,
  `- Distinct internal link targets: ${allInternalTargets.length}`,
  `- Distinct outbound link targets: ${allOutboundTargets.length}`,
  "- Capture mode: rendered HTML with JavaScript disabled. Visible lines remain in DOM reading order; repeated lines remain repeated so later loss or deduplication is detectable.",
  "",
  "## Route index",
  "",
  ...pages.map((page) => `- <${page.url}> — ${escapeText(page.title)}`),
  "",
  "## Distinct internal link targets",
  "",
  ...allInternalTargets.map((url) => `- <${url}>`),
  "",
  "## Distinct outbound link targets",
  "",
  ...allOutboundTargets.map((url) => `- <${url}>`),
  "",
  ...pages.flatMap((page, index) => [
    `## Page ${index + 1}: \`${page.route}\``,
    "",
    `- URL: <${page.url}>`,
    `- Title: ${escapeText(page.title)}`,
    `- Visible information lines: ${page.lines.length}`,
    `- Internal links: ${page.internalLinks.length}`,
    `- Outbound links: ${page.outboundLinks.length}`,
    "",
    "### Heading outline",
    "",
    ...(page.headings.length
      ? page.headings.map((heading) => `- H${heading.level}: ${escapeText(heading.text)}`)
      : ["- None"]),
    "",
    "### User-visible information",
    "",
    ...page.lines.map((line, lineIndex) => `${lineIndex + 1}. ${escapeText(line)}`),
    "",
    "### Internal links",
    "",
    ...(page.internalLinks.length
      ? page.internalLinks.map((link) => `- ${escapeText(link.label)} — <${link.url}>`)
      : ["- None"]),
    "",
    "### Outbound links",
    "",
    ...(page.outboundLinks.length
      ? page.outboundLinks.map((link) => `- ${escapeText(link.label)} — <${link.url}>`)
      : ["- None"]),
    "",
  ]),
].join("\n");

await writeFile(output, `${markdown}\n`, "utf8");
process.stdout.write(
  `${JSON.stringify({
    pages: pages.length,
    internalTargets: allInternalTargets.length,
    outboundTargets: allOutboundTargets.length,
    capturedAt,
  })}\n`,
);
