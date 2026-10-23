import { randomUUID } from "node:crypto";
import { cookies } from "next/headers";
import { cookiePath, isHttps, json } from "@/lib/http";
import { castVote, hasVoted, results } from "@/lib/polls";

export const dynamic = "force-dynamic";
const VOTER = "welle_voter";

// status for the vote page: poll state plus whether this browser already voted in it
export async function GET() {
  const r = results();
  const id = (await cookies()).get(VOTER)?.value;
  return json({ poll_id: r.poll_id, status: r.status, voted: !!id && hasVoted(id) });
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
