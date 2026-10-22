ARG TARGETARCH

FROM node:22-alpine AS web-build

WORKDIR /app/web-vue

COPY web-vue/package.json web-vue/package-lock.json ./
RUN npm ci

COPY VERSION /app/VERSION
COPY CHANGELOG.md /app/CHANGELOG.md
COPY web-vue ./
RUN npm run build


FROM node:22-alpine AS protocol-node-deps

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force


# 可选：sharp 放大引擎（image_upscale_engine=sharp_lanczos3）。需 glibc 预编译包，故用 bookworm 而非 alpine；
# 安装失败不阻断构建，运行时会自动回退 Pillow。
FROM node:22-bookworm-slim AS image-upscale-deps

WORKDIR /app/scripts/image_upscale

COPY scripts/image_upscale/package.json ./
RUN (npm install --omit=dev --ignore-scripts && npm cache clean --force) \
    || (echo "sharp install failed; falling back to Pillow upscale" && mkdir -p node_modules)


FROM python:3.13-slim AS app

ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    TZ=Asia/Shanghai \
    MALLOC_ARENA_MAX=2 \
    CHATGPT2API_THREAD_TOKENS=120

WORKDIR /app

# 安装系统依赖
# - git: Git 存储后端需要
# - libpq-dev: PostgreSQL 客户端库
# - postgresql-client: 提供 pg_dump，STORAGE_BACKEND=postgres 时备份需要导出一致性数据库快照
# - gcc: 编译 psycopg2-binary 需要
# - nodejs: 无浏览器执行 Castle SDK；npm 仅在 protocol-node-deps 阶段使用
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq-dev \
    postgresql-client \
    gcc \
    nodejs \
    openssl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY package.json package-lock.json ./
COPY --from=protocol-node-deps /app/node_modules ./node_modules

COPY main.py ./
COPY config.example.yaml ./
COPY config.defaults.toml ./
COPY VERSION ./
COPY CHANGELOG.md ./
COPY GROK2API_LICENSE ./
COPY app ./app
COPY api ./api
COPY services ./services
COPY utils ./utils
COPY scripts ./scripts
COPY --from=image-upscale-deps /app/scripts/image_upscale/node_modules ./scripts/image_upscale/node_modules
COPY --from=web-build /app/web-vue/dist ./web_dist

# 受管运行目录（可选）：保存一份应用文件种子，供 GPTGROK2API_MANAGED_RUNTIME=1 且 /app 挂载持久卷时
# 由入口脚本展开；默认模式下入口脚本只 exec 原命令，行为不变。
RUN mkdir -p /opt/gptgrok2api && cp -a main.py config.example.yaml config.defaults.toml VERSION CHANGELOG.md GROK2API_LICENSE app api services utils scripts web_dist package.json package-lock.json pyproject.toml uv.lock node_modules /opt/gptgrok2api/
COPY deploy/docker-entrypoint.sh /usr/local/bin/gptgrok2api-entrypoint
RUN chmod 0755 /usr/local/bin/gptgrok2api-entrypoint

EXPOSE 80

ENTRYPOINT ["gptgrok2api-entrypoint"]
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--access-log", "--timeout-graceful-shutdown", "30"]
