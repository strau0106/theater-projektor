import { createHmac, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";

export const ADMIN_COOKIE = "welle_admin";
export const MAX_AGE = 12 * 3600;

function secrets() {
  const dev = process.env.NODE_ENV !== "production";
  const password = process.env.ADMIN_PASSWORD || (dev ? "admin" : "");
  const secret = process.env.SESSION_SECRET || (dev ? "dev-secret" : "");
  if (!password || !secret) throw new Error("ADMIN_PASSWORD and SESSION_SECRET must be set");
  return { password, secret };
}

const sign = (data: string) => createHmac("sha256", secrets().secret).update(data).digest("base64url");

function same(a: string, b: string) {
  const x = createHmac("sha256", "cmp").update(a).digest();
  const y = createHmac("sha256", "cmp").update(b).digest();
  return timingSafeEqual(x, y);
}

export const checkPassword = (pw: string) => same(pw, secrets().password);

export function makeToken(): string {
  const exp = String(Math.floor(Date.now() / 1000) + MAX_AGE);
  return `${exp}.${sign(exp)}`;
}

export function validToken(token: string | undefined): boolean {
  if (!token) return false;
  const [exp, mac] = token.split(".");
  if (!exp || !mac || !same(mac, sign(exp))) return false;
  return Number(exp) > Date.now() / 1000;
}

export async function isAdmin(): Promise<boolean> {
  return validToken((await cookies()).get(ADMIN_COOKIE)?.value);
}
