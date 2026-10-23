import { loadFont } from "@/design/bundle";

export const dynamic = "force-dynamic";

export async function GET(_req: Request, ctx: { params: Promise<{ file: string }> }) {
  const font = loadFont((await ctx.params).file);
  if (!font) return new Response("not found", { status: 404 });
  return new Response(new Uint8Array(font), {
    headers: { "Content-Type": "font/ttf", "Cache-Control": "public, max-age=3600" },
  });
}
