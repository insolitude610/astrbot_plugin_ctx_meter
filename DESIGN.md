# astrbot_plugin_ctx_meter 设计说明

一句话：**发一条 `/ctx`，立刻知道上一次真实发给模型的请求体有多大，以及被什么撑起来的。**

本文是本插件的规格书。所有关于 AstrBot 行为的断言都在 4.28.2 源码里核对过（行号以本机
`D:\AstrBotLauncher-0.1.5.5\AstrBot` 的 4.28.2 树为准），方便日后升级框架时按图索骥。
文末 §9 记录了独立复审的四轮结论——包括两个被推翻的早期论断。

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

- `@filter.on_llm_request()`：每轮真实请求记一份**字节快照**
- `@filter.on_llm_response()`：同一轮拿到**这次请求的精确 token**（`LLMResponse.usage`）
- `/ctx`：把这两样渲染出来

`/ctx` 自己**掐断默认 LLM 链路**（`event.should_call_llm(True)`），所以这两个钩子都不会为它触发，
缓存里永远是"上一次真实请求"——而那就是当前上下文的规模。

## 3. 关键机制与证据（AstrBot 4.28.2）

| 事项 | 位置 |
|---|---|
| `on_llm_request` 钩子注册 | `core/star/register/star_handler.py:433` |
| `on_llm_response` 钩子注册 | `core/star/register/star_handler.py:456-476` |
| `ProviderRequest` 字段 | `core/provider/entities.py:94-120` |
| `LLMResponse.usage` | `core/provider/entities.py:386` |
| `TokenUsage`（`input_other`/`input_cached`/`output`/`.total`） | `core/provider/entities.py:322-351` |
| runner 组装 messages 的顺序 | `core/agent/runners/tool_loop_agent_runner.py:313-328` |
| 工具 schema | `tool_set.openai_schema()` — `core/agent/tool.py:203` |
| 额外内容块序列化 | `Part.model_dump_for_context()` — `core/agent/message.py:374` |
| 掐断默认 LLM | `event.should_call_llm(True)` — `astr_message_event.py:372`，闸门在 `process_stage/stage.py:56-60` |
| 指令回复 | async generator + `yield event.plain_result(...)`（洋葱模型 `pipeline/scheduler.py:44-70`） |

### 3.1 字节数为什么能对齐真实上行（以及它**不**包含什么）

httpx 发 JSON body 时用的是一组**特定参数**：

```python
# venv/Lib/site-packages/httpx/_content.py:176-178
body = json_dumps(json, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
```

openai SDK 走同一条路径（`openai/_base_client.py:586` 把 body 交给 `json=`），没有任何 orjson
旁路；streaming 只增加字段、不改编码。照抄这组参数，本地算出的字节数与线上请求体
**同一套序列化规则**。用 `json.dumps` 的**默认**参数会得到完全不同的数字
（`ensure_ascii=True` 会把中文转义成 `\uXXXX`，长度差近三倍），所以 `measure.py` 把参数
固化成常量，并有一条测试专门锁死这件事。

**它是下界，不是等式。** `total_bytes` 只累加消息面与工具面的分段，不含 provider 信封字段
——`model`、`stream`、`stream_options`、`tool_choice`、`temperature`、`max_tokens` 等
（见 `core/provider/sources/openai_source.py:539-540,624-626`）。这些在 400 KB 的请求体上
约百字节量级，所以输出用 `≈`，文档也只声称"下界"。

### 3.2 `/ctx` 为什么只回一条、且不触发模型

`/ctx` 的 handler 写成 **async generator**：

```python
event.should_call_llm(True)
yield event.plain_result(text)
```

pipeline 是洋葱模型（`core/pipeline/scheduler.py:44-70`）：handler 的 `yield` 会把控制权交出去，
由 `ResultDecorateStage` / `RespondStage`（`core/pipeline/stage_order.py:11-12`）把消息发出去。

`process_stage/stage.py:56` 的门是
`not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm`，
**两项都能关掉它**：

1. `event.call_llm` —— 由 `should_call_llm(True)` 置位（`astr_message_event.py:372-377`）。
   这是**主守卫**：它与"消息到底有没有真的发出去"无关，因此在任何发送路径下都成立。
2. `_has_send_oper` —— 基类 `send()` 会置 `True`（`astr_message_event.py:500`）。
   这是**顺带**关掉的：只要 `send()` 最终走到基类实现就会置位。

### 3.3 token 数据源：同一个 agent 轮次的 `LLMResponse.usage`

`@filter.on_llm_response()`（`star_handler.py:456`，v4.0.0 起就有）以
`async def handler(self, event, response: LLMResponse)` 回调，`response.usage` 是
`TokenUsage | None`，含 `input_other` / `input_cached` / `output` 与 `.total`
（`entities.py:322-351,386`）。这正是 core 自己落库的那个值（写于
`agent_sub_stages/internal.py:639-648`），但**在进程内直接拿，不需要碰数据库**。

