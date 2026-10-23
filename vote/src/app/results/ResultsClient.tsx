"use client";

import { Projector } from "@/components/Projector";
import { usePoll } from "@/components/usePoll";

export function ResultsClient({ svg }: { svg: string }) {
  const { data } = usePoll("/api/results", 2000);
  return (
    <main className="stage">
      <Projector svg={svg} counts={{ ja: data?.ja ?? 0, nein: data?.nein ?? 0 }} visible={!!data} />
    </main>
  );
}
