# Audience vote ("Die Welle") → results on the projector

```
phones ──HTTPS──▶ vote website (Next.js + SQLite, infmk1, Docker, behind Apache)
                         ▲  GET /api/results
QLab ──OSC /bridge/results──▶ projektor-bridge (Mac) ──LoRa: /proj/results/set ja nein poll──▶ Pi
                                                                                  │
Pi (pre-show, WiFi/internet) ──GET /api/design (+ template.svg, fonts)────────────┘  cached on SD
```

Results travel over LoRa as three integers only. Fetching (`/bridge/results`, any time between
"Abstimmung beenden" and the reveal) only **stores** them on the Pi; the results slide is shown by a
normal **results cue** in the cue list, triggered with GO like every other cue. The visual design is fetched by the Pi
**before the show** (OSC `/proj/design/fetch`, needs network) and cached; the Pi renders the
results slide itself from that design. Without a cached design the Pi uses the bundled default
design (a copy of the website's design, `app/src/projektor/results_default/`).

This file is the binding contract between `vote/` (website) and `app/` (bridge + Pi).

## 1. Website (`vote/`)

Next.js (App Router, TypeScript), SQLite (better-sqlite3), German UI, Docker compose,
listens on `127.0.0.1:3100` for the existing Apache reverse proxy on infmk1.
Optional sub-path via build arg `BASE_PATH` (default empty). All URLs below are relative to it.

Pages:

| Path | Who | What |
|---|---|---|
| `/vote` | audience (phones) | two buttons: **"Ja, ich bin für die Welle"** / **"Nein, ich bin gegen die Welle"**; one vote per browser per poll, final (no change); shows "Danke" after voting, "Abstimmung noch nicht gestartet" / "Abstimmung beendet" otherwise; polls status every few seconds |
| `/admin` | staff, password (`ADMIN_PASSWORD`) | live counts; **Neue Abstimmung starten** (closes current, starts new; everyone may vote again); **Abstimmung beenden**; preview of the projector graphic (exact template, 16:9, play in/out animation) and preview of `/vote`; QR code to `/vote` |
| `/results` | public | full-screen live results slide (same template as projector) |

Voter dedupe: httpOnly cookie `welle_voter` (random UUID, 1 year) + DB `UNIQUE(poll_id, voter_id)`.

### API

`GET /api/results` (public, `Cache-Control: no-store`):

```json
{"poll_id": 3, "status": "open", "ja": 41, "nein": 17, "total": 58,
 "started_at": "2026-10-22T19:30:00Z", "closed_at": null}
```

`status`: `"open" | "closed" | "none"` (`none` = no poll ever started: `poll_id` 0, counts 0).
Returns the **current** (most recently started) poll.

`POST /api/vote` `{"choice": "ja"|"nein"}` → 200 `{"ok": true}` | 409 already voted | 403 poll not open.
Admin endpoints (`/api/admin/...`) require the admin session cookie.

### Design bundle (for the projector)

`GET /api/design` → manifest (URLs relative to the manifest URL):

```json
{"version": "a1b2c3d4e5f6", "width": 1920, "height": 1080,
 "paragraph_mode": "sequence", "template": "design/template.svg",
 "fonts": [{"family": "Arimo", "file": "Arimo-Bold.ttf", "url": "design/fonts/Arimo-Bold.ttf"}]}
```

`paragraph_mode` (optional, `"sequence"` default | `"stack"`, see animation) is a per-design override.
`version` = short content hash of template + fonts + mode (Pi skips download when unchanged).
`npm run export-design -- <dir>` writes the same bundle to disk (`manifest.json`,
`template.svg`, `fonts/…`) — used to refresh the Pi's bundled default design.

`template.svg` rules (rendered by librsvg on the Pi, by the browser on the website):

- `viewBox="0 0 1920 1080"`, `width="1920" height="1080"`.
- Self-contained: raster images embedded as `data:image/jpeg;base64,…` or PNG (no WebP, no
  external hrefs), no CSS `@font-face`, no `<foreignObject>`, no filters librsvg cannot do,
  no scripts. Text only in font families listed in the manifest.
- Top-level groups, rendered as separate layers:
  - `<g id="layer-static">` (required) - background; never changes.
  - `<g id="layer-labels">` (required) - the text, whose content may contain tokens
    `{pct_ja}` `{pct_nein}` (integer percent, rounded so both sum to 100 when total > 0)
    `{count_ja}` `{count_nein}` `{total}`; substituted as plain strings before rendering. Its
    children may be paragraph groups `<g id="para-1">`, `<g id="para-2">`, ... (any number >= 1,
    ordered by N). Each paragraph is rendered as its own layer and shown after the previous one
    (see animation); in the default sequence mode paragraphs are shown alone, so each group is
    centred on the canvas by itself and they overlap in position. A labels layer without `para-N` children is a single paragraph (old bundles).
  - `<g id="layer-bars">` (optional, old bar-graph designs) containing `<rect id="bar-ja">` and
    `<rect id="bar-nein">`, each drawn at **100 %** size. The visible bar = the bottom `pct %` of
    that rect (bars grow upwards; bottom edge `y + height` is the baseline). Without it the Pi
    simply has no bars; the animation of bars (0 -> pct, ease-out) runs together with the first paragraph.
- Text has no auto-wrap in librsvg: line breaks are explicit `<tspan x=".." y="..">`.
- Percent with total = 0: both 0.

The current design ("Keynote text slide"): black background, white Arimo Bold (metric-compatible with
Helvetica/Arial, stands in for Keynote's Helvetica Neue Bold) 55 px, centred (horizontally and, per paragraph, vertically), three
paragraphs `para-1..3` that are shown one at a time. In the slide text `x` = Nein votes (`{count_nein}`), `y` = Ja votes (`{count_ja}`); this
mapping is the `SLIDE_COUNT` constant in `vote/src/design/tokens.ts`.

Animation (same timing on website and Pi, Pi values configurable), default `paragraph_mode` "sequence"
(like separate slides): in = black overlay fades in 0.5 s, then paragraph 1 fades in over 0.8 s and
holds. `para_hold_s` (default 6.0 s, counted from the *start* of the paragraph's fade-in) later it fades
out (0.6 s); after 0.2 s of black the next paragraph fades in (0.8 s), and so on. The last paragraph
stays until hide. So paragraph N (from 1) starts at 0.5 + (N-1) x 6.8 s: 0.5 / 7.3 / 14.1 s. Out
(next cue / GO / fadeout, also mid-sequence) = the visible paragraph fades out over 0.6 s, the overlay
fades 0.4 s overlapping the end (starts at 0.4 s; total 0.8 s); paragraphs not yet shown never appear.
If the results are set again while shown, only the visible paragraph crossfades to the new text (0.5 s);
the sequence timing goes on. A labels layer without `para-N` children is one paragraph: fade in and stay.
Mode "stack" (manifest `paragraph_mode`): paragraphs are never faded out, paragraph N+1 fades in
`para_hold_s` after N started and stays (the old behaviour, there with a 2.5 s gap).

## 2. Bridge (`projektor-bridge`, QLab Mac)

New option `--results-url URL` (e.g. `https://vote.rufusreglet.ch/api/results`).
OSC `/bridge/results` from QLab is **handled by the bridge, not forwarded**: GET the URL
(timeout 3 s), then send `/proj/results/set <ja:int> <nein:int> <poll_id:int>` like any other
command (LoRa with repeats, plus WiFi forward if configured). Reply to QLab:
`/bridge/results/ok <ja> <nein> <poll_id>` or `/bridge/error <msg>`.

## 3. Pi (`projektor`)

| Address | Args | Effect |
|---|---|---|
| `/proj/results/set` | `ja nein [poll_id]` | store the results (persisted in the data dir, survive a restart); no display change. Reply `/proj/results/ok <ja> <nein> <poll_id>` (over LoRa when it came over LoRa) |
| `/proj/design/fetch` | `[url]` | download the design bundle from `url` or `[results] design_url`, store under the data dir `design/`, register fonts. Worker thread. Reply `/proj/design/ok <version>` or `/proj/error <msg>` |

**Results cue** in `show.json`: a cue with `"type": "results"` and no `video`
(`{"id": "12", "name": "Ergebnis", "type": "results", "fade_in": null, "fade_out": null}`;
missing `type` = `"video"`). GO / `/proj/cue` on it behaves like an image cue: the graph
animates in with the stored results (the previous cue fades out as usual) and holds until the next
cue (graph animates out, next cue starts), `/proj/fadeout` (animates out) or `/proj/stop` (cut).
No stored results → shown as 0 / 0 and a warning is logged. The standby/preload logic treats it as
a cue without a clip. The web UI cue editor can add/edit results cues and shows the stored results
(`ja`, `nein`, `poll_id`, received time) and the cached design version.
```toml
[results]
design_url = "https://vote.rufusreglet.ch/api/design"
in_s = 1.3                 # overlay fade + fade-in of one paragraph (0.5 + 0.8 s nominal; scales both)
out_s = 0.8                # total out-animation (scales text and overlay fade)
para_hold_s = 6.0          # start of a paragraph's fade-in to the start of its fade-out (absolute, not scaled)
                           # (old name para_gap_s is still accepted)
```
