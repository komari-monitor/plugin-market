import fs from "node:fs";

const catalogPath = new URL("../v1.json", import.meta.url);
const write = process.argv.includes("--write");
const catalog = JSON.parse(fs.readFileSync(catalogPath, "utf8"));

if (!Array.isArray(catalog.plugins)) {
  throw new Error("v1.json must contain a plugins array");
}

const compareShort = (left, right) => {
  const a = String(left.short).toUpperCase();
  const b = String(right.short).toUpperCase();
  if (a < b) return -1;
  if (a > b) return 1;
  return String(left.short).localeCompare(String(right.short));
};

const seen = new Set();
for (const plugin of catalog.plugins) {
  if (typeof plugin.short !== "string" || !plugin.short) {
    throw new Error("Every plugin must have a non-empty short field");
  }
  const key = plugin.short.toUpperCase();
  if (seen.has(key)) {
    throw new Error(`Duplicate plugin short: ${plugin.short}`);
  }
  seen.add(key);
}

const sorted = [...catalog.plugins].sort(compareShort);
const outOfOrder = catalog.plugins.findIndex((plugin, index) => plugin !== sorted[index]);

if (write) {
  catalog.plugins = sorted;
  fs.writeFileSync(catalogPath, `${JSON.stringify(catalog, null, 2)}\n`);
  console.log("Sorted v1.json by plugins[].short");
  process.exit(0);
}

if (outOfOrder !== -1) {
  throw new Error(
    `plugins is not sorted by short at index ${outOfOrder}: expected ${sorted[outOfOrder].short}, found ${catalog.plugins[outOfOrder].short}. Run: node scripts/check-catalog-order.mjs --write`,
  );
}

console.log(`v1.json is sorted by short (${catalog.plugins.length} plugins)`);
