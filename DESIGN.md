# astrbot_plugin_ctx_meter 设计说明

一句话：**发一条 `/ctx`，立刻知道上一次真实发给模型的请求体有多大，以及被什么撑起来的。**

本文是本插件的规格书。所有关于 AstrBot 行为的断言都在 4.28.2 源码里核对过，行号随之附上，
方便日后升级框架时按图索骥。

---

## 1. 问题

AstrBot 的上下文只会越长越贵，但你在聊天里看不见它。想知道"现在我这条请求到底多大"，
只能离开聊天去翻数据库或日志。core 自带的 `/stats` 报的是**当前对话累计** token
（`astrbot/builtin_stars/builtin_commands/commands/conversation.py:172-229`，对 `ProviderStat`
做 SUM），既不是字节数，也不是"这一次"。

## 2. 为什么必须挂在 `on_llm_request` 上

指令处理函数运行时，请求还没组装。系统提示词里含人格、内置注入、其它插件的注入，
在指令阶段根本凑不齐——**只有 `on_llm_request` 能拿到完全组装好的 `ProviderRequest`**。

于是本插件用"记录 + 回放"两步走：

- `@filter.on_llm_request()`：每轮真实请求记一份快照
- `/ctx`：把最近的快照渲染出来

`/ctx` 自己**掐断默认 LLM 链路**（`event.should_call_llm(True)`），所以 `on_llm_request`
不会为它触发，缓存里永远是"上一次真实请求"——而那就是当前上下文的规模。

## 3. 关键机制与证据（AstrBot 4.28.2）

| 事项 | 位置 |
|---|---|
| `on_llm_request` 钩子注册 | `astrbot/core/star/register/star_handler.py:433` |
| `ProviderRequest` 字段 | `astrbot/core/provider/entities.py:94-120` |
| runner 组装 messages 的顺序 | `astrbot/core/agent/runners/tool_loop_agent_runner.py:313-328` |
| 工具 schema | `tool_set.openai_schema()` — `astrbot/core/agent/tool.py:203` |
| 额外内容块序列化 | `Part.model_dump_for_context()` — `astrbot/core/agent/message.py:374` |
| 最近一次请求的 token 总数 | `Conversation.token_usage` — `astrbot/core/db/po.py:588-589` |
| 该字段的写入点 | `.../agent_sub_stages/internal.py:639-648` |
| 掐断默认 LLM | `event.should_call_llm(True)` — `astr_message_event.py:372`，闸门在 `process_stage/stage.py:56-60` |
| 指令回复的正确写法 | async generator + `yield event.plain_result(...)`（洋葱模型 `pipeline/scheduler.py:44-70`） |

### 3.1 字节数为什么能对齐真实上行

httpx 发 JSON body 时用的是一组**特定参数**：

```python
# venv/Lib/site-packages/httpx/_content.py:176-178
body = json_dumps(json, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
```

照抄这组参数，本地算出的字节数就和线上请求体一致。用 `json.dumps` 的**默认**参数会得到
完全不同的数字（`ensure_ascii=True` 会把中文转义成 `\uXXXX`，长度差近三倍），所以
`measure.py` 把参数固化成常量，并有一条测试专门锁死这件事。

### 3.2 为什么必须用 `should_call_llm` 而不是 `stop_event`

pipeline 是洋葱模型：指令 handler 的 `yield` 会把控制权交出去，**由后续 stage 把消息发出去**。
在 `yield` 之前调 `stop_event()` 会让调度器直接中断，消息根本发不出去
（`star_request.py:53` 随后还会 `clear_result()`）。而 `should_call_llm(True)` 只置一个
`event.call_llm` 标志，不影响回复通道，正好只掐掉"再让模型答一句"。

### 3.3 版本下限 `>=4.11.0`

逐版本扫过 `_astrbot_hist/v*/`：

- `token_usage` 列：**v4.11.0** 起才有
- `on_llm_request`、`openai_schema`：v4.0.0 起就有

所以下限由 `token_usage` 决定。

## 4. 架构

`main.py` 只做接线，行为全部放在不依赖 astrbot 的 `ctxmeter/` 包里——这样整套测试
能在没装 AstrBot 的环境里跑。

```
main.py                # Star 子类 + 1 个 hook + 1 个指令
ctxmeter/measure.py    # 纯函数 measure(req) -> Snapshot
ctxmeter/render.py     # 纯函数 render(snapshot, token_usage, detail, now) -> str
tests/                 # 离线 pytest（按仓库惯例不纳入版本管理）
```

### 4.1 数据流

```
/ctx
 └─ CommandFilter 匹配（wake_prefix "/"）
    └─ handler: should_call_llm(True) → yield plain_result(render(...))
       └─ 链路已掐断 ⇒ on_llm_request 不为 /ctx 触发 ⇒ 快照始终是上一轮真实请求

真实对话轮次
 └─ on_llm_request(event, req)
    └─ snapshot = measure(req)      # 纯计算，无 I/O
    └─ snapshots[umo] = snapshot    # OrderedDict，LRU 上限 max_sessions
```

### 4.2 为什么 hook 里不做 I/O

