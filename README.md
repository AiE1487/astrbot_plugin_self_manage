# astrbot_plugin_self_manage

让 AstrBot 里的 LLM Agent 获得"自我维护"能力：

1. **更新人格提示词** —— Agent 把一个工作区文件的路径告诉插件，插件读取文件全文，
   替换**当前会话绑定人格**的 `system_prompt`（写入人格数据库记录）。
2. **读取近期日志** —— Agent 获取最近 N 分钟（默认 5 分钟）的 AstrBot 日志，
   支持按 `debug / info / warn / error` 过滤，用于自检报错原因。
3. **人工验证入口** —— `/selfmanage_logs`、`/selfmanage_persona` 两条聊天指令。

## LLM 工具（Agent 调用）

| 工具 | 参数 | 说明 |
|---|---|---|
| `selfmanage_update_persona_prompt` | `file_path`（必填） | 读取文件全文，替换当前会话人格记录的提示词。返回实际生效的人格 id 与修改摘要 |
| `selfmanage_get_recent_logs` | `minutes`、`levels`（均可选） | `minutes` 默认 5；`levels` 逗号分隔 `debug/info/warn/error`（warn 是 warning 别名），留空返回全部 |

### 人格更新的语义

- 解析"当前会话人格"的优先级由新版本宿主的
  `persona_manager.resolve_selected_persona()` 决定（会话级强制人格 → 会话绑定
  人格 → 默认人格）；老版本宿主自动回退为：会话绑定人格 → `get_default_persona_v3`。
- 只替换 `system_prompt`，不影响开局对话（begin_dialogs）、工具列表（tools）等
  其它人格字段（已核对 AstrBot db 层语义：未传字段不写库）。
- **持久修改**，下一次 LLM 请求起生效。
- 以下几种情况工具会明确拒绝并告知 Agent：会话人格为 `[%None]` 或未绑定；
  webchat 专用默认人格（`_chatui_default_`，不落库）；人格记录已被删除。
- 提示词文件必须真实存在于 **Bot 主机**，相对路径按 AstrBot 工作根目录解析。

### 日志的语义

- 内存缓冲记录插件挂载之后的日志（底层往 loguru 挂 sink，覆盖 AstrBot 全部
  日志流；loguru 不可用时降级挂标准 logging root handler）。
- 查询时间早于插件挂载时刻时，自动扫描 `data/logs/*.log*` 兜底解析（宽容正则，
  traceback 续行并入上一条）。
- 返回带「超出行数上限已截断」等提示，不会无限撑大模型上下文。

## 聊天指令

| 指令 | 权限 | 功能 |
|---|---|---|
| `/selfmanage_logs [分钟数] [级别]` | 管理员 | 人工查看近期日志（等价于日志工具） |
| `/selfmanage_persona` | 所有人 | 只读查看当前会话人格 id 与提示词 |

## 配置项

安装后 `[AstrBot] → 插件管理 → 本插件 → 配置`（默认值都是开箱可用的）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `require_admin` | false | 两个 LLM 工具是否仅限管理员触发（不影响聊天指令，日志指令本来就限管理员） |
| `log_buffer_size` | 4000 | 日志内存缓冲条数（**修改后需重载插件**） |
| `log_default_minutes` | 5 | 日志工具默认回看分钟数 |
| `log_max_return_lines` | 300 | 日志单次返回行数上限 |
| `log_file_fallback` | true | 历史时间窗是否扫描 `data/logs` 兜底 |
| `persona_prompt_max_kb` | 100 | 提示词文件大小上限（KB） |

## 安装

- 从本地上传：把打包的 zip 在 `插件管理 → 安装插件` 里上传，或解压到
  `AstrBot/data/plugins/` 后重载。
- 无第三方依赖（requirements.txt 不存在是正常的），Python ≥ 3.12。

## 兼容性

`astrbot_version: >=4.16,<5`。人格/会话接口按 AstrBot 4.28~4.29 源码核对；
`resolve_selected_persona` 在老宿主不存在时会自动走回退链。

## 风险提示

- 人格更新影响该人格下所有会话的 system prompt，是写入数据库的持久修改。
  介意的话请开 `require_admin`。
- 日志可能包含 token 等敏感信息（取决于宿主），`/selfmanage_logs` 已限管理员，
  工具侧请按需开 `require_admin`。

## License / 来源

借鉴 `astrbot_plugin_worldbook`（llm_tool 装饰器式写法）与
`astrbot_plugin_proactive_chat`（persona_manager / conversation_manager 的
使用姿势）的公开实现模式。代码是全新实现的，遵守各源项目开源许可。
