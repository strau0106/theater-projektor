# Die Welle – audience vote

Next.js (App Router) + SQLite. Audience votes Ja/Nein on their phones (`/vote`), staff run the poll
(`/admin`), the result is shown live on the web (`/results`) and on the projector (Keynote-style text slide rendered by the
Pi from the design bundle in `/api/design`). Contract with the bridge/projector: `../docs/results.md`.

| Path | |
|---|---|
| `/vote` | audience; one final vote per browser (cookie `welle_voter`) and poll |
| `/admin` | password login; start/close polls, live counts, projector preview (Einblenden/Ausblenden), vote-page preview, QR code |
| `/results` | public full-screen live graph |
| `GET /api/results` | current poll `{poll_id,status,ja,nein,total,started_at,closed_at}` |
| `GET /api/design` | design bundle manifest (+ `/api/design/template.svg`, `/api/design/fonts/<file>`) |

## Develop

```sh
npm install
npm run dev            # http://localhost:3000, admin password "admin" unless ADMIN_PASSWORD is set
npm run build && npm start
```

Node 20+. Env: `ADMIN_PASSWORD`, `SESSION_SECRET` (signs the admin cookie; both required in production),
`DATA_DIR` (SQLite location, default `./data`), build arg/env `BASE_PATH` (sub-path such as `/welle`, default empty).

## Deploy on infmk1

```sh
cp .env.example .env   # set ADMIN_PASSWORD, SESSION_SECRET (openssl rand -hex 32), optionally BASE_PATH
docker compose up -d --build
```

Listens on `127.0.0.1:3100`; the database lives in the `vote-data` volume. Changing `BASE_PATH` needs a rebuild.
Backup: `docker compose cp vote:/data/vote.db .` (WAL mode: copy while idle or use `sqlite3 .backup`).

Apache (mod_proxy, mod_proxy_http, mod_headers), inside the HTTPS vhost; use `/` instead of `/welle` when
`BASE_PATH` is empty:

```apache
ProxyPreserveHost On
RequestHeader set X-Forwarded-Proto "https"
ProxyPass        /welle http://127.0.0.1:3100/welle
ProxyPassReverse /welle http://127.0.0.1:3100/welle
```

(With `BASE_PATH=/welle` the app serves everything below that prefix; the QR code and cookies follow it.)

## Bridge and projector

- The bridge (`projektor-bridge --results-url https://example.org/welle/api/results`) fetches `GET /api/results`
  when QLab sends `/bridge/results` and forwards `ja`, `nein`, `poll_id` to the Pi as `/proj/results/show`.
- The Pi fetches the design before the show (`/proj/design/fetch`, `[results] design_url =
  "https://example.org/welle/api/design"`): the manifest lists `template.svg` and the Arimo Bold font (URLs relative
  to the manifest URL); `version` is a content hash, so unchanged designs are not downloaded again.
- Refresh the Pi's bundled default design after changing the look:
  `npm run export-design -- ../app/src/projektor/results_default` (writes `manifest.json`, `template.svg`,
  `fonts/`; the on-disk manifest points at `template.svg` and `fonts/<file>`).

## Design

One design system for web and projector: `src/design/tokens.ts` (palette, geometry, timings) is used by the
CSS variables in `layout.tsx` and by `src/design/template.ts`, which generates the 1920x1080 `template.svg`.
The web pages inline that same SVG (artwork referenced by URL instead of embedded) and `src/design/player.ts`
plays the animation from the contract (timings in `TIMING`). In: black overlay fades in 0-0.5 s, then the
paragraphs (`para-1..3`) fade in one after the other (0.8 s each, 2.5 s start to start). Out: all text fades 0-0.6 s,
overlay 0.4-0.8 s (also mid-reveal, from the current state). New counts while shown: 0.5 s text crossfade.
The slide text, its size/position and the mapping of the slide's placeholders to the counts (`SLIDE_COUNT`: x = Nein,
y = Ja) are in `src/design/tokens.ts`.

- Fonts (OFL, in `fonts/`): Anton (display, vote page), Arimo Bold (projector slide, in the design bundle; Helvetica stand-in, OFL), Barlow (web text).
- Artwork: `design-src/make-bg.sh` scales `design-src/bg-original.png` (the centre line and frame are part of the
  artwork) to `public/bg.jpg` (1920x1080, embedded in the template).
