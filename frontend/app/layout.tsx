import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "NovelOS · 长篇创作控制平台",
  description: "面向个人的长篇小说创作运行环境：任务、正式记忆、版本与质量控制",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
