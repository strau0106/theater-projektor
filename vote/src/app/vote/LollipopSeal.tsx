"use client";

import { useEffect, useState } from "react";

const pad = (n: number) => String(n).padStart(2, "0");

// Live proof for the bar: moving seal, running clock with seconds, per-vote code. A screenshot can't do any of that.
export function LollipopSeal({ poll, code }: { poll: number; code: string }) {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const id = setInterval(() => setNow(new Date()), 200);
    return () => clearInterval(id);
  }, []);
  const time = now ? `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}` : "--:--:--";
  // three wave crests along a circle-ish path, drawn as a repeating wave that scrolls
  const wave = "M0 40 Q 20 10 40 40 T 80 40 T 120 40 T 160 40 T 200 40 T 240 40";
  return (
    <div className="state lolli">
      <p className="state-title display">Danke für dein Ja zur Welle, hol dir einen Lollipop an der Bar.</p>
      <div className="seal-wrap">
        <svg className="seal" viewBox="0 0 200 200" role="img" aria-label="Live-Siegel">
          <defs>
            <clipPath id="sealclip"><circle cx="100" cy="100" r="62" /></clipPath>
          </defs>
          <g className="seal-spin">
            <circle cx="100" cy="100" r="96" fill="none" stroke="#0B2540" strokeWidth="5" strokeDasharray="3 9" strokeLinecap="round" />
            <circle cx="100" cy="100" r="86" fill="none" stroke="#0B2540" strokeWidth="3" />
          </g>
          <g className="seal-spin-rev">
            <circle cx="100" cy="100" r="76" fill="none" stroke="#20A0D0" strokeWidth="8" strokeDasharray="30 12 8 12" />
          </g>
          <g className="seal-pulse">
            <circle cx="100" cy="100" r="64" fill="#20A0D0" stroke="#0B2540" strokeWidth="4" />
          </g>
          <g clipPath="url(#sealclip)">
            <g className="seal-wave">
              <path d={wave} transform="translate(-40 62)" fill="none" stroke="#0B2540" strokeWidth="7" strokeLinecap="round" />
              <path d={wave} transform="translate(-40 86)" fill="none" stroke="#F4F1E6" strokeWidth="7" strokeLinecap="round" />
              <path d={wave} transform="translate(-40 110)" fill="none" stroke="#0B2540" strokeWidth="7" strokeLinecap="round" />
            </g>
          </g>
        </svg>
      </div>
      <p className="lolli-clock" aria-label="Uhrzeit">{time}</p>
      <p className="lolli-code">
        <span>Abstimmung {poll}</span>
        <strong className="display">{code}</strong>
      </p>
    </div>
  );
}
