"use client";

import { useEffect, useRef } from "react";
import type { Counts } from "@/design/percent";
import { ResultsPlayer } from "@/design/player";

type Props = { svg: string; counts: Counts; visible: boolean; className?: string };

/** Inline template.svg that plays the in/out animation and follows the counts. */
export function Projector({ svg, counts, visible, className }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const player = useRef<ResultsPlayer | null>(null);
  const latest = useRef({ counts, visible });
  latest.current = { counts, visible };

  useEffect(() => {
    const p = new ResultsPlayer(box.current!.querySelector("svg")!);
    player.current = p;
    p.update(latest.current.counts);
    if (latest.current.visible) p.show(latest.current.counts);
    return () => {
      p.destroy();
      player.current = null;
    };
  }, [svg]);

  useEffect(() => {
    if (visible) player.current?.show(latest.current.counts);
    else player.current?.hide();
  }, [visible]);

  useEffect(() => player.current?.update(counts), [counts.ja, counts.nein]); // eslint-disable-line react-hooks/exhaustive-deps

  return <div ref={box} className={`tpl ${className ?? ""}`} dangerouslySetInnerHTML={{ __html: svg }} />;
}
