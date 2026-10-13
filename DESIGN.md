# astrbot_plugin_ctx_meter 设计说明

一句话：**发一条 `/ctx`，立刻知道上一次真实发给模型的请求体有多大，以及被什么撑起来的。**

本文是本插件的规格书。所有关于 AstrBot 行为的断言都在 4.28.2 源码里核对过（行号以本机
`D:\AstrBotLauncher-0.1.5.5\AstrBot` 的 4.28.2 树为准），方便日后升级框架时按图索骥。
文末 §9 记录了独立复审的五轮结论——包括两个被推翻的早期论断，以及一次
"采纳了复审意见却没核实到底"的教训。

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
- `@filter.on_llm_response()`：同一轮拿到**末次 provider 调用的 token**（`LLMResponse.usage`，
  仅成功轮次；与快照可能不是同一次 HTTP 请求，见 §3.3）
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

**它是"快照那一刻组装好的请求"，两个方向都会偏。**

- **偏小**：不含 provider 信封字段——`model`、`stream`、`stream_options`、`tool_choice`、
  `temperature`、`max_tokens` 等（见 `core/provider/sources/openai_source.py:539-540,624-626`）。
  在 400 KB 的请求体上约百字节量级，所以输出用 `≈`。
- **偏大**：框架在钩子之后仍可能裁剪请求。`tool_schema_mode: skills_like` 会让 runner 把
  `req.func_tool` 换成不带参数的轻量集（`agent/runners/tool_loop_agent_runner.py:298-306`），
  而本插件记的是完整 `openai_schema()`；provider 的 `modalities` 不含 `image`/`audio` 时，
  媒体会被换成 `[Image]`/`[Audio]` 占位符（`provider/modalities.py:86-109`），而不含
  `tool_use` 时 `func_tool` 直接置空（`tool_loop_agent_runner.py:668-678`）。两处都是本插件偏大。
  （由终审复审指出，机制已在源码中确认。）

**绝不编数。** 某个分段若既没有 wire 形式的序列化方法、也无法用 `WIRE_DUMPS` 直接编码，
就**计 0**，而不是退回 `default=str` 硬凑一个值——httpx 并没有 `default=` 钩子，需要它的值
根本到不了线上，编出来的数只会污染"下界"这个契约。

**`req.tool_calls_result` 是唯一被整段排除的字段**，这是两轮终审复审逼出来的结论：

- 初版把它直接交给 `json.dumps`，触发 `default=str` 兜底。4.28.2 里它是 `ToolCallsResult`
  dataclass（`provider/entities.py:69-91`），`str()` 的 repr 远长于 JSON，**200 字节被报成 360**。
- 改用 `to_openai_messages()` 后，第二轮复审指出这**仍然不等**于线上形态：provider 在真正发起
  HTTP 之前还会重写整份消息列表——`_sanitize_assistant_messages`（定义于 `openai_source.py:446`，
  非流式 `:560`、流式 `:618` 调用；`:483-486` 重写 content、`:508-522` **丢弃孤儿 tool 消息**）
  与 `_finally_convert_payload`（定义于 `:995`、`:991` 调用、`:1016-1049` 改写循环）。
  （这里只列事实，不声称两者的先后顺序——第三轮终审指出我先前把顺序写反了。）
- 而这个字段在框架自身的流程里**于快照时刻恒为空**——runner 是在本轮首个请求之后
  （`tool_loop_agent_runner.py:1089`）才把结果挂上去的，钩子早在 `internal.py:352` 就触发了。

结论：为一段正常恒为 0 的内容去复刻一个有损清洗器不划算，**整段不量**。
`measure.py` 的 `_SECTION_LABELS` 顶部写明了这个理由，导入冒烟脚本则用**真实的**
`ToolCallsResult` 断言它确实没有泄进总数。

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

### 3.3 token 数据源：`LLMResponse.usage`，以及它与字节快照**并不同轮**

