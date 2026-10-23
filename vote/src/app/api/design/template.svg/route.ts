import { designBundle } from "@/design/bundle";
import { noStore } from "@/lib/http";

export const dynamic = "force-dynamic";

export const GET = () =>
  new Response(designBundle().template, { headers: { "Content-Type": "image/svg+xml; charset=utf-8", ...noStore } });
