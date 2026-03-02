import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Codeswarm",
  description: "Distributed Codex execution control plane",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="bg-slate-900 text-white antialiased">
        {children}
      </body>
    </html>
  );
}
