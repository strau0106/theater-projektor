import { isAdmin } from "@/lib/auth";
import { json } from "@/lib/http";
import { closePoll, results } from "@/lib/polls";

export const dynamic = "force-dynamic";

export async function POST() {
  if (!(await isAdmin())) return json({ error: "unauthorized" }, 401);
  closePoll();
  return json(results());
}
