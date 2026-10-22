# 部署与升级指南

本文介绍 ChatGPT2API 的常见部署方式，以及后续升级项目时需要保留的数据和执行步骤。

## 部署前准备

服务器需要安装：

- Docker
- Docker Compose v2
- Git

首次部署前建议确认：

```bash
docker version
docker compose version
git --version
```

项目核心持久化文件：

| 路径 | 作用 |
| --- | --- |
| `config.json` | 主配置、后台密钥、代理、图片、备份等配置 |
| `.env` | Docker compose 环境变量 |
| `data/` | 账号、注册配置、日志、图片、任务记录等运行数据 |

升级和迁移时重点保留以上内容。

## 方式一：普通 Docker 部署

适合不需要 WARP / FlareSolverr 清障的场景。

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
```

设置 `config.json` 中的 `auth-key`，或在 `docker-compose.yml` 中配置：

```yaml
environment:
  - CHATGPT2API_AUTH_KEY=your_secret_key
```

启动：

```bash
docker compose up -d
```

访问：

```text
http://localhost:3000
```

API 基础地址：

```text
http://localhost:3000/v1
```

查看日志：

```bash
docker logs -f chatgpt2api
```

停止：

```bash
docker compose down
```

## 方式二：WARP / FlareSolverr 部署

适合注册流程经常遇到 Cloudflare 拦截的场景。该方式会启动：

- `warp-proxy`
- `privoxy`
- `flaresolverr`
- `init-config`
- `app`

复制环境变量模板：

```bash
cp .env.example .env
```

至少修改 `.env` 中的：

```text
CHATGPT2API_AUTH_KEY=your_secret_key_here
```

启动：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

访问：

```text
http://localhost:3000
```

FlareSolverr 相关配置可以在后台设置页的 `FlareSolverr` tab 中查看和测试。更详细的 Cloudflare 清障说明见：

```text
docs/flaresolverr-cloudflare.md
```

查看容器状态：

```bash
docker compose -f docker-compose.warp.yml ps
```

查看日志：

```bash
docker logs -f chatgpt2api-warp
docker logs -f chatgpt2api-flaresolverr
```

停止：

```bash
docker compose -f docker-compose.warp.yml down
```

## 方式三：源码运行

适合本地开发或临时调试。

后端：

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

前端开发服务：

```bash
cd web
bun install
bun run dev
```

源码方式运行时，后端默认读取项目根目录的 `config.json` 和 `data/`。

## 存储后端

默认使用本地 JSON 文件：

```text
STORAGE_BACKEND=json
```

可选值：

| 值 | 说明 |
| --- | --- |
| `json` | 本地 JSON 文件，默认方式 |
| `sqlite` | 本地 SQLite，通常存放在 `data/accounts.db` |
| `postgres` | 外部 PostgreSQL |
| `git` | Git 私有仓库存储账号数据 |

PostgreSQL 示例：

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

SQLite 示例：

```yaml
environment:
  - STORAGE_BACKEND=sqlite
  - DATABASE_URL=sqlite:////app/data/accounts.db
