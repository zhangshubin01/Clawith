# 基础镜像走国内加速（docker.m.daocloud.io），加速器不可用时回退 docker.io
FROM docker.m.daocloud.io/library/node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci --registry https://registry.npmmirror.com
# .dockerignore 排除模式在 docker CLI 29 + buildkitd v0.29.0/v0.32.2（含 OrbStack 捆绑版）的
# 会话传输链路实测不被应用（上游问题，断点未定位，见 docs/technical-plans/20260816-p0-security-fix-plan.md
# P1）：宿主 node_modules（含 macOS 平台二进制）、dist、.git 等会随 COPY . . 进入镜像，与
# npm ci 的 linux 产物合并造成污染。经暂存目录过滤后再合并：不覆盖 npm ci 结果，同时保住 npm ci 层缓存。
COPY . ._ctx
RUN rm -rf ._ctx/node_modules ._ctx/dist ._ctx/.git ._ctx/tests ._ctx/.ruff_cache \
           ._ctx/.env* ._ctx/.DS_Store ._ctx/Dockerfile ._ctx/.dockerignore && \
    cp -a ._ctx/. . && \
    rm -rf ._ctx
RUN npm run build

# nginx 1.31.3 uses a pid-file pwrite path that the CentOS 7 / Docker seccomp
# profile on the 3010 host rejects. Pin the last verified compatible image
# instead of silently widening the container's syscall policy.
FROM docker.m.daocloud.io/library/nginx:1.31.2-alpine@sha256:54f2a904c251d5a34adf545a72d32515a15e08418dae0266e23be2e18c66fefa
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
