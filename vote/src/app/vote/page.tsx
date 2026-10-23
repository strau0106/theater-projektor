import { VoteClient } from "./VoteClient";

export const dynamic = "force-dynamic";

export default async function VotePage({ searchParams }: { searchParams: Promise<{ preview?: string }> }) {
  return <VoteClient preview={(await searchParams).preview === "1"} />;
}
