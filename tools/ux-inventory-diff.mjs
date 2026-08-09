import { readFile, writeFile } from "node:fs/promises";

const [beforePath, afterPath, outputPath] = process.argv.slice(2);
if (!beforePath || !afterPath || !outputPath) {
  throw new Error(
    "Usage: node tools/ux-inventory-diff.mjs BEFORE.md AFTER.md OUTPUT.md",
  );
}

const parseInventory = async (path) => {
  const source = await readFile(path, "utf8");
  const lines = source.split(/\r?\n/);
  const baseUrl = lines
    .find((line) => line.startsWith("- Rendered site:"))
    ?.match(/<([^>]+)>/)?.[1];
  if (!baseUrl) {
    throw new Error(`${path} does not declare its rendered site`);
  }

  const pages = new Map();
  let route = null;
  let section = null;
  for (const line of lines) {
    const routeMatch = line.match(/^## Page \d+: `(.+)`$/);
    if (routeMatch) {
      route = routeMatch[1];
      pages.set(route, { lines: [], internalLinks: [], outboundLinks: [] });
      section = null;
      continue;
    }
    if (!route) {
      continue;
    }
    if (line === "### User-visible information with native disclosures expanded") {
      section = "lines";
      continue;
    }
    if (line === "### Internal links") {
      section = "internalLinks";
      continue;
    }
    if (line === "### Outbound links") {
      section = "outboundLinks";
      continue;
    }
    if (line.startsWith("### ")) {
      section = null;
      continue;
    }
    const page = pages.get(route);
    if (section === "lines") {
      const match = line.match(/^\d+\. (.*)$/);
      if (match) {
        page.lines.push(match[1]);
      }
    } else if (section === "internalLinks" || section === "outboundLinks") {
      const match = line.match(/^- .* — <([^>]+)>$/);
      if (match) {
        page[section].push(match[1]);
      }
    }
  }
  return { baseUrl, pages };
};

const uniqueSorted = (values) => [...new Set(values)].sort();
const difference = (left, right) => {
  const rightSet = new Set(right);
  return left.filter((value) => !rightSet.has(value));
};
const normalizeInternal = (url, baseUrl) => {
  if (!url.startsWith(baseUrl)) {
    return url;
  }
  return `/${url.slice(baseUrl.length)}`.replace(/^\/\//, "/");
};

const before = await parseInventory(beforePath);
const after = await parseInventory(afterPath);
const beforeRoutes = [...before.pages.keys()].sort();
const afterRoutes = [...after.pages.keys()].sort();
const missingRoutes = difference(beforeRoutes, afterRoutes);
const addedRoutes = difference(afterRoutes, beforeRoutes);

const beforeLines = [...before.pages.values()].flatMap((page) => page.lines);
const afterLines = [...after.pages.values()].flatMap((page) => page.lines);
const beforeUniqueLines = uniqueSorted(beforeLines);
const afterUniqueLines = uniqueSorted(afterLines);
const missingUniqueLines = difference(beforeUniqueLines, afterUniqueLines);
const addedUniqueLines = difference(afterUniqueLines, beforeUniqueLines);

const beforeInternal = uniqueSorted(
  [...before.pages.values()]
    .flatMap((page) => page.internalLinks)
    .map((url) => normalizeInternal(url, before.baseUrl)),
);
const afterInternal = uniqueSorted(
  [...after.pages.values()]
    .flatMap((page) => page.internalLinks)
    .map((url) => normalizeInternal(url, after.baseUrl)),
);
const missingInternal = difference(beforeInternal, afterInternal);
const addedInternal = difference(afterInternal, beforeInternal);

const beforeOutbound = uniqueSorted(
  [...before.pages.values()].flatMap((page) => page.outboundLinks),
);
const afterOutbound = uniqueSorted(
  [...after.pages.values()].flatMap((page) => page.outboundLinks),
);
const missingOutbound = difference(beforeOutbound, afterOutbound);
const addedOutbound = difference(afterOutbound, beforeOutbound);

const homeBefore = before.pages.get("/")?.lines ?? [];
const homeAfter = after.pages.get("/")?.lines ?? [];
const baselineSentence =
  "No company-specific financing evidence found; estimate reflects proportional allocation.";
const count = (values, value) => values.filter((candidate) => candidate === value).length;

const list = (values) => (values.length ? values.map((value) => `- ${value}`) : ["- None"]);
const markdown = [
  "# UX inventory diff",
  "",
  "Status: **MACHINE DIFF COMPLETE — DISPOSITION REQUIRED BELOW**",
  "",
  `- Before inventory: \`${beforePath}\``,
  `- After inventory: \`${afterPath}\``,
  `- Routes: ${beforeRoutes.length} before, ${afterRoutes.length} after`,
  `- Disclosure-expanded information lines: ${beforeLines.length} before, ${afterLines.length} after`,
  `- Distinct information lines: ${beforeUniqueLines.length} before, ${afterUniqueLines.length} after`,
  `- Distinct outbound targets: ${beforeOutbound.length} before, ${afterOutbound.length} after`,
  "",
  "## Route changes",
  "",
  `Missing routes (${missingRoutes.length}):`,
  ...list(missingRoutes),
  "",
  `Added routes (${addedRoutes.length}):`,
  ...list(addedRoutes),
  "",
  "## Information-line changes",
  "",
  `Missing distinct lines (${missingUniqueLines.length}):`,
  ...list(missingUniqueLines),
  "",
  `Added distinct lines (${addedUniqueLines.length}):`,
  ...list(addedUniqueLines),
  "",
  `The repeated baseline interpretation sentence appears ${count(homeBefore, baselineSentence)} time(s) on the old homepage and ${count(homeAfter, baselineSentence)} time(s) on the new homepage.`,
  "",
  "## Link-target changes",
  "",
  `Missing normalized internal targets (${missingInternal.length}):`,
  ...list(missingInternal),
  "",
  `Added normalized internal targets (${addedInternal.length}):`,
  ...list(addedInternal),
  "",
  `Missing outbound targets (${missingOutbound.length}):`,
  ...list(missingOutbound),
  "",
  `Added outbound targets (${addedOutbound.length}):`,
  ...list(addedOutbound),
  "",
].join("\n");

await writeFile(outputPath, `${markdown.trimEnd()}\n`, "utf8");
process.stdout.write(
  `${JSON.stringify({
    beforeRoutes: beforeRoutes.length,
    afterRoutes: afterRoutes.length,
    missingRoutes: missingRoutes.length,
    addedRoutes: addedRoutes.length,
    missingUniqueLines: missingUniqueLines.length,
    addedUniqueLines: addedUniqueLines.length,
    missingInternal: missingInternal.length,
    addedInternal: addedInternal.length,
    missingOutbound: missingOutbound.length,
    addedOutbound: addedOutbound.length,
  })}\n`,
);