```

## 环境变量传入容器

仓库自带的 Compose 文件只把部分变量写进了 `environment:`，`.env` 里的值默认只用于 Compose 插值，不会自动进入容器。
下面各节提到的 `CHATGPT2API_TRUST_PROXY_HEADERS`、`CHATGPT2API_LOGIN_MAX_FAILURES`、`CHATGPT2API_LOGIN_FAILURE_WINDOW_SECONDS`、
`GPTGROK2API_IMAGE_THREAD_TOKENS`、`SQLITE_BUSY_TIMEOUT_SECONDS`、`DATABASE_POOL_*` 等变量，若 Compose 文件中没有对应条目，
请在 `docker-compose.override.yml` 的 `app.environment` 中显式添加后再 `docker compose up -d`，并用 `docker compose exec app env` 确认已生效。

## 反向代理后的登录节流

后台登录对失败次数做节流（同一来源窗口内超过上限会被拒绝）。默认只信任 TCP 对端地址：

- 服务部署在 Nginx / Caddy / Cloudflare 等反向代理之后时，必须设置 `CHATGPT2API_TRUST_PROXY_HEADERS=1`，节流才会读取 `X-Forwarded-For` 的第一个地址区分来源；
  不设置时所有请求来源都是代理地址，会共享同一份失败计数，少量失败即可锁住所有用户。
- 直接暴露到公网（没有可信代理）时请保持关闭，否则来源地址可被伪造以绕过节流。
- 反向代理必须覆盖而不是透传客户端自带的 `X-Forwarded-For`（Nginx 使用 `proxy_set_header X-Forwarded-For $remote_addr;`，或 `$proxy_add_x_forwarded_for` 且确认上游不可被客户端直连）。
- 阈值可调：`CHATGPT2API_LOGIN_MAX_FAILURES`（默认 10）、`CHATGPT2API_LOGIN_FAILURE_WINDOW_SECONDS`（默认 300）。
- `/health` 无需鉴权，只返回状态与版本，可直接用于代理或编排层的健康检查。

## PostgreSQL 部署

适用于希望把账号、日志、看板指标等业务数据放进 PostgreSQL 的场景（图片文件仍在 `data/`）。

- 本地容器：在 `.env` 设置 `POSTGRES_PASSWORD`（仅限字母、数字和 `. _ ~ -`），叠加 `docker-compose.postgres.yml` 启动（命令见 README“本地 PostgreSQL 容器”）。
  数据库使用命名卷 `chatgpt2api-postgres-data`，`app` 的 `STORAGE_BACKEND=postgres` 与 `DATABASE_URL` 由该文件注入。
- 一键安装：`bash deploy/install.sh --mode docker --database postgres-local`（Docker 模式专用，重复安装沿用 `.env` 中已有密码）；
  已有外部库用 `--database postgres-url --database-url postgresql://user:pass@host:5432/db`。
- 连接池：`DATABASE_POOL_SIZE`（10）、`DATABASE_MAX_OVERFLOW`（20）、`DATABASE_POOL_TIMEOUT_SECONDS`（30），见下文“数据库连接环境变量”。
- 备份与恢复：必须使用 `pg_dump`（后台 R2 备份会自动附带 `application-database.pgdump`），恢复使用 `pg_restore`，脚本只打印命令不自动执行，见下文“备份恢复”。
  不要用 `tar data/` 代替，也不要用 `docker compose down -v` 升级——会删除数据库命名卷。
- 当前仅 SQLite 路径经过实际运行验证，PostgreSQL 路径只有单元/模拟测试，生产使用前请先在测试环境完整走一遍备份与恢复。

## 在线更新与受管运行时

管理台的“一键更新”会从 GitHub Release 下载 `gptgrok2api-app.tar.gz`，用 `checksums.txt` 校验 SHA-256，替换应用文件，失败时回退到上一个版本（备份目录 `.gptgrok2api-previous-version`）。

- 仅在“受管运行时”可用：容器环境设置 `GPTGROK2API_MANAGED_RUNTIME=1`，并把 `/app` 挂为持久卷（`docker-compose.yml` 中有注释掉的 `gptgrok2api-runtime:/app` 示例，同时需在顶层声明该卷）。
  入口脚本会在镜像版本变化时用镜像内种子目录重置应用文件，保留 `data`、`logs`、`config.json` 与 `.venv`。
- 未启用时（默认）升级方式仍是拉取新镜像并重建容器，管理台只提示有新版本，不能在线更新。
- 在线更新包只在发布 Release 后由 CI 上传；更新前同样必须先完成备份（见下文）。
- 本功能的完整流程尚未经真实发布版本端到端验证。

## 升级前备份

> **升级或回滚之前，必须先完成一次备份并确认成功。** 在后台「设置 -> 备份」执行一次 R2 备份，
> 确认状态为“成功”且没有“已跳过数据库快照”的提示；没有配置 R2 时，请先停止服务再按下文手动备份。

