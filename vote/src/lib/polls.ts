import { db } from "./db";

export type Choice = "ja" | "nein";
export type Results = {
  poll_id: number;
  status: "open" | "closed" | "none";
  ja: number;
  nein: number;
  total: number;
  started_at: string | null;
  closed_at: string | null;
};

const now = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");

type PollRow = { id: number; started_at: string; closed_at: string | null };
const current = () => db().prepare("SELECT * FROM polls ORDER BY id DESC LIMIT 1").get() as PollRow | undefined;

export function results(): Results {
  const p = current();
  if (!p) return { poll_id: 0, status: "none", ja: 0, nein: 0, total: 0, started_at: null, closed_at: null };
  const rows = db()
    .prepare("SELECT choice, COUNT(*) AS n FROM votes WHERE poll_id = ? GROUP BY choice")
    .all(p.id) as { choice: Choice; n: number }[];
  const n = (c: Choice) => rows.find((r) => r.choice === c)?.n ?? 0;
  const ja = n("ja");
  const nein = n("nein");
  return {
    poll_id: p.id,
    status: p.closed_at ? "closed" : "open",
    ja,
    nein,
    total: ja + nein,
    started_at: p.started_at,
    closed_at: p.closed_at,
  };
}

export function startPoll(): number {
  const d = db();
  return d.transaction(() => {
    d.prepare("UPDATE polls SET closed_at = ? WHERE closed_at IS NULL").run(now());
    return Number(d.prepare("INSERT INTO polls (started_at) VALUES (?)").run(now()).lastInsertRowid);
  })();
}

export function closePoll(): boolean {
  return db().prepare("UPDATE polls SET closed_at = ? WHERE closed_at IS NULL").run(now()).changes > 0;
}

export function hasVoted(voter: string): boolean {
  const p = current();
  return !!p && !!db().prepare("SELECT 1 FROM votes WHERE poll_id = ? AND voter_id = ?").get(p.id, voter);
}

export type VoteOutcome = "ok" | "duplicate" | "closed";

export function castVote(voter: string, choice: Choice): VoteOutcome {
  const d = db();
  return d.transaction((): VoteOutcome => {
    const p = current();
    if (!p || p.closed_at) return "closed";
    try {
      d.prepare("INSERT INTO votes (poll_id, voter_id, choice, created_at) VALUES (?, ?, ?, ?)").run(p.id, voter, choice, now());
      return "ok";
    } catch (e) {
      if ((e as { code?: string }).code === "SQLITE_CONSTRAINT_UNIQUE") return "duplicate";
      throw e;
    }
  })();
}
