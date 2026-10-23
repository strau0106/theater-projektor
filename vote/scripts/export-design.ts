// npm run export-design -- <dir>: writes manifest.json, template.svg and fonts/ (same bundle as /api/design)
import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { designBundle, fontPath, manifest } from "../src/design/bundle";
import { FONTS } from "../src/design/tokens";

const out = process.argv[2];
if (!out) {
  console.error("usage: npm run export-design -- <dir>");
  process.exit(2);
}
mkdirSync(path.join(out, "fonts"), { recursive: true });
const m = { ...manifest(), template: "template.svg", fonts: manifest().fonts.map((f) => ({ ...f, url: `fonts/${f.file}` })) };
writeFileSync(path.join(out, "manifest.json"), JSON.stringify(m, null, 2) + "\n");
writeFileSync(path.join(out, "template.svg"), designBundle().template);
for (const f of FONTS) copyFileSync(fontPath(f.file), path.join(out, "fonts", f.file));
console.log(`design ${m.version} -> ${out}`);
