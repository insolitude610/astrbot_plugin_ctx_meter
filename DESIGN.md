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
| 掐断默认 LLM | `event.should_call_llm(True)` — `astr_message_event.py:372`，闸门在 `process_stage/stage.py:56-60` |
| 单次请求的 token 明细 | `ProviderStat` 表 —— 每个 agent 响应插一行：`core/db/sqlite.py:290-332`，插入点 `agent_sub_stages/internal.py:688` |
| 指令回复的正确写法 | async generator + `yield event.plain_result(...)`（洋葱模型 `pipeline/scheduler.py:44-70`） |

### 3.1 字节数为什么能对齐真实上行（以及它**不**包含什么）

httpx 发 JSON body 时用的是一组**特定参数**：

```python
# venv/Lib/site-packages/httpx/_content.py:176-178
body = json_dumps(json, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
```

照抄这组参数，本地算出的字节数与线上请求体**同一套序列化规则**。用 `json.dumps` 的**默认**参数
会得到完全不同的数字（`ensure_ascii=True` 会把中文转义成 `\uXXXX`，长度差近三倍），所以
`measure.py` 把参数固化成常量，并有一条测试专门锁死这件事。

**它是下界，不是等式。** `total_bytes` 只累加消息面与工具面的五个（外加 `tool_calls_result`）分段，
不含 provider 信封字段——`model`、`stream`、`stream_options`、`tool_choice`、`temperature`、
`max_tokens` 等（见 `core/provider/sources/openai_source.py:539-540,624-626`）。
这些在 400 KB 的请求体上约百字节量级，所以输出用 `≈`，文档也只声称"下界"。

### 3.2 `/ctx` 为什么只回一条、且不触发模型

`/ctx` 的 handler 写成 **async generator**：

```python
event.should_call_llm(True)
yield event.plain_result(text)
```

pipeline 是洋葱模型（`core/pipeline/scheduler.py:44-70`）：handler 的 `yield` 会把控制权交出去，
由 `ResultDecorateStage` / `RespondStage`（`core/pipeline/stage_order.py:11-12`）把消息发出去。

**两道闸门，且哪一道生效取决于平台：**

1. **`_has_send_oper`** —— 基类 `send()` 会置 `True`（`astr_message_event.py:500`）。
   发过消息之后 `process_stage/stage.py:57` 的 `not event._has_send_oper` 就为假，整块跳过。
2. **`event.call_llm`** —— `should_call_llm(True)` 置位后 `stage.py:59` 的 `not event.call_llm` 为假。

关键在于**平台子类可以重写 `send()` 而漏掉 `_has_send_oper`**：QQ 官方机器人就是这样——
`platform/sources/qqofficial/qqofficial_message_event.py:215-217` 的 `send()` 只写
`send_buffer` 再调（已废弃的）`_post_send()`，**既不调 `super().send()` 也不置 `_has_send_oper`**。
在那条路径上，第一道闸门是开的，**只有 `should_call_llm(True)` 能挡住"指令回完之后又让模型答一句"**。

所以 `should_call_llm(True)` 不是装饰，是**必需的第二个守卫**（代价为零）。

> **一处已修正的早期错误**：本设计初稿声称"用 `event.stop_event()` 代替 `should_call_llm` 会让回复发不出去"。
> 独立复审指出这是错的：`scheduler.py:60` 的 `break` 只跳出**内层** `async for`，外层 `for i`
> 仍会继续走到 `ResultDecorateStage` / `RespondStage`，回复照样发得出去。
> 该论断已从设计中删除。`stop_event()` 真实存在的两个毛病是另外两个，见 §7。

### 3.3 token 数据源：以 `ProviderStat` 为准

`Conversation.token_usage`（`core/db/po.py:588-589`）看着像"最近一次请求的总 token"，但**不可靠**：

- `agent_sub_stages/internal.py:597` 传的是 `None if has_checkpoint else req.conversation.token_usage`，
  而 `sqlite.py:629` 遇到 `None` 会**跳过该列**，于是旧值原样留着；
- 写入被 `if runner_stats:` 门控，且在 `internal.py:604`（有 checkpoint）、`internal.py:622`（空回复）
  会提前返回，**这些轮次根本不更新**。

所以被中断 / 空回复 / checkpoint 的轮次之后，读到的是**更早轮次**的数。

