# CLAUDE.md

给在本仓库工作的 AI 助手看的速查。始终用简体中文交流；技术名词和代码标识符保留原文。

## 项目是什么

GPTGrok2API：把 ChatGPT 网页版和 Grok 包装成 OpenAI 兼容接口（`/v1`、`/grok/v1`）的网关，带账号池、注册、代理、生图和 Vue 管理控制台。Python 3.13 + FastAPI 后端，`web-vue/` 是 Vue 3 + Vite 前端。许可证 AGPL-3.0-only。

## 目录速览

| 路径 | 内容 |
|---|---|
| `main.py` → `api/app.py:create_app` | 应用入口 |
| `api/` | 路由层：`ai.py`（/v1 对话与生图）、`accounts.py`、`register.py`、`image_tasks.py`、`system.py`、`grok*.py` 等 |
| `services/openai_backend_api.py` | 与 chatgpt.com `backend-api` 通信（会话、sentinel、生图、文件） |
| `services/protocol/` | 协议转换：`conversation.py` 是官网对话主流程，`openai_v1_*.py` 对应各 OpenAI 接口 |
| `services/account_*.py` | 账号池：存取、调度、健康度、刷新与重新登录 |
| `services/register/` | ChatGPT / Grok 注册与邮箱 Provider |
| `services/image_*.py` | 生图任务、存储（本地 / WebDAV）、放大、格式转换、保留期清理 |
| `services/storage/` | 持久化：SQLite（默认，WAL）/ PostgreSQL / JSON / Git |
| `services/config.py` | 设置项读取与钳制；新增设置项要同时进 `SETTINGS_UPDATE_KEYS` 和 `get()` |
| `utils/` | sentinel、PoW、TOTP、指纹、日志打码等 |
| `web-vue/src/` | 控制台：`views/`、`api/`（后端接口封装）、`config/modelCatalog.ts`（前端兜底模型列表） |
| `docs/dev/` | **开发笔记，见下** |

## 常用命令（Windows 本机）

- Python 一律用 `.venv\Scripts\python.exe`（`python` 可能是 Microsoft Store 存根）。
- 单个测试模块：`.venv\Scripts\python.exe -m unittest services.image_count_test`
- 全部测试：`.venv\Scripts\python.exe -m unittest discover -p "*_test.py"`
  - 已知与改动无关的 Windows 失败：`services.register.grok_account_store_test`、`services.xai_cli_oauth_store_test`（文件权限相关）。
  - `captcha-solver/` 下的测试因目录名带连字符无法用模块名加载，忽略。
- 前端类型检查：在 `web-vue/` 下运行 `./node_modules/.bin/tsc --noEmit`（不要用 `npx vue-tsc`，会临时装新版本报错）。
- 前端构建验证：`vite build --outDir <临时目录>`，验证后删掉产物。

## 硬规则

- **不要生成或提交 `web_dist/`**（`npm run build:server` 会生成，别跑）。
- **不要碰 `.agents/`**，不提交它。
- `config.json`、`.env`、`data/` 是本地运行数据，已被 gitignore，不要读出其中的密钥。
- 不建分支；只在用户要求时提交。
- 输出账号信息时邮箱脱敏，令牌、密码、2FA 密钥不打印、不写日志。
- 自定义域名邮箱（`services/register/domain_mail.py`）只保留后端，不扩展、不写测试、不推荐启用。
- 仓库是公开的：服务器地址、部署路径、账号数量等运维信息不要写进仓库文件。
- `docs/*.md` 默认被 gitignore（只有少数用户文档放行）；开发笔记放在 `docs/dev/`，不受影响。

## 改代码时的约定

- 测试文件与被测文件同目录，命名 `xxx_test.py`，用 `unittest`。
- 后端新设置项：`services/config.py`（属性 + `SETTINGS_UPDATE_KEYS` + `get()`）→ 前端 `web-vue/src/api/settings.ts` 和 `types/api.ts` → 设置页面板。
- 新模型名：`utils/helper.py` 的模型集合 + `web-vue/src/config/modelCatalog.ts` 兜底列表。
- 面向用户的错误走 OpenAI 错误格式（`{"error": {"message", "type", "param", "code"}}`）。

## 开发笔记（先查再动手，做完顺手更新）

- `docs/dev/chatgpt-web-api.md`：已实测的 chatgpt.com 网页接口行为（记忆、临时对话、生图等）。改官网对话、账号设置相关代码前先看。
- `docs/dev/upstream-sync.md`：参考项目（yukkcat/chatgpt2api、chatgpt-register）的吸收台账：已吸收、未补、明确不做。宣称"已吸收"必须在这里有逐项记录。

笔记只写可公开的事实与结论，写清日期；结论被推翻时直接改，并注明改动日期。
