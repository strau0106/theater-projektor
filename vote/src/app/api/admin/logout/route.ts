import { ADMIN_COOKIE } from "@/lib/auth";
import { cookiePath, json } from "@/lib/http";

export const dynamic = "force-dynamic";

export function POST() {
  const res = json({ ok: true });
  res.cookies.set(ADMIN_COOKIE, "", { path: cookiePath, maxAge: 0 });
  return res;
}