`@filter.on_llm_response()`（`star_handler.py:456`，v4.0.0 起就有）以
`async def handler(self, event, response: LLMResponse)` 回调，`response.usage` 是
`TokenUsage`，含 `input_other` / `input_cached` / `output` 与 `.total`
（`entities.py:322-351,386`）。这正是 core 自己落库的那个值（写于
`agent_sub_stages/internal.py:639-648`），但**在进程内直接拿，不需要碰数据库**。

因此本插件**不读** `Conversation.token_usage`，也不用 `ProviderStat`：少了
sqlalchemy / sqlmodel / ORM 依赖与版本守卫，也少了"该列在有 checkpoint / 被中断 /
空回复的轮次会被跳过写入、于是读到更早轮次旧值"的问题。

**但它有三个必须写进设计与输出的边界（独立复审指出，均已核实）：**

1. **失败轮次也会回调，但 `usage` 是 `None`。** `on_llm_response` 在 err/abort 路径上
   **照样触发**（`tool_loop_agent_runner.py:1536`、`astr_agent_run_util.py:343`），
   只是那些回调带的 `usage` 为 `None`。
   → 对策：`on_llm_request` 写新快照时**把 token 一起清空**，失败轮次显示"未知"，
   而不是把上一轮成功时的 token 拿来配这一轮的字节。
2. **全零 ≠ None。** provider 不报 usage 时 SDK 给的是 `TokenUsage()`（全零），
   而不是 `None`（`provider/sources/openai_source.py`: `... if completion.usage else TokenUsage()`）。
   → 对策：`total == 0` 一并当作"未知"，不显示 `0`。
3. **它描述的是本轮*最后一次* provider 调用，字节快照描述的是本轮*开头的*那次请求。**
   带工具调用的轮次里，二者是**不同的两次 HTTP 请求**（字节少算后续请求，token 是末次的值）。
   → 对策：输出标签写成"**本轮最后一次调用 token**"，并在 README 里说明该差异；
   不声称两者"天然一致"。

另：钩子对**插件发起的** `event.request_llm` 同样会触发（`process_stage/stage.py:38-48`）。
**这是有意保留的行为**——`/roll` 这类重新生成本身就是对该会话的一次真实模型请求，
刷新计数比不刷新更符合直觉。

### 3.4 版本下限与降级策略（`>=4.10.0`）

逐版本扫过 `_astrbot_hist/v*/`（**31 棵**树）得到每个 API 的首次出现版本：

| API | 首次出现 | 作用 | 缺失时 |
|---|---|---|---|
| `filter.on_llm_request` | v4.0.0 | 字节测量点 | 必须 |
| `filter.on_llm_response` | v4.0.0 | token 取数点 | 必须 |
| `LLMResponse.usage` / `TokenUsage` | **v4.10.0** | token 值 | 必须（决定下限） |
| `event.should_call_llm` | v4.0.0 | 主守卫 | 必须 |
| `filter.command(alias=...)` | v4.0.0 | 指令别名 | 必须 |
| `ToolSet.openai_schema` | v4.0.0 | 工具段字节 | 必须 |
| `ProviderRequest.extra_user_content_parts` | **v4.11.0** | 额外内容块 | `getattr` → 该段计 0 |
| `Part.model_dump_for_context` | **v4.24.1** | 额外内容块序列化 | 阶梯降级 |

**下限取最晚的那个必需项 = v4.10.0。** 两个降级项（额外内容块本身、以及它的序列化方法）
都只走 `getattr` 阶梯，**不需要顶层 import 守卫**：

`model_dump_for_context()`（4.24.1+）→ `model_dump()` → 纯 dict 原样 → **计 0**

最后一级是"计 0"而不是 `str()`：见 §3.1 的"绝不编数"。降级只影响**精度**，不影响可用性。

## 4. 架构

`main.py` 只做接线，行为全部放在不依赖 astrbot 的 `ctxmeter/` 包里——这样整套测试
能在没装 AstrBot 的环境里跑。

