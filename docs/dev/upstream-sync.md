# 参考项目吸收台账

参考来源：

- **chatgpt2api**：上游是 yukkcat/chatgpt2api，本地另有一份它的 fork。
- **chatgpt-register**：注册控制台。

规则：只有在本表里有逐项记录（对应提交或文件）的才算"已吸收"，不凭 CHANGELOG 印象宣称完成。每做完一批就更新本表。

## 已吸收

### 2026-10-21 ～ 10-22：第一轮全面对照

这一轮做了六路审计：函数级、设计文档、前端、注册子项目、配置与部署、CHANGELOG 与契约。审计出的高价值项按主题分批提交：

| 提交 | 主题 | 要点 |
|---|---|---|
| `a481764` | 许可证 | 改为 AGPL-3.0-only，NOTICE 保留 MIT 来源声明 |
| `ad39059` | 文档 | README、更新记录、部署文档、环境变量说明 |
| `c786fe9` | 部署 | Docker/compose/CI/安装脚本、PostgreSQL 脚手架 |
| `24909f7` | 在线更新 | SHA-256 校验、失败回退、运行环境面板 |
| `76024e6` | 安全 | 敏感配置脱敏、访问日志打码、`/health` 收紧、SSRF 防护、登录节流 |
| `553be59` | 存储 | SQLite WAL、图片墓碑删除、备份含数据库快照、保留期统一清理、GenBox 推送、放大引擎 |
| `74bb9de` | 注册 | 瞬时失败放回邮箱、Outlook 邮箱池、出口检查/配额/槽位、登录导入 |
| `603104c` | 账号 | 稳定管理 ID、凭据展示、调度/冷静期/失败冷却/待核验、健康度、导入导出 |
| `d11924a` | 图片与对话 | 官网对话流程、请求级截止时间、断流恢复、失败分类、n 上限 4、账号并发默认 1、OpenAI 错误格式 |
| `ccffaf4` | 观测 | 调用结果分类、生图尝试时间线、监控详情、调用记录入库 |
| `3d04355` | 核心 | 新配置项与钳制、设置保存版本冲突检测 |
| `35684fe` | 前端 | 账号、日志、概览、设置、注册、Studio、代理等页面 |
| `7e32720` | 账号 | 保存密码和 2FA，RT 失效时自动重新登录 |

### 2026-11-20：上游近期更新（`90f01a8`）

| 项 | 改动位置 |
|---|---|
| `gpt-image-2.6` 系列模型名，兼容 `-flare`、`-sunburst` 后缀 | `utils/helper.py` 的 `WEB_IMAGE_MODELS`、`web-vue/src/config/modelCatalog.ts` |
| 单次生图张数上限可配置 `image_max_n`，范围 1–10，默认 4；Grok 仍为 10 | `services/config.py`、`services/image_count.py`、`api/ai.py`、`services/image_task_service.py`、设置页"单次最多生图张数" |
| 输出格式 `output_format`：png、jpeg（jpg 为别名）、webp | `services/image_output_format.py`，生图/编辑/chat/responses 各入口，任务记录，存储扩展名与 WebDAV Content-Type |
| 聊天 markdown 里的图片按实际格式写 data URI 的 MIME | `utils/helper.py` 的 `_b64_image_mime` |

## 未补（可做，未排期）

- 排队任务的参考图 spool。
- 聊天里的完整 PPT/PSD 文件模式。
- 通用的长操作进度抽屉。
- 请求详情里的细分耗时，以及逐次尝试时间线。
- 真实联网链路的端到端验证。
- Studio 里的"输出格式"选择按钮（后端已支持）。

## 明确不做（已与维护者确认）

| 项 | 原因 |
|---|---|
| 设置规范化重构、统一应用数据库、contracts 层 | 改动面大、收益低 |
| 参考项目的内部文档本身 | 只吸收其中的经验 |
| 免费代理源扩展、OpenVPN 网关、按出口限速默认值 | 不需要 |
| ChatGPT 与 Grok 并行注册、推广弹窗、新的批量注册规避手段 | 不做 |
| 上游的 GIF/SVG 输出 | 不是生图主路径，价值低 |
| 照搬上游的完整记忆管理界面 | 网关不需要记忆；改为"账号关闭记忆"的轻量方案，见 [chatgpt-web-api.md](chatgpt-web-api.md) |
| 自定义域名邮箱的测试与前端 | 只保留后端，不推荐启用 |
