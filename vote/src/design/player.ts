import { type Counts, fillTokens, tokenValues } from "./percent";
import { TIMING as T } from "./tokens";

const seg = (t: number, start: number, dur: number) => Math.min(1, Math.max(0, (t - start) / dur));

type State = "hidden" | "in" | "shown" | "out";

/** Drives the template inside an inline <svg> with the animation timings from docs/results.md. */
export class ResultsPlayer {
  private labels: { el: Element; text: string }[];
  private paras: Element[]; // <g id="para-N"> in order, or the whole labels layer
  private ops: number[];
  private overlay = 0;
  private counts: Counts | null = null;
  private state: State = "hidden";
  private raf = 0;

  constructor(private svg: SVGSVGElement) {
    const layer = svg.querySelector("#layer-labels")!;
    const ps = [...layer.querySelectorAll(":scope > [id^='para-']")].sort((a, b) => +a.id.slice(5) - +b.id.slice(5));
    this.paras = ps.length ? ps : [layer];
    this.ops = this.paras.map(() => 0);
    // remember the template text on the nodes so a re-created player starts from the original
    this.labels = [...layer.querySelectorAll("text, tspan")].map((el) => {
      const e = el as SVGElement;
      e.dataset.tpl ??= el.textContent ?? "";
      return { el, text: e.dataset.tpl };
    });
    this.draw();
  }

  private draw() {
    this.svg.style.opacity = String(this.overlay);
    this.paras.forEach((p, i) => p.setAttribute("opacity", String(this.ops[i])));
  }

  private set(c: Counts) {
    this.counts = c;
    const v = tokenValues(c);
    // only leaf nodes (a <text> holding <tspan>s keeps its children)
    for (const l of this.labels) if (l.el.children.length === 0) l.el.textContent = fillTokens(l.text, v);
  }

  private play(dur: number, frame: (t: number) => void, done: () => void) {
    cancelAnimationFrame(this.raf);
    const t0 = performance.now();
    const tick = (now: number) => {
      const t = (now - t0) / 1000;
      frame(Math.min(t, dur));
      this.draw();
      if (t >= dur) done();
      else this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }

  show(c: Counts) {
    this.set(c);
    this.state = "in";
    this.play(
      T.inOverlay + (this.paras.length - 1) * T.paraGap + T.inPara,
      (t) => {
        this.overlay = seg(t, 0, T.inOverlay);
        this.ops = this.paras.map((_, i) => seg(t, T.inOverlay + i * T.paraGap, T.inPara));
      },
      () => (this.state = "shown"),
    );
  }

  /** Out from whatever is visible now (also mid-reveal): all text together, overlay overlapping the end. */
  hide() {
    if (this.state === "hidden") return;
    this.state = "out";
    const o0 = this.overlay;
    const p0 = [...this.ops];
    const end = Math.max(T.outText, T.outOverlayAt + T.outOverlay);
    this.play(
      end,
      (t) => {
        this.ops = p0.map((p) => p * (1 - seg(t, 0, T.outText)));
        this.overlay = o0 * (1 - seg(t, T.outOverlayAt, T.outOverlay));
      },
      () => (this.state = "hidden"),
    );
  }

  /** New counts: the text crossfades (old out, new in) when shown, else switches at once. */
  update(c: Counts) {
    if (this.counts && c.ja === this.counts.ja && c.nein === this.counts.nein) return;
    if (this.state !== "shown") return this.set(c);
    const half = T.update / 2;
    let swapped = false;
    this.play(
      T.update,
      (t) => {
        if (t >= half && !swapped) (swapped = true), this.set(c);
        const k = t < half ? 1 - seg(t, 0, half) : seg(t, half, half);
        this.ops = this.paras.map(() => k);
      },
      () => {},
    );
  }

  destroy() {
    cancelAnimationFrame(this.raf);
  }
}
