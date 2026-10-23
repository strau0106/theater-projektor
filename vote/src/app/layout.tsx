import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import { COLORS } from "@/design/tokens";
import "./globals.css";

const display = localFont({ src: "../../fonts/Anton-Regular.ttf", variable: "--font-display", display: "swap" });
const slide = localFont({ src: "../../fonts/LiberationSans-Bold.ttf", variable: "--font-slide", weight: "700", display: "swap" });
const text = localFont({
  src: [
    { path: "../../fonts/Barlow-Regular.ttf", weight: "400" },
    { path: "../../fonts/Barlow-SemiBold.ttf", weight: "600" },
    { path: "../../fonts/Barlow-Bold.ttf", weight: "700" },
  ],
  variable: "--font-text",
  display: "swap",
});

export const metadata: Metadata = { title: "Die Welle – Abstimmung", robots: { index: false } };
export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: COLORS.blue };

const vars = `:root{${Object.entries(COLORS)
  .map(([k, v]) => `--${k.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase())}:${v}`)
  .join(";")}}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="de" className={`${display.variable} ${slide.variable} ${text.variable}`}>
      <head>
        <style dangerouslySetInnerHTML={{ __html: vars }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