因此本插件**不读** `Conversation.token_usage`，也不用 `ProviderStat`：

- 少了 sqlalchemy / sqlmodel / ORM 依赖与随之而来的版本守卫；
- 少了"该列在有 checkpoint / 被中断 / 空回复的轮次会被跳过写入、于是读到更早轮次旧值"的问题；
- 与字节快照来自**同一轮**，两者天然一致。

`usage` 为 `None`（部分 provider / 流式分片）时该行显示"未知"。

### 3.4 版本下限与降级策略（`>=4.10.0`）

逐版本扫过 `_astrbot_hist/v*/`（32 棵树）得到每个 API 的首次出现版本：

| API | 首次出现 | 作用 | 缺失时 |
|---|---|---|---|
| `filter.on_llm_request` | v4.0.0 | 字节测量点 | 必须 |
| `filter.on_llm_response` | v4.0.0 | token 取数点 | 必须 |
| `LLMResponse.usage` / `TokenUsage` | **v4.10.0** | token 值 | 必须（决定下限） |
| `event.should_call_llm` | v4.0.0 | 主守卫 | 必须 |
| `filter.command(alias=...)` | v4.0.0 | 指令别名 | 必须 |
| `ToolSet.openai_schema` | v4.0.0 | 工具段字节 | 必须 |
| `Part.model_dump_for_context` | **v4.24.1** | 额外内容块字节 | 降级 |

**下限取最晚的那个必需项 = v4.10.0。** 唯一的降级项是额外内容块的序列化，
按阶梯退化，**不需要顶层 import 守卫**（`model_dump_for_context` 是方法，走 `getattr` 即可）：

`model_dump_for_context()`（4.24.1+）→ `model_dump()` → 纯 dict 原样 → `str()`

降级只影响**精度**，不影响可用性。

## 4. 架构

`main.py` 只做接线，行为全部放在不依赖 astrbot 的 `ctxmeter/` 包里——这样整套测试
能在没装 AstrBot 的环境里跑。

```
main.py                # Star 子类 + 2 个 hook + 1 个指令
ctxmeter/measure.py    # 纯函数 measure(req) -> Snapshot
ctxmeter/render.py     # 纯函数 render(snapshot, tokens, detail, now) -> str
tests/                 # 离线 pytest（按仓库惯例不纳入版本管理）
```

### 4.1 数据流

```
/ctx
 └─ CommandFilter 匹配（wake_prefix "/"）
    └─ handler: should_call_llm(True) → yield plain_result(render(...))
       └─ 链路掐断 ⇒ 两个钩子都不为 /ctx 触发 ⇒ 缓存始终是上一轮真实请求

真实对话轮次
 ├─ on_llm_request(event, req)   → snapshot = measure(req)      # 纯计算，无 I/O
 └─ on_llm_response(event, resp) → tokens = resp.usage          # 同一轮
    两者写进同一个 self._sessions[umo] = Entry(snapshot, tokens)  # OrderedDict，LRU 上限
```

### 4.2 为什么 hook 里不做 I/O

`req.image_urls` 是本轮附件的**引用**（路径或 URL），要还原成 base64 得读文件。
每轮都读一遍会让图片 I/O 翻倍，不可接受。

已落库的历史图片本来就是 data URL，**已经算进 `req.contexts` 了**（`assemble_context`
在 `provider/entities.py:251-274` 把引用解析成 data URL，`_save_to_history` 原样持久化）；
只有"本轮新附的图片"会少算一次，而它们在下一轮落库后自然被算准——自纠正。
所以本轮附件只**单独报数量**，不假装计入字节。

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
    toolcalls_bytes: int     # req.tool_calls_result（工具续跑附加消息）
    history_messages: int
    tool_count: int
    image_refs: int          # 本轮新附图，只计数
    audio_refs: int
    model: str | None
    taken_at: float          # time.time()

    @property
    def total_bytes(self) -> int: ...    # 分段之和；对真实 body 是下界（见 §3.1）
    @property
    def sections(self) -> tuple[tuple[str, int], ...]: ...   # 渲染用

def measure(req, now=None) -> Snapshot: ...
```

取值**一律** `getattr(x, name, default)`，保证旧版本和测试桩都不炸。任何一段抛异常
只降级为 0，绝不冒泡。

### 5.2 `render.py`

```python
@dataclass(frozen=True, slots=True)
class Tokens:
    total: int
    input_cached: int = 0
    input_other: int = 0
    output: int = 0
    at: float | None = None
    @classmethod
    def from_usage(cls, usage, now=None) -> "Tokens | None": ...   # usage 为 None 时返回 None

