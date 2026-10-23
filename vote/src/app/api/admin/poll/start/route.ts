import { isAdmin } from "@/lib/auth";
import { json } from "@/lib/http";
import { results, startPoll } from "@/lib/polls";

export const dynamic = "force-dynamic";

export async function POST() {
  if (!(await isAdmin())) return json({ error: "unauthorized" }, 401);
  startPoll();
  return json(results());
}
