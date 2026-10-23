import { NextResponse } from "next/server";
import { BASE } from "./base";

export const noStore = { "Cache-Control": "no-store" };
export const json = (body: unknown, status = 200) => NextResponse.json(body, { status, headers: noStore });
export const cookiePath = BASE || "/";
export const isHttps = (req: Request) =>
  (req.headers.get("x-forwarded-proto") ?? new URL(req.url).protocol).startsWith("https");
