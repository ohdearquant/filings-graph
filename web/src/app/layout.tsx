import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "filings-graph",
  description: "SEC filings graph: who controls whom, who holds what, what happened since the 10-K.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