### 备份内容与数据库一致性快照

后台 R2 备份的归档（`backup-*.tar.gz`，开启加密时为 `.tar.gz.enc`）包含：

- `backup-metadata.json`：版本、时间、存储后端、数据库快照的哈希；
- `backup-manifest.json`：归档内每个文件的 SHA-256 与大小，供恢复脚本校验；
- 数据库一致性快照（`STORAGE_BACKEND=sqlite` / `postgres` 时**始终包含**，不受勾选项影响）：
  - SQLite：`data/application-database.sqlite3`，用 SQLite online backup API 导出，服务运行中也一致（WAL 模式安全）；
  - PostgreSQL：`data/application-database.pgdump`，由 `pg_dump --format=custom` 导出（密码通过 `PGPASSWORD` 环境变量传递，不出现在命令行）。镜像已内置 `postgresql-client`；源码运行需自行安装。若找不到 `pg_dump`，备份会**跳过数据库快照并在状态里给出中文提示**，其余内容照常备份——此时请勿把这次备份当作完整备份；
- 其余按设置勾选：配置、注册数据、CPA/Sub2API 配置、图片任务与索引（含待重试的 WebDAV 删除记录）、可编辑文件、图片目录。外部 WebDAV 上的图片副本需要单独备份。

库内的调用日志、看板指标、可编辑文件任务等表都位于上述数据库快照中；仅备份 `logs.jsonl` / `dashboard_metrics.json` 无法覆盖它们。

### 手动备份（未配置 R2 或需要离线副本）

JSON / Git 存储后端，**先停止服务**再打包：

```bash
mkdir -p backups
docker compose stop
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

如果没有 `.env`，去掉它即可。

SQLite 后端：停服后复制 `data/accounts.db`（或 `DATABASE_URL` 指向的文件）**连同同名的 `-wal`、`-shm` 文件**，
或使用在线备份：`sqlite3 data/accounts.db ".backup 'backups/accounts-$(date +%Y%m%d-%H%M%S).db'"`。
SQLite 现在以 WAL 模式运行，**只复制主库文件而漏掉 `-wal` 会丢失最近的写入**。

PostgreSQL 后端：**必须使用 `pg_dump`，不能用 `tar data/` 代替数据库备份**（数据库文件不在 `data/` 里）：

```bash
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

本地 PostgreSQL 容器可直接在数据库容器内导出：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec -T postgres \
  sh -c 'pg_dump --format=custom --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

### 数据库连接环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SQLITE_BUSY_TIMEOUT_SECONDS` | `30` | SQLite 写锁冲突时的等待秒数（最小 1） |
| `DATABASE_POOL_SIZE` | `10` | PostgreSQL 连接池大小（SQLite 固定单连接） |
| `DATABASE_MAX_OVERFLOW` | `20` | PostgreSQL 连接池溢出上限 |
| `DATABASE_POOL_TIMEOUT_SECONDS` | `30` | 等待空闲连接的秒数 |

## 备份恢复

使用 `scripts/restore_backup.py` 校验并恢复备份。**默认只校验、不写入任何文件**：

```bash
# 仅校验并列出内容（加密备份通过环境变量提供口令）
CHATGPT2API_BACKUP_PASSPHRASE='你的口令' python scripts/restore_backup.py backup-20260101T000000Z-ab12.tar.gz.enc

# 确认无误后，停止服务，再真正写入（先自动把现有 data/ 与 config.json 快照到同级的 *.pre-restore-<时间>）
docker compose stop
python scripts/restore_backup.py backup-20260101T000000Z-ab12.tar.gz --target . --apply
```

要点：

- 脚本会解密（需要 `openssl`）、解包、校验 `backup-metadata.json` 与 `backup-manifest.json` 的哈希；校验失败或发现不安全路径时退出码为 2，不写入任何文件。
- **SQLite**：数据库快照默认写到 `data/application-database.sqlite3`，不会覆盖在线库。请在服务停止后把它复制为 `DATABASE_URL` 指向的库文件（并删除旧的 `-wal`、`-shm`），或加 `--sqlite-target data/accounts.db` 直接恢复到该文件。
- **PostgreSQL**：脚本**不会自动执行**恢复，只打印命令，请确认目标库可以被覆盖后手动执行：
  `pg_restore --clean --if-exists --no-owner --no-privileges --dbname "$DATABASE_URL" data/application-database.pgdump`