```
__init__.py            # 包标记（AstrBot 以包形式加载 main.py）
main.py                # Star 子类 + 2 个 hook + 1 个指令（每个都只是转发）
ctxmeter/measure.py    # 纯函数 measure(req) -> Snapshot
ctxmeter/render.py     # 纯 Tokens / render() / format_bytes()
ctxmeter/service.py    # CtxMeterService：缓存、开关与权限判定、渲染入口
tests/                 # 离线 pytest + 导入冒烟（按仓库惯例不纳入版本管理）
```

之所以把 hook 的逻辑也放进 `ctxmeter/service.py` 而不是留在 `main.py`：`main.py` 一旦
`import astrbot`，离线套件就测不了它。放一个无框架依赖的 `CtxMeterService` 进去，
两个 hook 和指令各剩一行转发，于是**开关、权限、缓存淘汰、清空 token 这些真正会出错的逻辑
全部可测**——这也是本仓库既有的 `roll/` + `main.py` 分工。

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

def wire_bytes(obj) -> int: ...          # 编码失败返回 0；无 default=str 兜底（§3.1）

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
    taken_at: float          # time.time()

    @property
    def total_bytes(self) -> int: ...    # 五段之和；不等于真实 body（见 §3.1）
    @property
    def sections(self) -> tuple[tuple[str, int], ...]: ...   # 渲染用，固定五段

def measure(req, now=None) -> Snapshot: ...
```

取值**一律** `getattr(x, name, default)`，保证旧版本和测试桩都不炸。任何一段抛异常
只降级为 0，绝不冒泡。

各分段的序列化规则：

| 分段 | 取值 |
|---|---|
| 系统提示词 / 本轮输入 | 本身即字符串，空则不计数 |
| 历史上下文 | `req.contexts`（已是 OpenAI 格式） |
| 额外内容块 | 见 §3.4 的阶梯，末级计 0 |
| 工具 Schema | `openai_schema()`；不可用则退化为工具名列表（粗但诚实） |
| 工具调用结果 | **不量**，理由见 §3.1 |

`wire_bytes()` 在 `WIRE_DUMPS` 编码失败时返回 **0**，没有 `default=str` 兜底（§3.1）。
`Snapshot.sections` 因而固定为**五段**。

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
    def from_usage(cls, usage, now=None) -> "Tokens | None":
        """usage 为 None、缺 total、或 total == 0 时返回 None（未知）。"""

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
🔢 本轮最后一次调用 token：118,432（缓存输入 98,000 · 其它输入 18,000 · 输出 2,432）
🖼 本轮附带图片 2 张（尚未编码，未计入）
🤖 模型 gpt-4o-mini · 快照 14:23:07
```

- `detail=False` 只留首行 + token 行
- `snapshot is None` → "还没有记录到本会话的请求，先随便聊一句再来看看。"
- `tokens is None` → `🔢 本轮最后一次调用 token：未知`（含失败轮次、provider 不报 usage）
- token 行**不写"上一次请求"**：它描述的是本轮末次 provider 调用，与字节快照描述的首个请求
  可能不是同一次（见 §3.3）
- 零值分段不在明细里出现（如没有工具就不显示工具行）
- 字节格式化：`<1 KiB` → B，`<1 MiB` → KB，否则 MB，一位小数

### 5.3 `main.py` + `service.py`

`main.py` 只做接线，**没有任何判定逻辑留在这一层**——因为它 `import astrbot`，离线套件
够不着它：

```python
@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class CtxMeterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self._service = CtxMeterService(config=config or {}, logger=logger)

    @filter.on_llm_request()                      # 一行转发（外套 try/except 兜底）
    async def _record_request(self, event, req) -> None: ...

    @filter.on_llm_response()                     # 一行转发（外套 try/except 兜底）
    async def _record_response(self, event, response) -> None: ...

    @filter.command("ctx", alias={"请求体", "ctxsize"})
    async def ctx(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        yield event.plain_result(self._service.view(event))
```

判定逻辑全在 `CtxMeterService`（无框架依赖，全部经鸭子类型访问）：

