import type { Metadata } from "next";
import type { ReactNode } from "react";

import "@tabler/core/dist/css/tabler.min.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "LifeOS Agent",
  description: "Privacy-first personal document and life management agent",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