def render(snapshot, tokens, detail=True, now=None) -> str: ...
```

`detail=True`：
```
📦 当前对话请求体 ≈ 412.6 KB (422,512 bytes)
├ 系统提示词    12.4 KB
├ 历史上下文   388.1 KB  (62 条消息)
├ 本轮输入       1.2 KB
├ 额外内容块     0.3 KB
└ 工具 Schema   10.6 KB  (7 个)
🔢 上一次请求 token：118,432（缓存输入 98,000 · 其它输入 18,000 · 输出 2,432）
🖼 本轮附带图片 2 张（尚未编码，未计入）
🤖 模型 gpt-4o-mini · 快照 14:23:07
```

- `detail=False` 只留首行 + token 行
- `snapshot is None` → "还没有记录到本会话的请求，先随便聊一句再来看看。"
- `tokens is None` → `🔢 上一次请求 token：未知`
- 零值分段不在明细里出现（如没有工具就不显示工具行）
- 字节格式化：`<1 KiB` → B，`<1 MiB` → KB，否则 MB，一位小数

### 5.3 `main.py`

```python
class CtxMeterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None: ...
        # self._sessions: OrderedDict[str, Entry]；配置 enable/permission_mode/detail/max_sessions

    @filter.on_llm_request()                     # 字节快照
    async def _record_request(self, event, req) -> None: ...

    @filter.on_llm_response()                    # 同一轮的精确 token
    async def _record_response(self, event, response) -> None: ...

    @filter.command("ctx", alias={"请求体", "ctxsize"})
    async def ctx(self, event: AstrMessageEvent): ...
