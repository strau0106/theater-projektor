import { manifest } from "@/design/bundle";
import { json } from "@/lib/http";

export const dynamic = "force-dynamic";

export const GET = () => json(manifest());