改用 `ProviderStat`：它由 `internal.py:688` 在**每个 agent 响应**后插一行
（`sqlite.py:290` 注释原文 "Insert a provider stat record for a single agent response"），
带 `conversation_id`、`token_input_cached`、`token_input_other`、`token_output`、`status`
（completed / aborted / error）与时间戳。

取值链（每层都 try/except，只读）：

1. `ProviderStat` 里本对话最新一行（`agent_type == "internal"`，按时间倒序取 1 条）→ 显示三段明细 + 状态
2. 读不到就退回 `Conversation.token_usage` → 标注"**可能来自更早轮次**"
3. 都没有 → "未知"

### 3.4 版本下限与降级策略（`>=4.11.0`）

逐版本扫过 `_astrbot_hist/v*/`（32 棵树）得到每个 API 的首次出现版本：

| API | 首次出现 | 作用 |
|---|---|---|
| `filter.on_llm_request` | v4.0.0 | 测量点 |
| `ToolSet.openai_schema` | v4.0.0 | 工具段字节 |
| `event.should_call_llm` | v4.0.0 | 兜底掐断 |
| `Context.get_db` | v4.0.0 | 读 token |
| `Conversation.token_usage` | **v4.11.0** | token 退回档 |
| `ProviderStat` 表 | **v4.22.3** | token 首选档 |
| `Part.model_dump_for_context` | **v4.24.1** | 额外内容块字节 |

**下限取最老的那个必需项 = v4.11.0。** 其余两个更晚的 API 一律做成优雅降级，
代价是不能在顶层直接 import（否则 4.11–4.22.2 上直接 `ImportError`，
而包在 *读* 外围的 try/except 救不了 import 期）：

- `ProviderStat` / `select` / `col` 用 `try: ... except Exception: X = None` 顶层守卫；
  运行时任一为 `None` 就跳过 ProviderStat 档，直接走 `Conversation.token_usage`
- 额外内容块按阶梯降级：`model_dump_for_context()`（4.24.1+）→ `model_dump()` → 纯 dict → `str()`

降级只影响**精度**，不影响可用性；`/ctx` 在 4.11.0 上仍然报得出字节数与 token（退回档）。

## 4. 架构

`main.py` 只做接线，行为全部放在不依赖 astrbot 的 `ctxmeter/` 包里——这样整套测试
能在没装 AstrBot 的环境里跑。

```
main.py                # Star 子类 + 1 个 hook + 1 个指令
ctxmeter/measure.py    # 纯函数 measure(req) -> Snapshot
ctxmeter/render.py     # 纯函数 render(snapshot, tokens, detail, now) -> str
tests/                 # 离线 pytest（按仓库惯例不纳入版本管理）
```

### 4.1 数据流

```
/ctx
 └─ CommandFilter 匹配（wake_prefix "/"）
    └─ handler: should_call_llm(True) → yield plain_result(render(...))
       └─ 链路掐断 ⇒ on_llm_request 不为 /ctx 触发 ⇒ 快照始终是上一轮真实请求
       └─ 顺带读一条 ProviderStat（只读）

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
    toolcalls_bytes: int     # req.tool_calls_result（工具续跑附加消息）
    history_messages: int
    tool_count: int
    image_refs: int          # 本轮新附图，只计数
    audio_refs: int
    model: str | None
    taken_at: float          # 快照时间（time.time()）

    @property
    def total_bytes(self) -> int: ...   # 六段之和；对真实 body 是下界（见 §3.1）
    @property
    def sections(self) -> tuple[tuple[str, int], ...]: ...   # 渲染用，零值段由 render 决定是否隐藏

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
    status: str = ""         # completed / aborted / error
    from_stat: bool = False  # True = 来自 ProviderStat（是"上一次请求"）
    at: float | None = None

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
- token 行三态：
  - `tokens.from_stat` → `🔢 上一次请求 token：…`（带三段明细）
  - 退回 `Conversation.token_usage` → `🔢 框架最近记录 token：…（可能来自更早轮次）`
  - 都没有 → `🔢 token：未知`
  - `status != "completed"` 时追加 ` · 状态 aborted/error`
- 字节格式化：`<1 KiB` → B，`<1 MiB` → KB，否则 MB，一位小数

### 5.3 `main.py`

- `@filter.on_llm_request()`：整段 try/except，测量出问题只 `logger.debug`，绝不影响真实请求
- `@filter.command("ctx", alias={"请求体", "ctxsize"})`
- `_read_tokens(event)` 两档，全程只读，**绝不调用 `update_conversation` / `insert_provider_stat`**：
  1. **首选档**：`ProviderStat` 里本对话最新一行（`agent_type == "internal"`，按时间倒序 limit 1）。
     符号在 4.22.2 及以前不存在，靠顶层守卫置 `None` 后跳过本档。
  2. **退回档**：`await conversation_manager.get_curr_conversation_id(umo)` →
     `await conversation_manager.get_conversation(umo, cid)` → `.token_usage`。
     **必须走 `conversation_manager`，不能 `select(Conversation)`**：`Conversation`
     （`core/db/po.py:569`）是**非表的普通 dataclass**，真表是 `ConversationV2`；
     管理器在第 80-90 行把 `conv_v2.token_usage` 映射进这个数据类（`conversation_mgr.py:89`）。
     直接查库只会静默失败、永远退化成"未知"。
  3. 两档都失败 → 返回 `None`，渲染成"未知"。
- DB 访问照抄 core 的写法（`builtin_commands/commands/conversation.py:1-12`），
  **但全部加顶层守卫**，因为这几个符号在 4.22.2 及以前不存在：

```python
try:                                            # 4.22.3 起才有这张表
    from astrbot.core.db.po import ProviderStat
    from sqlalchemy import select
    from sqlmodel import col
