import { createHmac, randomUUID } from "node:crypto";
import { cookies } from "next/headers";
import { cookiePath, isHttps, json } from "@/lib/http";
import { castVote, results, voteOf } from "@/lib/polls";

export const dynamic = "force-dynamic";
const VOTER = "welle_voter";

// status for the vote page: poll state plus whether this browser already voted in it
// 4-character code (no look-alike characters) from poll id + voter id, only ever returned to that voter
const ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
function voteCode(poll: number, voter: string): string {
  const h = createHmac("sha256", process.env.SESSION_SECRET || "welle-vote").update(`${poll}:${voter}`).digest();
  return Array.from(h.subarray(0, 4), (b) => ALPHABET[b % ALPHABET.length]).join("");
}

export async function GET() {
  const r = results();
  const id = (await cookies()).get(VOTER)?.value;
  const choice = id ? voteOf(id) : null;
  return json({
    poll_id: r.poll_id,
    status: r.status,
    voted: !!choice,
    choice,
    code: id && choice === "ja" ? voteCode(r.poll_id, id) : null,
  });
}

export async function POST(req: Request) {
  const body = await req.json().catch(() => null);
  const choice = body?.choice;
  if (choice !== "ja" && choice !== "nein") return json({ error: "invalid choice" }, 400);
  const existing = (await cookies()).get(VOTER)?.value;
  const voter = existing && /^[0-9a-f-]{36}$/.test(existing) ? existing : randomUUID();
  const outcome = castVote(voter, choice);
  const res =
    outcome === "ok" ? json({ ok: true }) : outcome === "duplicate" ? json({ error: "already voted" }, 409) : json({ error: "poll not open" }, 403);
  if (voter !== existing) {
    res.cookies.set(VOTER, voter, { httpOnly: true, sameSite: "lax", secure: isHttps(req), path: cookiePath, maxAge: 365 * 86400 });
  }
  return res;
}