```python
class CtxMeterService:
    def __init__(self, config=None, logger=None) -> None: ...
    def record_request(self, umo, req, now=None) -> None      # hook 1
    def record_response(self, umo, usage, now=None) -> None   # hook 2
    def view(self, event, now=None) -> str                    # 指令要回的文本
    def snapshot_for(self, umo) -> Snapshot | None            # 只读；兼作测试探针
    def tokens_for(self, umo) -> Tokens | None                # 只读；兼作测试探针
```

行为约定：

- `record_request` **整体替换**该会话的 Entry 为 `Entry(snapshot=新, tokens=None)` ——
  **token 必须一起清空**。失败轮次照样会回调 `on_llm_response` 但不带 usage，
  留着旧值就会把上一轮成功的 token 配到这一轮的字节上（§3.3 边界 1）
- `record_request` 测量失败时**不动**已有 Entry：瞬时故障不该抹掉一个好数字，
  何况页脚本来就打印快照时间，过期是看得见的
- `record_response` 只补 tokens，不动 snapshot；usage 未知（`None` 或全零）时保持"未知"
- `view` 依次判定：开关 → 权限 → 取 Entry → `render`；**只读**，不产生任何写入
- 缓存是 `OrderedDict[str, Entry]`，两个 `record_*` 都 `move_to_end`，超上限时
  `popitem(last=False)` 淘汰最久未使用的会话；`Entry` 只存标量，不持有请求或历史对象
- `max_sessions` 容忍配置界面传回字符串
- 指令侧：`should_call_llm(True)` → `view(...)`（外面再包一层 try/except，
  保证**无论如何都会 `yield` 一条 `plain_result`**，绝不静默无回复）
- 全程**不读数据库**、不调用 `update_conversation` / `insert_provider_stat`、不写会话状态

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
| provider 不报 usage（`TokenUsage()` 全零） | 同样显示"未知"，**不显示 `0`** |
| 本轮失败 / 被中断 | 回调**会**发生但 `usage` 为 `None`，token 显示"未知"（新 Entry 里 token 已被清空） |
| 带工具调用的轮次 | 字节 = 本轮首个请求，token = 本轮末次调用；两者是不同请求，README 已说明 |
| 插件发起的 `event.request_llm`（如 `/roll`） | **有意**同样刷新该会话的 Entry |
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
3. `extra_user_content_parts` 走 `model_dump_for_context()`；只有 `model_dump()` 的对象退到它；
   纯 dict 原样透传；**两者都没有的对象计 0**
4. **降级阶梯**：见第 3 条；另测 `wire_bytes()` 对无法编码的对象返回 0（绝不编数）
5. `func_tool=None` / `openai_schema` 抛异常 → 不抛（后者退化为工具名字节数）
6. **裸 `object()` 当 req** → 全 0，不抛
7. 图片/音频只计数、不进字节
8. **工具调用结果被整段排除**：用镜像 4.28.2 `ToolCallsResult` 的 stub 断言它**不**影响
   `total_bytes`；导入冒烟脚本再用**真实的** `ToolCallsResult` 复验一次
   （`without == with`）。**这条用例最初用 dict 写、后来又改成断言 `to_openai_messages()`，
   两次都没抓住真相**，最终形态见 §3.1
9. `render`：detail 开关、B/KB/MB 边界、零值分段隐藏
10. `Tokens.from_usage`：`usage=None` → `None`；`total == 0`（provider 不报 usage 的全零对象）
    → **也返回 `None`**；正常对象 → 三段 + total 正确
11. `render(snapshot, None, ...)` → "未知" 且不抛；**断言输出里不含 `0`**
12. 接线：`/ctx` 恰好一次 `should_call_llm(True)` + 恰好一条 `plain_result`
13. LRU 淘汰
14. 两个 hook 各自抗炸（monkeypatch `measure` 抛异常 → 不冒泡）
15. `on_llm_response` 只补 tokens、不动 snapshot
16. **失败轮次路径**：先喂一轮 `on_llm_request` + `on_llm_response`（有 token），
    再只喂一轮 `on_llm_request`（模拟 err/abort，无 response）→ 断言 token 变回"未知"，
    **不是上一轮的旧值**（§3.3 边界 1）
