import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { buildTemplate } from "./template";
import { FONTS, HEIGHT, PARAGRAPH_MODE, WIDTH } from "./tokens";

const root = () => process.cwd();
export const fontPath = (file: string) => path.join(root(), "fonts", file);

let cache: { template: string; version: string } | undefined;

export function loadFont(file: string): Buffer | null {
  const f = FONTS.find((x) => x.file === file);
  return f ? readFileSync(fontPath(f.file)) : null;
}

/** Self-contained template.svg plus the content hash of template and fonts. */
export function designBundle() {
  if (!cache) {
    const template = buildTemplate();
    const h = createHash("sha256").update(template).update(PARAGRAPH_MODE);
    for (const f of FONTS) h.update(readFileSync(fontPath(f.file)));
    cache = { template, version: h.digest("hex").slice(0, 12) };
  }
  return cache;
}

export function manifest() {
  return {
    version: designBundle().version,
    width: WIDTH,
    height: HEIGHT,
    paragraph_mode: PARAGRAPH_MODE,
    template: "design/template.svg",
    fonts: FONTS.map((f) => ({ family: f.family, file: f.file, url: `design/fonts/${f.file}` })),
  };
}
