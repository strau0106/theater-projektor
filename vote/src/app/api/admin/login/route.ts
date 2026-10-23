import { ADMIN_COOKIE, MAX_AGE, checkPassword, makeToken } from "@/lib/auth";
import { cookiePath, isHttps, json } from "@/lib/http";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const body = await req.json().catch(() => null);
  if (typeof body?.password !== "string" || !checkPassword(body.password)) {
    await new Promise((r) => setTimeout(r, 600));
    return json({ error: "wrong password" }, 401);
  }
  const res = json({ ok: true });
  res.cookies.set(ADMIN_COOKIE, makeToken(), { httpOnly: true, sameSite: "lax", secure: isHttps(req), path: cookiePath, maxAge: MAX_AGE });
  return res;
}
