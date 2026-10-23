import { type Counts, fillTokens, tokenValues } from "./percent";
import { PARAGRAPH_MODE, TIMING as T } from "./tokens";

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
  private t0 = 0;
  private xf: { start: number; c: Counts; swapped: boolean } | null = null;

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

  /** Opacity of paragraph i at t seconds after show() (overlay fade included in the offset). */
  private sched(i: number, t: number): number {
    const n = this.paras.length;
    if (PARAGRAPH_MODE === "stack") return seg(t, T.inOverlay + i * T.paraHold, T.inPara);
    const s = T.inOverlay + i * (T.paraHold + T.outText + T.paraGap);
    const up = seg(t, s, T.inPara);
    return i === n - 1 ? up : up * (1 - seg(t, s + T.paraHold, T.outText));
  }

  private seqEnd(): number {
    const n = this.paras.length;
    const gap = PARAGRAPH_MODE === "stack" ? T.paraHold : T.paraHold + T.outText + T.paraGap;
    return T.inOverlay + (n - 1) * gap + T.inPara;
  }

  show(c: Counts) {
    this.set(c);
    this.state = "in";
    this.xf = null;
    this.t0 = performance.now();
    this.run();
  }

  /** Timeline loop: overlay + paragraph schedule (+ live-update crossfade) until the sequence is over. */
  private run() {
    cancelAnimationFrame(this.raf);
    const tick = (now: number) => {
      const t = (now - this.t0) / 1000;
      this.overlay = seg(t, 0, T.inOverlay);
      let k = 1;
      if (this.xf) {
        const u = (now - this.xf.start) / 1000;
        if (u >= T.update / 2 && !this.xf.swapped) (this.xf.swapped = true), this.set(this.xf.c);
        k = u < T.update / 2 ? 1 - seg(u, 0, T.update / 2) : seg(u, T.update / 2, T.update / 2);
        if (u >= T.update) this.xf = null;
      }
      this.ops = this.paras.map((_, i) => this.sched(i, t) * k);
      this.draw();
      if (t >= this.seqEnd() && !this.xf) this.state = "shown";
      else this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }

  /** Out from whatever is visible now (also mid-sequence): visible text and overlay fade out; pending text never appears. */
  hide() {
    if (this.state === "hidden") return;
    this.state = "out";
    this.xf = null;
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

  /** New counts: the visible paragraph crossfades (old out, new in) while shown, the sequence goes on; else switches at once. */
  update(c: Counts) {
    if (this.counts && c.ja === this.counts.ja && c.nein === this.counts.nein) return;
    if (this.state !== "shown" && this.state !== "in") return this.set(c);
    this.xf = { start: performance.now(), c, swapped: false };
    this.counts = c;
    this.run();
  }

  destroy() {
    cancelAnimationFrame(this.raf);
  }
}
