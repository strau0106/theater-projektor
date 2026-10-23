// Single source of truth for the visual design (web pages, admin preview, projector template).
export const COLORS = {
  blue: "#20A0D0",
  blueDeep: "#0B6F95",
  navy: "#0B2540",
  cream: "#F4F1E6",
  paper: "#FBFAF4",
  track: "#DDD9C8",
  muted: "#4A5D70",
  danger: "#B3261E",
} as const;

// projector slide font (Helvetica Neue Bold stand-in, metric-compatible with Arial/Helvetica)
export const FONTS = [{ family: "Liberation Sans", file: "LiberationSans-Bold.ttf" }] as const;

export const WIDTH = 1920;
export const HEIGHT = 1080;
// "sequence": paragraphs are shown one at a time; "stack": each one stays and the next appears below/over it
export const PARAGRAPH_MODE: "sequence" | "stack" = "sequence";
export const QUESTION = "Bist du für die Welle?";

// seconds; the same numbers are used by the projector (docs/results.md)
export const TIMING = {
  inOverlay: 0.5,
  inPara: 0.8, // fade-in of one paragraph
  paraHold: 6.0, // start of a paragraph's fade-in to the start of its fade-out (Pi: [results] para_hold_s)
  paraGap: 0.2, // black between one paragraph's fade-out and the next one's fade-in
  outText: 0.6,
  outOverlayAt: 0.4,
  outOverlay: 0.4,
  update: 0.5, // text crossfade when the counts change while shown
} as const;

// The one place that maps the slide's placeholders to the vote counts:
// "x" in the Keynote slide = Nein votes, "y" = Ja votes. Swap the tokens here to flip it.
export const SLIDE_COUNT = { x: "count_nein", y: "count_ja" } as const;

// Keynote-style text slide (black, white Liberation Sans Bold). Each paragraph is its own slide, so the
// text is set large: the widest line with a 3-digit count is ~1725 px of 1920 at size 92.
// Lines are centred horizontally; each paragraph is centred vertically on its own (template.ts).
export const SLIDE = {
  size: 92,
  lineHeight: 112,
  capHeight: 0.688, // Liberation Sans cap height / size, for the vertical centring
  // paragraphs (-> <g id="para-N">), one entry per line; {x} / {y} are replaced by SLIDE_COUNT tokens
  paragraphs: [
    ["Heute Abend haben sich {x} Menschen", "mit ihrer Stimme für eine freie und", "offene Gesellschaft eingesetzt."],
    ["Für {y} reichte ein Chupa Chups aus,", "diese Gesellschaft aufzugeben."],
    ["Wie viele von euch es nicht wichtig", "genug fanden, abzustimmen und damit", "die Katastrophe zu verhindern,", "wissen wir nicht."],
  ],
} as const;