- 旧版本生成的备份没有 `backup-manifest.json`，脚本会提示无法逐文件校验哈希，但仍可恢复。

## 升级：普通 Docker 部署

进入项目目录：

```bash
cd chatgpt2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码和镜像：

```bash
git pull
docker compose pull
docker compose up -d
```

查看状态：

```bash
docker compose ps
docker logs -f chatgpt2api
```

## 升级：WARP / FlareSolverr 部署

进入项目目录：

```bash
cd chatgpt2api
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码并重新构建：

```bash
git pull
docker compose -f docker-compose.warp.yml up -d --build
```

查看状态：

```bash
docker compose -f docker-compose.warp.yml ps
docker logs -f chatgpt2api-warp
```

## 升级：源码运行

```bash
cd chatgpt2api
git pull
uv sync
```

如果需要重新构建前端静态产物：

```bash
cd web
bun install
bun run build
```

然后按你的进程管理方式重启后端服务。

## 回滚

> **回滚会让代码回到旧版本，但数据库结构 / 数据可能已被新版本修改。回滚前务必先备份当前状态，
> 并准备好升级前的备份；数据恢复请使用 `scripts/restore_backup.py`（见上文“备份恢复”）。**

### 升级 / 回滚警示

- 升级前先备份并**确认备份成功**，再执行 `pull` / `up`；备份状态显示“已跳过数据库快照”时不算成功备份。
- PostgreSQL 必须用 `pg_dump` 备份；**不要用 `tar data/` 代替数据库备份**。
- SQLite 为 WAL 模式，手动复制库文件时必须一并复制 `-wal`、`-shm`，或使用在线备份。
- **不要使用 `docker compose down -v`** 做普通升级或回滚：`-v` 会删除 Compose 管理的命名卷（含 PostgreSQL 数据卷 `chatgpt2api-postgres-data`），数据无法找回。
- 不要在服务运行时直接覆盖 `data/` 或数据库文件；恢复前先停止服务。
- `/app` 运行时卷不是业务备份，可以从镜像重新创建；业务数据只在 `data/` 与数据库中。
- 图片删除采用“墓碑”机制：WebDAV 删除失败的记录保存在 `data/image_index.json.remote-deletes.json`，由后台保留期清理周期性重试；回滚到不识别该文件的旧版本不会破坏数据，但远端残留文件不会再被自动清理。

如果升级后需要回滚代码：

```bash
git log --oneline -n 20
git checkout <旧版本commit>
```

普通 Docker 部署：

```bash
docker compose up -d
```

WARP / FlareSolverr 部署：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

如果需要恢复数据：

- 使用 `scripts/restore_backup.py` 恢复后台生成的备份归档（见“备份恢复”）；
- 或者对手动打包的备份：`tar -xzf backups/你的备份文件.tgz`（仅适用于 JSON / Git 后端；PostgreSQL 用 `pg_restore` 恢复 `.pgdump`）。

恢复数据前建议先停止容器，避免运行中写入覆盖：

```bash
docker compose down
```

或：

```bash
docker compose -f docker-compose.warp.yml down
```

## 常用维护命令

查看容器：

```bash
docker compose ps
```

查看主服务日志：

```bash
docker logs -f chatgpt2api
```

查看 WARP 部署主服务日志：

```bash
docker logs -f chatgpt2api-warp
```

重启普通部署：

```bash
docker compose restart
```

重启 WARP 部署：

```bash
docker compose -f docker-compose.warp.yml restart
```

清理未使用镜像：

```bash
docker image prune
```

不要直接删除 `data/`、`config.json`、`.env`，除非已经确认有可用备份。
