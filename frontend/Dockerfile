# 前端镜像：Next.js standalone 构建，运行时只带必要文件。
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# 前端只请求相对路径 /api/*，由服务端 rewrite 转发；容器里指到 backend 服务名
ENV NOVELOS_BACKEND_URL=http://backend:8000
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    NOVELOS_BACKEND_URL=http://backend:8000 \
    PORT=3000
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
# 说明：这个项目没有 public/ 静态目录（图标走 app/icon.svg，由应用自己提供）。
# 以后要加静态资源，建 public/ 后在这里补一行 COPY --from=builder /app/public ./public。
EXPOSE 3000
CMD ["node", "server.js"]
