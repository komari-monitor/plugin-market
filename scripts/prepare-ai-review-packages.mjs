import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const MAX_DOWNLOAD_SIZE = 100 << 20;

function argument(name) {
  const prefix = `${name}=`;
  const value = process.argv.find((item) => item.startsWith(prefix))?.slice(prefix.length);
  if (!value) throw new Error(`Missing ${name}`);
  return value;
}

function catalogAt(ref) {
  return JSON.parse(execFileSync("git", ["show", `${ref}:v1.json`], { encoding: "utf8" }));
}

async function downloadPackage(url) {
  const response = await fetch(url, {
    headers: { "User-Agent": "komari-plugin-market-ai-reviewer" },
  });
  if (!response.ok) throw new Error(`download returned HTTP ${response.status}`);
  const contentLength = Number(response.headers.get("content-length") || 0);
  if (contentLength > MAX_DOWNLOAD_SIZE) throw new Error("download exceeds the catalog size limit");
  if (!response.body) throw new Error("download response has no body");

  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > MAX_DOWNLOAD_SIZE) throw new Error("download exceeds the catalog size limit");
    chunks.push(value);
  }
  return Buffer.concat(chunks);
}

function changedPlugins(baseCatalog, catalog) {
  const previous = new Map(baseCatalog.plugins.map((plugin) => [plugin.short.toUpperCase(), plugin]));
  return catalog.plugins.filter((plugin) => {
    const existing = previous.get(plugin.short.toUpperCase());
    return !existing || JSON.stringify(existing) !== JSON.stringify(plugin);
  });
}

const baseSha = argument("--base-sha");
const headSha = argument("--head-sha");
const directory = path.resolve(argument("--directory"));
const catalog = catalogAt(headSha);
const baseCatalog = catalogAt(baseSha);
const plugins = changedPlugins(baseCatalog, catalog).filter((plugin) => plugin.download && plugin.sha256);

if (plugins.length === 0) throw new Error("No changed installable plugins found in v1.json");
fs.rmSync(directory, { recursive: true, force: true });
fs.mkdirSync(directory, { recursive: true });

const targets = [];
for (const plugin of plugins) {
  const packageData = await downloadPackage(plugin.download);
  const sha256 = createHash("sha256").update(packageData).digest("hex");
  if (sha256 !== plugin.sha256) throw new Error(`${plugin.short}: download does not match catalog SHA-256`);
  const packageName = `${plugin.short}.zip`;
  fs.writeFileSync(path.join(directory, packageName), packageData);
  targets.push({
    short: plugin.short,
    version: plugin.version,
    source: plugin.url,
    package: packageName,
    sha256,
    komari: plugin.komari || "",
  });
}

fs.writeFileSync(
  path.join(directory, "review-targets.json"),
  `${JSON.stringify({ targets }, null, 2)}\n`,
  "utf8",
);
console.log(`Prepared ${targets.length} verified plugin package(s) for AI review`);
