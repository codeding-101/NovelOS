/** Next.js 配置：把 /api 转发到 FastAPI 后端，前端代码只使用相对路径。 */
const backend = process.env.NOVELOS_BACKEND_URL || "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  // 自部署用：产出 .next/standalone，容器里只跑 server.js，不用带整套 node_modules
  output: "standalone",
  experimental: {
    // 开发服务器的代理默认 30 秒就断（socket hang up → 前端只看到 500）。
    // 章节完成工作流、修订闭环、全量扫描这些都是分钟级的真实模型调用，必须放宽。
    proxyTimeout: 600_000,
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