except Exception:                               # noqa: BLE001 - 旧版本上必须能 import
    ProviderStat = select = col = None
```

`_read_tokens` 在 `ProviderStat is None` 时直接跳过首选档；DB 会话用
`async with self.context.get_db().get_db() as session:`，整段 try/except。

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
| 无 `ProviderStat` 行（旧版本 / 从未成功请求） | 退回 `Conversation.token_usage`，并标注可能过期 |
| 两者都拿不到 | 显示"未知" |
| AstrBot ≤ 4.22.2（无 `ProviderStat` 表） | 顶层守卫把符号置 `None`，直接走退回档，**不 ImportError** |
| AstrBot ≤ 4.24.1（无 `model_dump_for_context`） | 额外内容块按 `model_dump()` → dict → `str()` 阶梯降级，字节数为近似值 |
| 真实 body 的 provider 信封字段 | 不计入 `total_bytes`（见 §3.1），故输出用 `≈` |
| `req.func_tool` 为 `None` | 工具段为 0 |
| `openai_schema()` 抛异常 | 退化为工具名字节数 |
| `req` 缺字段（旧版/桩对象） | `getattr` 兜底为 0 |
| 本轮带图片/音频 | 单独报"N 张（尚未编码，未计入）" |
| 会话数超上限 | LRU 淘汰最旧，内存有界 |
| 插件内任何异常 | hook 侧全包；指令侧只回错误提示 |
| 非 OpenAI 系 provider | 工具段按 OpenAI schema 估，**偏差仅限工具段** |

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
4. `func_tool=None` / `openai_schema` 抛异常 → 不抛
5. **裸 `object()` 当 req** → 全 0，不抛
6. 图片/音频只计数、不进字节
7. `render`：detail 开关、B/KB/MB 边界
8. **token 三态**：`from_stat=True` 出明细；退回态出"可能来自更早轮次"；都无 → "未知"；
   `status="aborted"` 追加状态
9. 接线：`/ctx` 恰好一次 `should_call_llm(True)` + 恰好一条 `plain_result`。
   **桩事件故意不置 `_has_send_oper`**（模拟 QQ 官方那条重写 `send()` 的路径），
   断言 `call_llm` 仍被置位——这是防止"指令之后又多一句模型回复"的唯一守卫（§3.2）
10. LRU 淘汰
11. hook 抗炸（measure 抛异常不冒泡）
12. **DB 读失败时静默降级**（桩 session 抛异常 → 仍能出数）
13. `admin` 模式下非管理员被拒
14. **符号缺失时模块仍可 import**：monkeypatch `main.ProviderStat = None` → `_read_tokens`
    走退回档，不抛
15. **额外内容块降级阶梯**：对象只有 `model_dump()` → 用它；只有 `dict` → 原样；都没有 → `str()`
16. `tool_calls_result` 非空时计入 `total_bytes`，为空时该段为 0 且渲染隐藏

另外用 AstrBot 的 venv 做 import 冒烟，证明能在真框架里加载。

## 9. 复审记录

Level 2 方案闸门第一轮送审，复审器（基础代次 v4-flash）给出两条技术反驳，均已核实并采纳：

1. **"`stop_event()` 会让回复发不出去"是错的。** 已删除该论断，改按 §3.2 的准确解释重写，
   并把 `stop_event()` 真正的毛病（丢后续 yield、装饰阶段停事件可能重复发送）记进 §7。
2. **"`Conversation.token_usage` 就是最近一次请求的总量"站不住。** 它会被 checkpoint /
   中断 / 空回复的轮次跳过写入，残留旧值；`0` 还把"未知"和"真的 0"混在一起。
   已改为以 `ProviderStat` 最新行为主、`token_usage` 仅作退回并显式标注过期风险。

（该轮 `VERDICT: FAIL` 的另一半原因是送审载荷漏了 `PROPOSER_MODELS` 元数据——属送审方失误，
已按规则连同修订一起重新送审。）

**第二轮**（补齐元数据后重送，复审器仍为基础代次 v4-flash）判 `VERDICT: FAIL`，
四条核实结论采纳三条：

3. **字节数下界，不是等式。** `total_bytes` 不含 provider 信封字段（`model` / `stream` /
   `stream_options` / `tool_choice` 等）与 `tool_calls_result` 消息。已按 §3.1 改写措辞、
   输出改用 `≈`，并把 `tool_calls_result` 补进分段（第六段）。
   序列化器本身 CONFIRMED：httpx 用 stdlib json（`_content.py:5,176-183`），
   openai 2.16.0 走同一路径（`openai/_base_client.py:586`），streaming 只增字段不改编码。
4. **`>=4.11.0` 下限站不住（唯一必须修的）。** `ProviderStat` 实际是 **v4.22.3** 才有的表、
   `model_dump_for_context` 是 **v4.24.1**，而顶层 `import` 失败是 `ImportError`，
   **包在读外围的 try/except 救不了**。已按 §3.4 改为顶层守卫 + 降级阶梯；
   并独立复扫 32 个版本树确认（`should_call_llm` / `get_db` 都是 v4.0.0）。
5. **历史图片确实带 data URL** → §4.2 的"自纠正"论证成立，CONFIRMED，未改。
6. **生命周期无隐患**（Snapshot 只存标量、变更在单个事件循环 tick 内同步完成、
   上限 64、重载后旧实例可回收）→ 不需要 `terminate()`，CONFIRMED，未改。

**第三轮**（要求复审器对 §3.2 从第一性原理**重新推导**，而不是确认上一轮它自己的归因）
判 `VERDICT: FAIL`，两条均已在本地独立复核属实并采纳：

7. **§3.2 是过度概括。** 原稿写"`stage.py:56` 那道门本来就进不去"。这只在基类 `send()` 被走到时成立；
   平台子类可以重写 `send()` 而漏掉 `_has_send_oper`——`qqofficial_message_event.py:215-217`
   就是这样。**在 QQ 官方平台上 `should_call_llm(True)` 是唯一守卫。**
   已按 §3.2 改写为"两道闸门、视平台而定"，测试 9 的桩事件也改成不置 `_has_send_oper` 以覆盖该路径。
8. **退回档必须写明取数路径。** `Conversation`（`core/db/po.py:569`）是非表 dataclass，
   真表是 `ConversationV2`；写 `select(Conversation)` 会静默失败、永远出"未知"。
   已按 §5.3 明确为走 `conversation_manager.get_conversation()`（映射见 `conversation_mgr.py:89`）。

**残余风险（未消除，已记录）**：退回档依赖 4.11.0 的 sqlite 实现真的把 `ConversationV2.token_usage`
写进去了；本机只有 4.28.x 运行环境，未在 4.11.0 上实机执行。影响仅限于退回档显示"未知"。
另：本文引用的行号均以本机 4.28.2 源码树为准。

## 10. 明确不做

- 不把本轮未落库的图片真去 base64 精确计量（自纠正，且会带来每轮 I/O）
- 不做 anthropic / gemini 的原生工具 schema 计量
- 不做 Dashboard 面板
- 不改动任何现有插件，也不改 AstrBot core