```

- 两个 hook 各自**整段 try/except**，出问题只 `logger.debug`，绝不影响真实请求
- `/ctx` 依次做：`should_call_llm(True)` → 开关 → 权限 → 取 `Entry` → `yield plain_result(...)`
- 两个 hook 通过 `event.unified_msg_origin` 写同一个 `Entry`；`on_llm_request` 新建
  （替换旧的 snapshot、**保留旧 tokens 直到同一轮的 response 到达**），`on_llm_response` 只补 tokens
- 不读数据库，**不调用** `update_conversation` / `insert_provider_stat`，不写会话状态

## 6. 配置

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | `true` | 总开关 |
| `permission_mode` | string | `everyone` | `everyone` / `admin` |
| `detail` | bool | `true` | 是否输出分段明细 |
| `max_sessions` | int | `64` | 会话缓存条数上限 |

## 7. 边界与失败模式

| 场景 | 行为 |
|---|---|
| 本会话还没有真实请求 / 冷启动 / 热重载 | 回提示语，不报错 |
| `response.usage` 为 `None` | token 行显示"未知"，字节部分照常 |
| AstrBot ≤ 4.24.1（无 `model_dump_for_context`） | 额外内容块按 `model_dump()` → dict → `str()` 降级 |
| `req.func_tool` 为 `None` | 工具段为 0，明细里不显示该行 |
| `openai_schema()` 抛异常 | 退化为工具名字节数 |
| `req` 缺字段（旧版/桩对象） | `getattr` 兜底为 0 |
| 本轮带图片/音频 | 单独报"N 张（尚未编码，未计入）" |
| 会话数超上限 | LRU 淘汰最旧，内存有界 |
| 插件内任何异常 | 两个 hook 各自全包；指令侧只回错误提示 |
| 非 OpenAI 系 provider | 工具段按 OpenAI schema 估，**偏差仅限工具段** |
| 真实 body 的 provider 信封字段 | 不计入 `total_bytes`（见 §3.1），故输出用 `≈` |

**本插件刻意不调 `stop_event()`**，因为框架里它有真实毛病：

- 它会让 `scheduler.py:56` 直接 `break`，于是 **handler 后续 `yield` 的结果再也发不出去**
  （`stop_event(); yield; yield plain_result` 里第二条会丢）；
- 若某个第三方 `on_decorating_result` 钩子在装饰阶段停掉事件，`respond/stage.py:329`
  会跳过 `clear_result`，外层循环回头再次经过装饰/发送阶段，**可能重复发送**。

`should_call_llm(True)` 没有这两个副作用，所以用它。

## 8. 测试策略

离线 pytest（`python -m pytest`，不需要 AstrBot）。必测：

1. **黄金字节数**：手工构造等价 payload，断言 `total_bytes` 与按 `WIRE_DUMPS` 序列化长度相等
2. **锁死 wire 参数**：含中文时断言**不等于** `json.dumps` 默认参数的长度
3. `extra_user_content_parts` 走 `model_dump_for_context()`，纯 dict 原样透传
4. **降级阶梯**：对象只有 `model_dump()` → 用它；只有 dict → 原样；都没有 → `str()`
5. `func_tool=None` / `openai_schema` 抛异常 → 不抛
6. **裸 `object()` 当 req** → 全 0，不抛
7. 图片/音频只计数、不进字节
8. `tool_calls_result` 非空时计入 `total_bytes`，为空时该段为 0 且渲染隐藏
9. `render`：detail 开关、B/KB/MB 边界、零值分段隐藏
10. `Tokens.from_usage`：`usage=None` → `None`；正常对象 → 三段 + total 正确
11. `render(snapshot, None, ...)` → "未知" 且不抛
12. 接线：`/ctx` 恰好一次 `should_call_llm(True)` + 恰好一条 `plain_result`
13. LRU 淘汰
14. 两个 hook 各自抗炸（monkeypatch `measure` 抛异常 → 不冒泡）
15. `on_llm_response` 保留同一 `Entry` 的 snapshot，只补 tokens
16. `admin` 模式下非管理员被拒

另外用 AstrBot 的 venv 做 import 冒烟，证明能在真框架里加载。

## 9. 独立复审记录（四轮）

方案闸门送审四轮，每轮都被判 `FAIL`，但结论逐轮收敛。以下按"被推翻的是什么"记录。

**第一轮**：因送审载荷漏掉 `PROPOSER_MODELS` 元数据构成强制 FAIL（送审方失误）。
技术反驳两条，均已核实采纳：

1. "用 `stop_event()` 会让回复发不出去"是**错的**——`scheduler.py:60` 的 `break` 只跳内层
   `async for`，外层 `for i` 仍会走到 `ResultDecorateStage` / `RespondStage`。
   已删除该论断，换成 `stop_event()` 真正的两个毛病（见 §7）。
2. "`Conversation.token_usage` 就是最近一次请求的总量"**站不住**——有 checkpoint / 被中断 /
   空回复的轮次会跳过写入，读到的是更早轮次的旧值。

**第二轮**：`>=4.11.0` 下限**站不住**。`ProviderStat` 实际是 v4.22.3 才有的表、
`model_dump_for_context` 是 v4.24.1，而顶层 `import` 失败是 `ImportError`，包在读外围的
try/except 救不了。（另外确认了：历史图片确实带 data URL，§4.2 的自纠正论证成立；
缓存生命周期无隐患，不需要 `terminate()`。）

**第三轮**：又两条，其中一条**后来被证明是错的**（见第四轮）。

**第四轮**：否掉了前一轮采纳的说法，并提出了一个更好的机制。

3. §3.2 的"平台相关"说法**不成立**。经完整追链：`qqofficial_message_event.py:215-217`
   的 `send()` → `_post_send()` → `_post_send_one()` → **`await super().send(...)` 于 `:619`**
   → 基类照样置 `_has_send_oper`（`astr_message_event.py:500`）；只有 `386/398/425/462`
   的提前 return 会绕过。**没有哪个平台的 `send()` 永远到不了基类。**
   → 这同时暴露了**送审方（本 agent）的核实失误**：第三轮采纳它时只看了 `send()` 本体，
   没有往下追一层调用。教训：采纳复审意见同样要完整追链，不能只看断言处的几行。
   → §3.2 与测试 12 已按正确理由重写：`should_call_llm(True)` 是**主守卫**（与发送路径无关），
   `_has_send_oper` 是顺带关掉的第二道。
4. **采纳复审器提出的更优机制**：用 `@filter.on_llm_response()` 在进程内取
   `LLMResponse.usage`，取代原先"`ProviderStat` 表 + `conversation_manager` 退回"的两档
   DB 方案。该方案删掉了 ORM 依赖、顶层 import 守卫、以及 4.11.0↔4.22.3 的版本纠缠，
   并把 token 与字节快照绑定到**同一轮**。§3.3 / §5.3 已据此重写。
   （同时修正了 `select(Conversation)` 的说法：它**会抛异常**，不是静默失败——
   `Conversation` 是非表 dataclass，真表是 `ConversationV2`。）

**残余风险（未消除，已记录）**：`on_llm_response` 每次 agent 轮次回调一次，拿到的是该轮的
最终 `LLMResponse`；被中断的轮次其 `usage` 语义未在 4.11.0 实机验证。影响仅限于 token 行
显示"未知"。另：本机只有 4.28.x 运行环境，低版本无法实机冒烟。

## 10. 明确不做

- 不读数据库（`ProviderStat` / `Conversation.token_usage` 都不用）
- 不把本轮未落库的图片真去 base64 精确计量（自纠正，且会带来每轮 I/O）
- 不做 anthropic / gemini 的原生工具 schema 计量
- 不做 Dashboard 面板
- 不改动任何现有插件，也不改 AstrBot core