17. **工具续跑路径**：一轮里 `on_llm_request` 出现两次而 `on_llm_response` 只在末尾一次 →
    断言最终 Entry 的 snapshot 来自**最后一次** request、tokens 来自 response（§3.3 边界 3）
18. `admin` 模式下非管理员被拒；`enable=False` 时**两个 hook 都不记录任何东西**
    （不只是不回复——这条由终审复审指出配置提示与实现不符后补上）

用例分三个文件：`tests/test_measure.py`（字节口径与降级）、`tests/test_render.py`（格式化与
token 三态）、`tests/test_service.py`（缓存、清空 token、两条边界、开关与权限）。

**导入冒烟**单独成脚本 `tests/smoke_import.py`，用 AstrBot 的 venv 跑（那环境里没有 pytest）：
它加载真实框架、断言两个 hook 与 `/ctx` 都注册成功、并确认 `/ctx` 挂的是 `CommandFilter`
（即真的能从聊天触发）。这一层是离线套件够不着的，所以必须实机验。

## 9. 独立复审记录（五轮）

方案闸门送审五轮，每轮都被判 `FAIL`，但结论逐轮收敛——第五轮已经把全部承重机制
独立确认为正确，剩下的只是边界标签。以下按"被推翻的是什么"记录。

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
   并把 token 与字节快照挂到同一个缓存 Entry 上（**注意**：是同一个"轮次"，
   不是同一次 HTTP 请求——该区别在第五轮被指出，见下）。§3.3 / §5.3 已据此重写。
   （同时修正了 `select(Conversation)` 的说法：它**会抛异常**，不是静默失败——
   `Conversation` 是非表 dataclass，真表是 `ConversationV2`。）

**第五轮**：这一轮把**所有承重机制都独立确认了**——§3.2 的"主守卫与发送路径无关"成立
（逐个平台 grep 过 `send()`，没有任何平台绕过基类）；`on_llm_request` 在常规路径上每轮一次
（`internal.py:352`，无逐段派发；注意 `stage.py:44,65` 在插件自行发起 LLM 时会让子阶段跑两次，
测试 17 覆盖该情形）；下限 `>=4.10.0` 正确且无别的 API 会抬高它。
剩下的是三条边界，均已采纳：

5. **失败轮次的 `usage` 是 `None`**（回调**会**发生——`tool_loop_agent_runner.py:1536`、
   `astr_agent_run_util.py:343`——只是没有 usage）。于是失败轮次会出现"新字节 + 旧 token"
   的误导性组合。→ `on_llm_request` 写新 Entry 时**连 token 一起清空**；
   §3.3、§7、测试 16 已改。（第六轮更正了本条原先"失败轮次不回调"的措辞。）
6. **字节与 token 在带工具的轮次里描述不同的两次请求**（快照取自本轮首个请求，
   token 取自末次 provider 调用）。原稿"天然一致"的说法不成立。
   → token 行改标签为"本轮最后一次调用 token"，§3.3 写明差异，新增测试 17。
7. **全零 ≠ None**：provider 不报 usage 时是 `TokenUsage()` 而非 `None`
   （`provider/sources/openai_source.py` 的三元表达式，已核实）。
   → `total == 0` 一并当"未知"；测试 10/11 加了断言。
   另补齐 §3.4 表（`extra_user_content_parts` 实为 **v4.11.0**）并更正版本树数量
   （**31 棵**，不是 32）。

**第六轮：`VERDICT: PASS`。** 闸门通过，可以实施。它验证掉了送审方自己提出的
时序担忧，并确认了前几轮的机制：

- **轮次交错不可达**：会话锁（`internal.py:220`）把 `internal.py:352` 与流式消费都包在内
  （`respond/stage.py:230` 内联 await；`aiocqhttp_message_event.py:207/221`、
  `tg_event.py:611-618`、`lark_event.py:1255-1286`），`on_agent_done` 必在锁释放之前，
  同 umo 事件串行。因此**不需要轮次 ID 关联**。
