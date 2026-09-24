import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "NovelOS V0.6 · 长篇小说 AI 辅助创作",
  description: "中文长篇小说 AI 辅助创作系统：长期记忆与 Canon 一致性守卫",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
