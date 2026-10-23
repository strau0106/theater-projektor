"use client";

import { useCallback, useEffect, useState } from "react";
import { withBase } from "@/lib/base";
import type { Results } from "@/lib/polls";

type State<T> = { data: T | null; status: number };

/** Poll a JSON endpoint; data is null until the first answer or on an error status. */
export function usePoll<T = Results>(path: string, ms: number) {
  const [state, set] = useState<State<T>>({ data: null, status: 0 });
  const [n, setN] = useState(0);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await fetch(withBase(path), { cache: "no-store" });
        const data = r.ok ? ((await r.json()) as T) : null;
        if (alive) set({ data, status: r.status });
      } catch {
        /* keep last state, retry */
      }
    };
    tick();
    const id = setInterval(tick, ms);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [path, ms, n]);
  const refresh = useCallback(() => setN((x) => x + 1), []);
  return { ...state, refresh };
}