- **"有 response 无 request" 无反例**：所有 `on_agent_done` 触发点
  （`tool_loop_agent_runner.py:201,1536`；`astr_agent_run_util.py:343`；`dify:287`、
  `coze:324`、`dashscope:359`、`deerflow:129`）都在已派发过 `OnLLMRequestEvent` 的轮次内。
- `total == 0 → 未知` 安全（真·全零与"未上报"在语义上本就不可区分）。
- 工具轮次的不对称描述正确（钩子只在本轮首次请求处触发 `:352`；runner 累加于 `:867`
  但只传末次响应）。

要求的两处**非阻塞**文档更正已应用：失败轮次是"回调了但 `usage=None`"（不是不回调）；
`on_llm_request` 在插件自行发起 LLM 时可跑两次（`stage.py:44,65`）。

**终审闸门（实现完成后的独立质量复审）第一轮：`VERDICT: FAIL`**，抓到一条真会算错数的
**MAJOR** 与若干边界问题，全部已修：

7. **`req.tool_calls_result` 的度量口径是错的（MAJOR）。** 4.28.2 里它是 `ToolCallsResult`
   dataclass（`provider/entities.py:69-91`），真实线格式是 `to_openai_messages()`；初版直接把它
   交给 `json.dumps`，触发 `default=str` 兜底，**把 200 字节报成 360 字节（+80%）**。
   更糟的是锁这条行为的测试用的是 dict——**框架永远不会产出那个形状**，所以测试永远不会失败。
   → 已改为 `to_openai_messages()` → `model_dump()` → 计 0；测试改用镜像真实 dataclass 的
   stub；并让导入冒烟脚本对着**真实类**断言（实测 `measured=200 wire=200 lossy-would-be=360`）。
8. **"精确下界"是过度声称（MINOR）。** `tool_schema_mode: skills_like` 会在钩子之后把工具集
   换成轻量版、provider 的 `modalities` 缺 `image`/`audio` 时媒体被换成占位符——这些都是本插件**偏大**。
   → §3.1 改为"两个方向都会偏"，README 与 metadata 同步。
9. **配置提示与实现不符（MINOR）。** `enable` 的 hint 说"两个钩子会直接返回"，但代码只在
   `view` 里判。→ 改为两个 hook 也判，并补测试。
10. **设计文档对 `toolcalls_bytes` 的描述不实（MINOR）。** 框架自身的流程里钩子在本轮首个请求上
    触发，工具结果是**之后**才挂上去的，所以该段通常为 0，只在第三方插件自带
    `ProviderRequest` 时才有值。→ §5.1 与 README 已按实情改写。
11. **冒烟脚本把凭据文件搬去了 `%TEMP%`（MINOR，与部署事故同一类）。** 临时根目录从不清理，
    每次都留下一份 `data/cmd_config.json`（含 `jwt_secret`/`api_key`）。
    → `finally` 里先 `chdir` 回原目录再 `shutil.rmtree`（Windows 不允许删当前工作目录），
    并清理了历史残留。
12. **文档少列一段（NIT）**：当时的渲染器支持六段，README/metadata 只写了五段。
    （后续第二轮复审把"工具调用结果"整段删掉了，最终就是**五段**；见第 14 条。）
13. **`default=str` 兜底本身不是 wire 行为（NIT）**：httpx 没有 `default=` 钩子，需要它的值
    根本到不了线上。→ 已整体删除该兜底（见 §3.1 "绝不编数"）。

**终审闸门第二轮：`VERDICT: FAIL`**，而且**否掉的正是第一轮我刚采纳的那个修法**：