`req.image_urls` 是本轮附件的**引用**（路径或 URL），要还原成 base64 得读文件。
每轮都读一遍会让图片 I/O 翻倍，不可接受。

已落库的历史图片本来就是 data URL，**已经算进 `req.contexts` 了**；只有"本轮新附的图片"
会少算一次，而它们在下一轮落库后自然被算准——自纠正。所以本轮附件只**单独报数量**，
不假装计入字节。

## 5. 模块规格

### 5.1 `measure.py`

```python
WIRE_DUMPS = {"ensure_ascii": False, "separators": (",", ":"), "allow_nan": False}

def wire_bytes(obj) -> int: ...          # 序列化失败时用 default=str 兜底

@dataclass(frozen=True, slots=True)
class Snapshot:
    system_bytes: int        # req.system_prompt
    history_bytes: int       # req.contexts
    prompt_bytes: int        # req.prompt
    extra_bytes: int         # req.extra_user_content_parts
    tools_bytes: int         # req.func_tool
    history_messages: int
    tool_count: int
    image_refs: int          # 本轮新附图，只计数
    audio_refs: int
    model: str | None

    @property
    def total_bytes(self) -> int: ...

def measure(req) -> Snapshot: ...
```

取值**一律** `getattr(x, name, default)`，保证旧版本和测试桩都不炸。任何一段抛异常
只降级为 0，绝不冒泡。

### 5.2 `render.py`

`render(snapshot, token_usage, detail=True, now=None) -> str`

```
📦 当前对话请求体 ≈ 412.6 KB (422,512 bytes)
├ 系统提示词    12.4 KB
├ 历史上下文   388.1 KB  (62 条消息)
├ 本轮输入       1.2 KB
├ 额外内容块     0.3 KB
└ 工具 Schema   10.6 KB  (7 个)
🔢 上一次请求 token：118,432
🖼 本轮附带图片 2 张（尚未编码，未计入）
🤖 模型 gpt-4o-mini · 快照 14:23:07
```

- `detail=False` 只留首行 + token 行
- `snapshot is None` → "还没有记录到本会话的请求，先随便聊一句再来看看。"
- `token_usage` 为 `None` 或 `0` → "未知"（`po.py:589` 明确 0 表示未知）
- 字节格式化：`<1 KiB` → B，`<1 MiB` → KB，否则 MB，一位小数

### 5.3 `main.py`

- `@filter.on_llm_request()`：整段 try/except，测量出问题只 `logger.debug`，绝不影响真实请求
- `@filter.command("ctx", alias={"请求体", "ctxsize"})`
- `_read_token_usage`：`conversation_manager` 只读；任一环节异常返回 `None`。
  **绝不调用 `update_conversation`**

## 6. 配置

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | `true` | 总开关 |
| `permission_mode` | string | `everyone` | `everyone` / `admin` |
| `detail` | bool | `true` | 是否输出分段明细 |
| `max_sessions` | int | `64` | 快照缓存会话数上限 |

## 7. 边界与失败模式

| 场景 | 行为 |
|---|---|
| 本会话还没有真实请求 / 冷启动 / 热重载 | 回提示语，不报错 |
| `token_usage == 0` | 显示"未知" |
| `req.func_tool` 为 `None` | 工具段为 0 |
| `openai_schema()` 抛异常 | 退化为工具名字节数 |
| `req` 缺字段（旧版/桩对象） | `getattr` 兜底为 0 |
| 本轮带图片/音频 | 单独报"N 张（尚未编码，未计入）" |
| 会话数超上限 | LRU 淘汰最旧，内存有界 |
| 插件内任何异常 | hook 侧全包；指令侧只回错误提示 |
| 非 OpenAI 系 provider | 工具段按 OpenAI schema 估，**偏差仅限工具段** |

## 8. 测试策略

离线 pytest（`python -m pytest`，不需要 AstrBot）。必测：

1. **黄金字节数**：手工构造等价 payload，断言 `total_bytes` 与按 `WIRE_DUMPS` 序列化长度相等
2. **锁死 wire 参数**：含中文时断言**不等于** `json.dumps` 默认参数的长度
3. `extra_user_content_parts` 走 `model_dump_for_context()`，纯 dict 原样透传
4. `func_tool=None` / `openai_schema` 抛异常 → 不抛
5. **裸 `object()` 当 req** → 全 0，不抛
6. 图片/音频只计数、不进字节
7. `render`：detail 开关、`token_usage=0`、B/KB/MB 边界
8. 接线：`/ctx` 恰好一次 `should_call_llm(True)` + 恰好一条 `plain_result`
9. LRU 淘汰
10. hook 抗炸（measure 抛异常不冒泡）
11. `admin` 模式下非管理员被拒

另外用 AstrBot 的 venv 做 import 冒烟，证明能在真框架里加载。

## 9. 明确不做

- 不把本轮未落库的图片真去 base64 精确计量（自纠正，且会带来每轮 I/O）
- 不做 anthropic / gemini 的原生工具 schema 计量
- 不做 `ProviderStat` 的单次请求 input cached/other/output 拆解
- 不做 Dashboard 面板
- 不改动任何现有插件，也不改 AstrBot core
