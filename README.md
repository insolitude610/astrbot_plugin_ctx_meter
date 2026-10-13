# 请求体计量 (/ctx)

发一条 `/ctx`，看看这轮喂给模型的请求体到底有多大。

AstrBot 的上下文只会越长越贵，但你在聊天里看不见它。这个插件在每次真实请求前记一份快照，
你随时发一条指令就能把上一次请求的尺寸调出来。

## 效果

```
/ctx
```

```
📦 当前对话请求体 ≈ 412.6 KB (422,512 bytes)
├ 系统提示词  12.4 KB
├ 历史上下文  388.1 KB  (62 条消息)
├ 本轮输入  1.2 KB
└ 工具 Schema  10.6 KB  (7 个)
🔢 本轮最后一次调用 token：118,432（缓存输入 98,000 · 其它输入 18,000 · 输出 2,432）
🖼 本轮附带图片 2 张（尚未编码，未计入）
🤖 模型 gpt-4o-mini · 快照 14:23:07
```

零值的分段不会出现（没有挂工具就没有工具那一行）。把配置里的 **detail** 关掉，
就只回总字节数和 token 两行。

## 安装

把整个目录放到 AstrBot 的 `data/plugins/` 下，然后在 WebUI 插件页启用（或重启 AstrBot）。

- 需要 **AstrBot >= 4.10.0**
- 指令：`/ctx`，别名 `/ctxsize`、`/请求体`
- 全平台可用；不依赖任何平台特有接口

## 它到底量的是什么

在请求即将发给模型的那一刻，把已经组装好的请求体按 **httpx 真实的 JSON 序列化参数**
（`ensure_ascii=False`、紧凑分隔符）算一遍 UTF-8 字节数——也就是线上请求体用的同一套编码规则。
所以这个数字和你在抓包里看到的长度是同一口径。

**它量的是"快照那一刻组装好的请求"**，覆盖五段：系统提示词、历史上下文、本轮输入、
额外内容块、工具 Schema。有两个方向上的偏差要知道：

- **偏小**：不含 provider 信封字段（`model`、`stream`、`stream_options`、`tool_choice`、
  `temperature`、`max_tokens` 等），也不含**本轮新附、尚未编码**的图片，同样不含工具调用结果
  （理由见下）。前者在几百 KB 的请求体上只有百字节量级，所以输出用 `≈`。
- **偏大**：框架在快照之后可能还会裁剪请求——开了 `tool_schema_mode: skills_like` 的话，
  runner 会把工具集换成不带参数的轻量版（本插件记的是完整版）；provider 的 `modalities`
  不含 `image`/`audio` 时，媒体会被换成占位符（本插件记的是实际长度）。

**工具调用结果那一段是不量的，这是有意的。** 它在框架自身的流程里于快照时刻恒为空
（runner 是在本轮首个请求之后才把工具结果挂上去的）；而真有值的时候，它的线上形态也**不是**
`to_openai_messages()` 的返回值——provider 之后还会跑 `_sanitize_assistant_messages` 与
`_finally_convert_payload`，重写 assistant 的 content、丢弃孤儿 tool 消息。与其复刻一个有损清洗器
去凑一个站不住的数，不如明确不算。

拿不准形状的东西**一律计 0**，而不是拿 `str()` 凑一个数——宁可少算也不编。

**图片怎么算**：已经落进历史记录的图片（base64 data URL）是历史上下文的一部分，**已经算进去了**。
只有**本轮新附**的图片此刻还只是文件引用、没编码，所以单独报个数量、不假装计入——
它们在下一轮落库后会自然被算准。

**token 那一行**来自 `on_llm_response`，是**本轮最后一次 provider 调用**的用量，
而字节快照取自本轮**开头那次**请求。带工具调用的轮次里这是两次不同的 HTTP 请求，
所以标签写的是"本轮最后一次调用"而不是"上一次请求"。本轮失败或被中断时显示"未知"
（不会拿上一轮的数字来充数）。

## 配置

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | `true` | 总开关 |
| `permission_mode` | string | `everyone` | `everyone` 任何人可用 / `admin` 仅管理员 |
| `detail` | bool | `true` | 是否输出分段明细 |
| `max_sessions` | int | `64` | 快照缓存保留的会话数上限（LRU） |

## 值得知道的几点

- **`/ctx` 不消耗 token**：它掐断默认的模型请求链路，只发一条诊断消息，不会让模型再答一句。
- **只读**：不写会话历史、不写数据库、不改任何现有插件或 AstrBot 本体。
- **量出来的东西不会泄露内容**：缓存里只存数字、条数和模型名，不存对话文本。
- **内存有界**：每个会话一条快照，超出 `max_sessions` 时淘汰最久未使用的。
- **冷启动后为空**：快照活在内存里，AstrBot 重启或插件重载后需要先正常聊一句。
- **非 OpenAI 系 provider**（anthropic / gemini 等）的工具段按 OpenAI schema 估算，
  偏差仅限工具段。
- **≤ 4.24.1 的老版本**上"额外内容块"一段会退化成近似值（框架那时还没有
  `model_dump_for_context`），其余分段不受影响。

## 开发

行为全部在无框架依赖的 `ctxmeter/` 包里，`main.py` 只做接线，所以离线测试不需要装 AstrBot。
**注意 `tests/` 不纳入版本管理**（本仓库惯例，见 `.gitignore`），所以从 GitHub 克隆下来的副本里
没有测试文件，这两条命令只在本地工作副本里可跑：

```bash
python -m pytest tests -q
```

对着真实 AstrBot 的导入冒烟（需要 AstrBot 的 venv，那个环境里没有 pytest）：

```bash
& 'D:\AstrBotLauncher-0.1.5.5\AstrBot\venv\Scripts\python.exe' tests\smoke_import.py
```

设计说明（含每一项 Framework 断言的源码出处、以及独立复审五轮的结论）见 [DESIGN.md](DESIGN.md)。

## License

MIT
