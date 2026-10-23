"use client";

import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { Projector } from "@/components/Projector";
import { usePoll } from "@/components/usePoll";
import { percents } from "@/design/percent";
import { withBase } from "@/lib/base";
import type { Results } from "@/lib/polls";

async function post(path: string, body?: unknown) {
  return fetch(withBase(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
}

export function AdminClient({ svg }: { svg: string }) {
  const { data, status, refresh } = usePoll("/api/admin/state", 2000);
  if (status === 401 || (status === 0 && !data)) return <Login onDone={refresh} checking={status === 0} />;
  if (!data) return <main className="admin"><p className="note">Fehler beim Laden.</p></main>;
  return <Dashboard svg={svg} data={data} refresh={refresh} />;
}

function Login({ onDone, checking }: { onDone: () => void; checking: boolean }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const r = await post("/api/admin/login", { password: pw });
    if (r.ok) onDone();
    else setErr("Falsches Passwort.");
  }
  if (checking) return <main className="admin" />;
  return (
    <main className="vote-bg" style={{ backgroundImage: `url(${withBase("/bg.jpg")})` }}>
      <form className="card" onSubmit={submit}>
        <p className="kicker">Die Welle</p>
        <h1 className="display">Admin</h1>
        <label className="field">
          Passwort
          <input type="password" autoFocus autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} />
        </label>
        {err && <p className="error" role="alert">{err}</p>}
        <button className="btn primary" type="submit">Anmelden</button>
      </form>
    </main>
  );
}

function Dashboard({ svg, data, refresh }: { svg: string; data: Results; refresh: () => void }) {
  const [shown, setShown] = useState(false);
  const [qr, setQr] = useState("");
  const [url, setUrl] = useState("");
  const p = percents(data);
  const open = data.status === "open";

  useEffect(() => {
    const u = `${location.origin}${withBase("/vote")}`;
    setUrl(u);
    QRCode.toDataURL(u, { margin: 1, width: 360, color: { dark: "#0B2540", light: "#FFFFFF" } }).then(setQr);
  }, []);

  async function start() {
    if (!confirm("Neue Abstimmung starten? Die laufende Abstimmung wird beendet, alle dürfen wieder abstimmen.")) return;
    await post("/api/admin/poll/start");
    refresh();
  }
  async function close() {
    await post("/api/admin/poll/close");
    refresh();
  }
  async function logout() {
    await post("/api/admin/logout");
    refresh();
  }

  const label = { none: "Noch keine Abstimmung", open: `Abstimmung #${data.poll_id} läuft`, closed: `Abstimmung #${data.poll_id} beendet` }[data.status];

  return (
    <main className="admin">
      <header className="bar">
        <h1 className="display">Die Welle · Admin</h1>
        <span className={`chip ${data.status}`}>{label}</span>
        <button className="btn ghost" onClick={logout}>Abmelden</button>
      </header>

      <div className="grid">
        <section className="panel">
          <h2>Abstimmung</h2>
          <div className="actions">
            <button className="btn primary" onClick={start}>Neue Abstimmung starten</button>
            <button className="btn" onClick={close} disabled={!open}>Abstimmung beenden</button>
          </div>
          <div className="counts">
            <div className="count ja"><span className="n display">{data.ja}</span><span>Ja · {p.ja} %</span></div>
            <div className="count nein"><span className="n display">{data.nein}</span><span>Nein · {p.nein} %</span></div>
            <div className="count"><span className="n display">{data.total}</span><span>Stimmen gesamt</span></div>
          </div>
        </section>

        <section className="panel wide">
          <h2>Projektor-Vorschau</h2>
          <div className="preview">
            <Projector svg={svg} counts={data} visible={shown} />
          </div>
          <div className="actions">
            <button className="btn primary" onClick={() => setShown(true)} disabled={shown}>Einblenden</button>
            <button className="btn" onClick={() => setShown(false)} disabled={!shown}>Ausblenden</button>
          </div>
        </section>

        <section className="panel">
          <h2>Abstimmungsseite</h2>
          <div className="phone">
            <iframe title="Vorschau Abstimmungsseite" src={withBase("/vote?preview=1")} />
          </div>
        </section>

        <section className="panel">
          <h2>QR-Code</h2>
          {qr && <img className="qr" src={qr} alt={`QR-Code für ${url}`} width={240} height={240} />}
          <p className="note"><a href={url}>{url}</a></p>
        </section>
      </div>
    </main>
  );
}
