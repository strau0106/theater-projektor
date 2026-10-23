import { HEIGHT, SLIDE, SLIDE_COUNT, WIDTH } from "./tokens";

/**
 * Projector/web graphic: a black Keynote-style text slide, centred, one `<g id="para-N">` per paragraph
 * (revealed one after the other). No bars: `layer-bars` is optional in the contract.
 */
export function buildTemplate(): string {
  const n = SLIDE.paragraphs.reduce((a, p) => a + p.length, 0) + SLIDE.paragraphs.length - 1; // lines incl. blank ones
  // first baseline so that the ink (cap top of the first line .. last baseline) is centred vertically
  const y0 = (HEIGHT - (n - 1) * SLIDE.lineHeight + SLIDE.size * SLIDE.capHeight) / 2;
  let row = 0;
  const paras = SLIDE.paragraphs.map((lines, p) => {
    const spans = lines.map((l) => {
      const t = l.replace("{x}", `{${SLIDE_COUNT.x}}`).replace("{y}", `{${SLIDE_COUNT.y}}`);
      return `<tspan x="${WIDTH / 2}" y="${y0 + row++ * SLIDE.lineHeight}">${t}</tspan>`;
    });
    row++; // blank line between paragraphs
    return `<g id="para-${p + 1}">
<text text-anchor="middle" font-family="Arimo" font-weight="bold" font-size="${SLIDE.size}" fill="#FFFFFF">
${spans.join("\n")}
</text>
</g>`;
  });
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${WIDTH}" height="${HEIGHT}" viewBox="0 0 ${WIDTH} ${HEIGHT}">
<g id="layer-static">
<rect width="${WIDTH}" height="${HEIGHT}" fill="#000000"/>
</g>
<g id="layer-labels">
${paras.join("\n")}
</g>
</svg>
`;
}
