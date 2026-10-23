"use client";

import { useState } from "react";
import { LollipopSeal } from "./LollipopSeal";
import { usePoll } from "@/components/usePoll";
import { withBase } from "@/lib/base";
import { QUESTION } from "@/design/tokens";

type Status = { poll_id: number; status: "open" | "closed" | "none"; voted: boolean; choice: "ja" | "nein" | null; code: string | null };

export function VoteClient({ preview }: { preview: boolean }) {
  const { data, refresh } = usePoll<Status>("/api/vote", 4000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function vote(choice: "ja" | "nein") {
    setBusy(true);
    setError("");
    try {
      const r = await fetch(withBase("/api/vote"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choice }),
      });
      if (!r.ok && r.status !== 409 && r.status !== 403) setError("Das hat nicht geklappt. Bitte noch einmal versuchen.");
    } catch {
      setError("Keine Verbindung. Bitte noch einmal versuchen.");
    }
    setBusy(false);
    refresh();
  }

  let body;
  if (!data) body = <p className="note">Lädt …</p>;
  else if (data.status === "none") body = <Note title="Abstimmung noch nicht gestartet" text="Bitte einen Moment Geduld." />;
  else if (data.status === "closed" && !(data.voted && data.choice === "ja" && data.code)) body = <Note title="Abstimmung beendet" text="Danke fürs Mitmachen." />;
  else if (data.voted && data.choice === "ja" && data.code) body = <LollipopSeal poll={data.poll_id} code={data.code} />;
  else if (data.voted) body = <Note title="Danke!" text="Deine Stimme wurde gezählt." />;
  else
    body = (
      <>
        <div className="choices">
          <button className="choice ja" disabled={busy || preview} onClick={() => vote("ja")}>
            Ja, ich bin für die Welle
          </button>
          <button className="choice nein" disabled={busy || preview} onClick={() => vote("nein")}>
            Nein, ich bin gegen die Welle
          </button>
        </div>
        <p className="note">Du kannst nur einmal abstimmen. Deine Antwort ist endgültig.</p>
        {error && <p className="error" role="alert">{error}</p>}
      </>
    );

  return (
    <main className="vote-bg" style={{ backgroundImage: `url(${withBase("/bg.jpg")})` }}>
      <section className="card" aria-live="polite">
        <p className="kicker">Die Welle</p>
        <h1 className="display">{QUESTION}</h1>
        {body}
      </section>
    </main>
  );
}

function Note({ title, text }: { title: string; text: string }) {
  return (
    <div className="state">
      <p className="state-title display">{title}</p>
      <p className="note">{text}</p>
    </div>
  );
}
