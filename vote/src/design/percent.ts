export type Counts = { ja: number; nein: number };

// integer percentages that sum to 100 (largest remainder); 0/0 when there are no votes
export function percents({ ja, nein }: Counts): { ja: number; nein: number } {
  const total = ja + nein;
  if (total <= 0) return { ja: 0, nein: 0 };
  const pja = Math.round((ja * 100) / total);
  return { ja: pja, nein: 100 - pja };
}

export function tokenValues(c: Counts): Record<string, string> {
  const p = percents(c);
  return {
    pct_ja: String(p.ja),
    pct_nein: String(p.nein),
    count_ja: String(c.ja),
    count_nein: String(c.nein),
    total: String(c.ja + c.nein),
  };
}

export function fillTokens(text: string, values: Record<string, string>): string {
  return text.replace(/\{(\w+)\}/g, (m, k: string) => (k in values ? values[k] : m));
}
