import { json } from "@/lib/http";
import { results } from "@/lib/polls";

export const dynamic = "force-dynamic";

export const GET = () => json(results());
