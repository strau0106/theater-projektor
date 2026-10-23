import { isAdmin } from "@/lib/auth";
import { json } from "@/lib/http";
import { results } from "@/lib/polls";

export const dynamic = "force-dynamic";

export async function GET() {
  if (!(await isAdmin())) return json({ error: "unauthorized" }, 401);
  return json(results());
}
