# teleagent-dashboard — TeleAgent 云电脑保活/积分/电源管理控制台
# 鉴权用页面「账号登录」（JWT 自动落盘 data/jwt.txt 并自动续期）
# 基础镜像走 DaoCloud 公共加速（docker.io/library/node 的国内缓存），
# 构建机拉不到 Docker Hub 时无需改任何配置；若想强制回官方源，
# 构建时传 --build-arg NODE_IMAGE=node:20-alpine 即可。
ARG NODE_IMAGE=docker.m.daocloud.io/library/node:20-alpine
FROM ${NODE_IMAGE}

# 时区：装 tzdata 并设 TZ，保证日志/定时任务（领取时间、重启计划）按北京时间走
RUN apk add --no-cache tzdata
ENV TZ=Asia/Shanghai

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --registry=https://registry.npmmirror.com && npm cache clean --force
COPY server.js ./
COPY lib ./lib
COPY public ./public

ENV PORT=8590
# 容器内监听全部网卡，宿主机 docker port 映射才能访问（裸 node 直跑默认 127.0.0.1）
ENV TA_HOST=0.0.0.0
ENV TA_DATA_DIR=/app/data
EXPOSE 8590
VOLUME ["/app/data"]

# 健康检查走公开的登录页（/api/* 需要鉴权，不能用）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8590/login >/dev/null || exit 1

CMD ["node", "server.js"]