14. **`to_openai_messages()` 不是该字段的线上形态。** 它确实进了 `messages`
    （`openai_source.py:979/982` → `:989`），但之后还会被 `_sanitize_assistant_messages`
    改写（非流式 `:560`、流式 `:618`；`:471-486` 重写 content、`:490-523` **丢弃孤儿 tool
    消息**、`:523` 整体替换 `payloads["messages"]`），`_finally_convert_payload`
    （`:991,1016-1049`）还会再改一次。所以即使改用 `to_openai_messages()`，在那种罕见情形下
    报出的仍是**偏大**的数。
    → 不复刻有损清洗器，**整段排除**（§3.1）。这同时让"五段"成为事实，
    README / metadata / §5.1 / §8 全部同步。
    → 教训（第二次同类）：采纳复审意见后仍要从**完整的调用链末端**验证，而不是验证到
    第一个看起来对的函数为止。

15. **第三轮 A 判定 PASS**：确认 §3.1 引用的行号与结论都对（`_sanitize_assistant_messages`
    定义于 `:446`、应用于 `:560`/`:618`，`:483-486` 改 content、`:508-522` 丢孤儿；
    `_finally_convert_payload` 定义于 `:995`、`:991` 调用、`:1016-1049` 循环），
    也确认该字段在钩子时刻确为空、整段排除是可辩护的。两条 P3 措辞已修：
    补上 `def` 行号，并去掉了我写反的执行顺序。
16. **第三轮 B 判定 FAIL，抓到真 bug。** `wire_bytes` 把 `text.encode("utf-8")` 写在了 `try`
    **之外**：`ensure_ascii=False` 会保留孤立代理字符，`.encode` 随即抛 `UnicodeEncodeError`
    （`ValueError` 的子类，本可被捕获）。平台适配器解码出的文本可能带这种字符，
    于是 `measure` 违约抛异常，且经 `record_request` 时**整份快照被静默丢弃**。
    → 已把 encode 挪进 try，并补两条用例（孤立代理字符、`__str__` 抛异常的敌意对象）。
    实测：`wire_bytes('a\ud800b') == 0`，快照保留且总数为 5，而修复前是 `snapshot: None`。

17. **第四轮 A 判定 FAIL：`measure` 仍有逃逸路径**，四条全部由复审器的探针复现——
    `_call_dump` 里的 `getattr` 在 `try` 之外（属性本身抛异常即逃逸）、`measure` 内多处裸
    `getattr(req, ...)`、`getattr(func_tool, "openai_schema")` 未保护、`except` 抓不住
    `BaseException`（如 `CancelledError`）。
    → 引入 `_attr()` 作为全局兜底；`_call_dump` 的 `getattr` 入 `try`；`wire_bytes` 放宽为
    `except Exception`。**有意不吞 `BaseException`**——吃掉任务取消比丢一个分段更糟。
    → 并把 `measure` / `view` 的 docstring 从"never raises"改成**准确表述**：分段各自降级、
    普通敌意输入不抛，但失败由调用方兜住且**保留旧快照**，而不是记一个假的 0。
    五条探针复跑全部不再抛（含那条 `items()` 抛异常的 dict 子类，返回 2 字节空对象）。
18. **第四轮 B 判定 PASS**，四条 P3 措辞已修：`measure.py` 里的 "honest lower bound"、
    冒烟脚本仍说自己在验证尺寸（实际是验证排除）、`conftest` 的 `OpaquePart` 注释、
    README 没说明 `tests/` 不入库。

**残余风险（未消除，已记录）**：

- 本机只有 4.28.x 运行环境，`>=4.10.0` 下限与 4.10–4.27 的行为无法实机冒烟。
- `usage` 的单次调用 vs 整轮累计语义未在真实 provider 上比对；影响限于 token 行。
- `skills_like` 与 `modalities` 两处偏大只在源码层面确认，未端到端复现。
- 终审复审指出 `_astrbot_hist/v*/` 似乎已不在磁盘上；**本 agent 复核后确认它仍在**
  （31 棵树），§3.4 的版本表可据此复查。

## 10. 明确不做

- 不读数据库（`ProviderStat` / `Conversation.token_usage` 都不用）
- 不把本轮未落库的图片真去 base64 精确计量（自纠正，且会带来每轮 I/O）
- 不做 anthropic / gemini 的原生工具 schema 计量
- 不做 Dashboard 面板
- 不改动任何现有插件，也不改 AstrBot core
