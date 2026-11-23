# chatgpt.com 网页接口实测笔记

记录对 chatgpt.com `backend-api` 的实测结论，供改动官网对话、账号设置相关代码时参考。官网行为随时会变，每条都注明实测日期；发现不符时直接更新本文件。

## 记忆（Memory）——2026-11-20 实测

### 开关与接口

官网设置里现在只剩一个总开关"启用记忆"，对应用户设置字段 `m3m`。旧字段 `sunshine`（已保存记忆）、`moonshine`（参考聊天历史）已不再单独出现在界面上。

| 操作 | 接口 |
|---|---|
| 读取设置 | `GET /backend-api/settings/user` → `settings.m3m`（另有 `m3m_opted_out`） |
| 开关记忆 | `PATCH /backend-api/settings/account_user_setting?feature=m3m&value=true\|false` |
| 读取已保存记忆 | `GET /backend-api/memories?include_memory_entries=true` → `memories[]`、`memory_num_tokens` |
| 清空已保存记忆 | `DELETE /backend-api/settings/clear_account_user_memory` |

调用方式与其它 `backend-api` 请求相同：用 `OpenAIBackendAPI` 的 `session` + `_headers(path)`。

### 行为结论（免费账号，对照实验）

| 问题 | 结论 |
|---|---|
| 免费账号默认开着记忆吗 | 绝大多数是开着的 |
| 普通（非临时）文字对话会写入记忆吗 | 会，用户说"请记住……"即写入 |
| 生图对话会写入记忆吗 | 会，生图提示词里的个人信息同样被写入 |
| 请求体带 `is_do_not_remember: true` 能阻止写入吗 | **不能**，文字和生图都照样写入 |
| 关闭 `m3m` 能阻止写入吗 | **能**，同样的对话一条都不写 |
| 已写入的记忆会被后续对话读出吗 | 写入后约 15 分钟内，普通对话和临时对话都没有读出（不代表永远不会） |

对网关的含义：号池账号被许多下游用户共用，开着记忆时，用户提示词会被存进账号记忆。这既是隐私问题，也有将来串到别人结果里的风险。网关不需要记忆，账号应关闭 `m3m` 并清空已存记忆；不要依赖 `is_do_not_remember`。

已实现（2026-11-20）：`services/account_memory_service.py` 后台逐个处理没处理过的账号（`OpenAIBackendAPI.disable_memory`），结果写在账号的 `memory_disabled_at` / `memory_cleared` / `memory_check_error` 上，账号页凭据弹层显示"官网记忆"状态。设置项 `account_memory_auto_disable`，默认开。开关是账号级的，换令牌、重新登录后不用再处理。

## 账号身份与去重

access_token 的 JWT 声明 `https://api.openai.com/auth` 里有 `chatgpt_user_id`（或 `user_id`）和 `chatgpt_account_id`（工作区）。同一用户的个人空间和 Team 空间是两个号，所以去重键是"用户 ID + 工作区 ID"（`AccountService.account_identity`）。同一个号每次登录拿到的 AT/RT 都不同，只按令牌去重会把它登记两次。导入时和启动时都会按这个键合并：凭据取签发时间最新的一组，其余字段互相补齐。

### 临时对话

- 请求体字段：`history_and_training_disabled: true`、`temporary_chat_requests_personalization: true`。
- 官网说明："此聊天可使用记忆、插件和自定义指令，但不会显示在你的历史记录中"——也就是说临时对话**并不隔离记忆读取**。
- **临时对话不能生图**（官网回复"目前这个临时聊天无法直接生成图片"）。所以生图必须走普通对话。
- 本项目：文字对话默认走临时对话（设置 `chat_temporary_mode`，默认开）；生图不走临时对话。

### `is_do_not_remember`

官网前端会在对话请求里带这个字段；本项目文字请求固定发 `False`（`services/openai_backend_api.py` 的 `_web_conversation_common`）。实测它不影响记忆写入，改成 `True` 没有意义。

## 生图模型名

网页生图的所有模型名（`gpt-image-2.6`、`2.5` 及 `-flare`、`-sunburst` 后缀）在 `_image_model_slug` 里都映射成 `auto` + `picture_v2`，由官网决定画图引擎，**模型名不影响结果**，只是给客户端选的名字。默认 `gpt-image-2.6`（`utils/helper.py` 的 `DEFAULT_IMAGE_MODEL`）。旧名 `gpt-image-2` 自 2026-11-20 起不再列出，但仍接受（`LEGACY_IMAGE_MODELS`）。Codex 生图链路里传给官方 API 的 `"model": "gpt-image-2"` 是真实模型名，不要改。

## GIF 动图——2026-11-20 实测

在生图提示词末尾加上"生成格式为 gif。"（可再加 `frame_count: 8`、`frame_duration_ms: 150` 两行），官网会直接返回多帧 GIF：实测 `gpt-image-2.6`、8 帧，帧数与要求一致。动图是官网画的，不是本地合成的；官网没给 GIF 时网关保留原图，不把单帧图硬转成 GIF。Codex 生图链路不支持。

## 生图会话残留

生图对话会留在账号历史里。设置 `image_remove_conversation_after_result`（默认关，与上游一致）可在拿到结果后删除该对话，减少账号里累积的用户提示词。
