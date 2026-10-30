# model-gateway

DeepSeek V4 Flash 和 Anthropic Messages API 的供应商网关。一次调用会完成这些事：校验参数、渲染带版本的提示词、按供应商限流、失败时指数退避、把上游响应收成统一结果，并把 token、花费、延迟和错误记进 SQLite。

Python 3.10 及以上。依赖 `pydantic` 和 `httpx`。调用是同步的：`complete()` 返回整段结果，`stream_sse()` 按 SSE 逐块返回。

网页入口见下方「开启网页」。

## 开启网页

在项目根目录执行：

```bash
python3 -m model_gateway.web
```

终端打印 `模型网关控制台: http://127.0.0.1:8765` 后，用浏览器打开这个地址。停止时在该终端按 `Ctrl+C`。

端口已被占用时，先停掉上一个进程，或换一个端口：

```bash
python3 -m model_gateway.web --port 8766
```

`--host` 默认是 `127.0.0.1`，只监听本机。

不配置密钥也能打开页面：可以发布提示词、预览渲染、看账本。点「发送并查看过程」时，供应商那一步会失败，错误码是 `AUTHENTICATION`，这次失败会记进账本。页面只显示密钥是否已配置，不显示密钥本身。

要打到真实模型，点右上角的「未配置密钥」或「配置」。填入后立刻用于当前进程，并写入当前目录的 `.env`（权限仅本人可读，已被 `.gitignore` 排除）。留空表示不修改，勾选清除才会删掉。保存后输入框会被清空，接口也不再返回密钥。密钥不进 SQLite，也不进页面和日志。

也可以事先准备 `.env`，再启动。网页进程启动时会读取这个文件；已经在环境里的同名变量优先，不会被文件盖掉。不要把密钥写在命令行上。在自己的代码里调用 `ModelGateway.from_env()` 时，库不会自动读 `.env`，仍要先 `source .env`。

```bash
cp .env.example .env
# 编辑 .env，填上 DEEPSEEK_API_KEY 和 ANTHROPIC_API_KEY
set -a
source .env
set +a
python3 -m model_gateway.web
```

变量说明见下方「配置」。

页面分三栏：

- 左边发布提示词版本。槽位分成可替换变量和外部资料。
- 中间是一次调用，按顺序标出：校验参数、渲染提示词、调用供应商、返回、写入账本。方式选「流式」时边生成边显示；选「完成后一次返回」时页面只做等待计时，正文到齐后一次出现。预览渲染只替换变量，外部资料里的 `{name}` 保持原文，并且不会请求模型。
- 右边是账本：调用次数、错误率、花费、延迟、token。

打开页面时已经填好示例 `trip.plan`：`city` 和 `nights` 是可替换变量，`policy` 是外部资料。

## 目录

```
model_gateway/
  models.py              请求、响应、提示词、重试和限流的 Pydantic 模型
  errors.py              统一错误码
  gateway.py             门面：把下面的模块串成一次调用
  prompts.py             提示词版本、变量替换、外部资料
  usage.py               调用账本：token、花费、延迟、错误率
  pricing.py             按公开价目把 token 换成美元
  resilience.py          指数退避和令牌桶限流
  sse.py                 SSE 编解码
  structured.py          response_format 与 JSON 校验
  db.py                  SQLite 表结构
  providers/
    deepseek.py          POST /chat/completions
    anthropic.py         POST /v1/messages
  web/                   网页入口，python3 -m model_gateway.web
tests/                   单元测试；test_live.py 才会打真实接口
```

调用关系：

```
ChatRequest
    │  Pydantic 校验
    ▼
PromptStore.render          可替换变量填进正文，外部资料附在后面
    │
    ▼
RateLimiter                 请求数 / 输入 token
    │
    ▼
DeepSeekProvider 或 AnthropicProvider
    │  失败且错误码可重试 → 指数退避后再打
    ▼
structured.parse_model_output
    │
    ▼
UsageStore.record           每一次尝试一行
    │
    ▼
ChatResponse 或 SSE 帧
```

## 安装

在项目根目录：

```bash
python3 -m pip install -e ".[dev]"
```

只跑测试、不安装包也可以。`pyproject.toml` 已经把项目根加进 `pytest` 的 `pythonpath`。

```bash
python3 -m pytest tests -q
```

## 配置

密钥只放在进程环境里，不写进源码、不写进 SQLite、不出现在日志和对象的 `repr` 里。业务代码用 `ModelGateway.from_env()`，它只读环境变量。

本地可以这样准备，密钥留在 `.env`，这个文件已被 `.gitignore` 排除：

```bash
cp .env.example .env
# 编辑 .env，填上密钥。不要把 .env 提交进仓库。
set -a
source .env
set +a
```

```python
from model_gateway import ModelGateway

with ModelGateway.from_env() as gateway:
    ...
```

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek 密钥 | 空 |
| `ANTHROPIC_API_KEY` | Anthropic 密钥 | 空 |
| `DEEPSEEK_BASE_URL` | DeepSeek 根地址 | `https://api.deepseek.com` |
| `ANTHROPIC_BASE_URL` | Anthropic 根地址 | `https://api.anthropic.com` |
| `GATEWAY_DB` | SQLite 文件 | `gateway.db` |

重试和限流不是密钥，写在代码里：

```python
from model_gateway import ModelGateway, RateLimitPolicy, RetryPolicy

gateway = ModelGateway.from_env(
    retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.5, multiplier=2),
    rate_limits={
        "deepseek": RateLimitPolicy(requests_per_minute=60, request_burst=1),
    },
)
```

构造函数上的 `deepseek_api_key` / `anthropic_api_key` 只给测试，或给已经从密钥管理系统取出密钥的进程内注入。仓库里的示例和业务代码不要填真实密钥。单元测试里的 `sk-test` 只打到 `httpx.MockTransport`，不会发到供应商。

没配密钥时对象仍能创建。真正发请求时抛出 `GatewayError`，错误码是 `AUTHENTICATION`，这次失败会记入调用账本，账本里没有密钥。

## 供应商和模型

| provider | 默认模型 | 接口 |
| --- | --- | --- |
| `deepseek` | `deepseek-v4-flash` | `POST {base}/chat/completions` |
| `anthropic` | `claude-sonnet-5-5` | `POST {base}/v1/messages` |

`deepseek-v4-flash`、`deepseek-flash`、`deepseek-v4-flash-vision-exp` 都按 DeepSeek Flash 计价。官方当前推荐的名字是 `deepseek-flash`，`deepseek-v4-flash` 仍被接受并走同一档价格。请求里不写 `model` 时用上表的默认值。

已内置价格的 Anthropic 模型：`claude-sonnet-5-5`、`claude-sonnet-5`、`claude-sonnet-4-6`、`claude-sonnet-4-5`、`claude-haiku-4-5`、`claude-haiku-5-5`、`claude-opus-5-5`、`claude-opus-5`。

价格表里没有的模型，在发请求之前就会被拒绝，避免把一次真实调用的花费记成 0。供应商如果回了一个不在表里的版本号，花费按你请求的那个模型计算，响应里的 `model` 仍保留供应商原文。

`temperature` 范围是 0 到 2。`max_tokens` 范围是 1 到 384000。Anthropic 没传 `max_tokens` 时，适配层补 1024。DeepSeek 没传则不带这个字段。

## 一次普通调用

`messages` 和 `prompt` 至少要有一个。字典和 `ChatRequest` 都可以传给 `complete()`。

```python
from model_gateway import ChatMessage, ChatRequest, ModelGateway

with ModelGateway.from_env() as gateway:
    response = gateway.complete(ChatRequest(
        provider="deepseek",
        messages=[ChatMessage(role="user", content="用一句话介绍巴黎")],
        temperature=0.2,
        max_tokens=128,
    ))

print(response.content)
print(response.usage.total_tokens, response.cost.total_cost, response.latency_ms)
print(response.record_id, response.logical_request_id, response.attempts)
```

`ChatResponse` 上和追溯有关的字段：

| 字段 | 含义 |
| --- | --- |
| `content` | 模型正文 |
| `parsed` | 结构化输出解析后的对象；普通文本是 `None` |
| `usage` | 输入、输出、缓存命中、缓存写入、未命中输入、总 token |
| `cost` | 美元，含 `price_tier`（`off_peak` / `peak` / `standard` 等） |
| `latency_ms` | 从进入 `complete()` 到返回，含限流等待和退避 |
| `attempt_latency_ms` | 最后一次成功尝试本身的耗时 |
| `attempts` | 第几次尝试成功 |
| `logical_request_id` | 同一次用户调用的各次重试共用 |
| `record_id` | 成功那一行账的主键 |
| `prompt_key` / `prompt_version` | 用了版本化提示词时才有 |

## 提示词版本

提示词存在 SQLite 的 `prompt_versions` 表。同一个 `key` 每发布一次，`version` 加 1。

槽位分两种，发布时就必须分开：

| `SlotKind` | 含义 | 会不会出现在正文占位符里 |
| --- | --- | --- |
| `variable` | 可替换变量 | 会。正文里的 `{city}` 必须正好对应这些槽位 |
| `external` | 外部资料 | 不会。资料在渲染后附在正文后面，不参与替换 |

占位符只认 `{变量名}`，名字是字母或下划线开头。值按原文插入：字符串原样，布尔变成 `true` / `false`，数字变成十进制字符串，`dict` / `list` 变成 JSON。值里面再出现 `{other}` 也不会被继续替换。

正文里要写字面量大括号时，把它们加倍：`{{` 变成 `{`，`}}` 变成 `}`。适合在模板里放 JSON 示例。

```python
from model_gateway import MaterialInput, MaterialSpec, PromptSlot, PromptSpec, SlotKind

gateway.prompts.publish(PromptSpec(
    key="trip.plan",
    role="system",          # system 或 user，决定渲染结果插到哪条消息
    body='Plan {city} for {nights} nights. Example: {{"city": "{city}"}}',
    slots=[
        PromptSlot(name="city", kind=SlotKind.VARIABLE, description="目的地"),
        PromptSlot(name="nights", kind=SlotKind.VARIABLE, required=False, default=2),
        PromptSlot(name="policy", kind=SlotKind.EXTERNAL, description="退改原文"),
    ],
    changelog="first",
    status="active",        # active 或 draft
))
```

版本规则：

- 新发布一条 `active` 时，同一 `key` 上原来的 `active` 会变成 `archived`。
- `draft` 不会挤掉当前生效版。
- `prompts.get(key)` 返回当前 `active`。`prompts.get(key, version)` 按版本取，包括已归档的，方便复现某次调用。
- `prompts.set_status(key, version, "active" | "draft" | "archived")` 改状态。设成 `active` 时，其它生效版会被归档。
- `prompts.list_versions(key)` 按版本号升序返回。

外部资料存在 `external_materials` 表，同样按 `material_id` 递增版本。渲染时可以引用某一版，也可以直接给一段文本。

```python
gateway.prompts.put_material(MaterialSpec(
    material_id="refund",
    title="退改",
    content="原文 {city} 不会被当成变量",
    source="handbook",
))

rendered = gateway.render_prompt(
    "trip.plan",
    version=1,                 # 省略则用当前 active
    variables={"city": "Paris"},
    materials=[MaterialInput(slot="policy", material_id="refund", version=1)],
)
```

`rendered.text` 只含替换后的模板。`rendered.materials` 是外部资料列表。`rendered.composed_text()` 把两者拼在一起，资料放在「【外部资料】」标题下。调用模型时用的是拼好的这段，所以模型看得到原文，但原文不会被 `{city}` 这种规则改写。

内联资料不用先入库：

```python
MaterialInput(slot="policy", content="临时附件", title="备注")
```

`material_id` 和 `content` 只能填一个。

把提示词接到调用上：

```python
response = gateway.complete({
    "provider": "deepseek",
    "prompt": {
        "key": "trip.plan",
        "variables": {"city": "Paris"},
        "materials": [{"slot": "policy", "material_id": "refund"}],
    },
    "messages": [{"role": "user", "content": "开始规划"}],
})
```

渲染结果插在 `messages` 前面。账本里会记下 `prompt_key`、`prompt_version`。外部资料如果来自库，记录 id 和版本；如果是内联文本，记录 SHA-256 和字符数，不把原文再抄进账本。

`variables` 也可以是带实例属性的对象（例如 `types.SimpleNamespace`），`PromptStore.render()` 会读它的公开属性。走 `ChatRequest` 时请传字典，这样请求本身能被 Pydantic 校验。

这些写法会在发布或渲染时失败，错误类型是 `PromptError`（错误码 `INVALID_REQUEST`）：

- 正文占位符和 `variable` 槽位对不上
- 把外部资料槽位写进了 `{policy}`
- 调用时少了必填变量，或把外部资料塞进 `variables`
- 传入了没声明的变量名或资料槽位

## 结构化输出和流式

统一入口是 `response_format`，不要自己拼两家不同的字段。

| `type` | 行为 |
| --- | --- |
| `text` | 普通文本，`parsed` 为 `None` |
| `json_object` | 返回值必须是合法 JSON |
| `json_schema` | 在合法 JSON 之上，再按给出的 schema 检查类型、`required`、`enum`、`additionalProperties` 和数组 `items` |

```python
from model_gateway import ResponseFormat

fmt = ResponseFormat(
    type="json_schema",
    name="trip",
    json_schema={
        "type": "object",
        "properties": {"city": {"type": "string"}, "nights": {"type": "integer"}},
        "required": ["city", "nights"],
        "additionalProperties": False,
    },
)
response = gateway.complete(ChatRequest(
    provider="anthropic",
    messages=[ChatMessage(role="user", content="Paris for 3 nights")],
    response_format=fmt,
))
assert response.parsed["city"] == "Paris"
```

两家适配方式不同：

- DeepSeek 只保证 `response_format: {"type": "json_object"}`。`json_schema` 会写进一条系统提示，返回后再在本地校验。提示里如果还没有 `json` 这个词，适配层会补一句。
- Anthropic 使用 `output_config.format`，类型为 `json_schema`。`json_object` 会变成允许额外字段的 object schema。系统消息会从 `messages` 里抽到顶层 `system`，连续的同角色消息会合并。对话必须以 `user` 开头。

JSON 解析失败或不符合 schema 时抛 `GatewayError`，错误码 `PARSE_ERROR`。这次调用已经产生的 token 和花费仍会入账。

流式用 `stream_sse()`。每个块是一条 SSE：

```
event: message_start
data: {"id":"...","provider":"deepseek","model":"deepseek-v4-flash","logical_request_id":"...","attempt":1}

event: content_delta
data: {"text":"{\"city\":"}

event: usage
data: {"input_tokens":1000,"output_tokens":500,"cost_usd":"0.00045","latency_ms":200.0,"record_id":"...","logical_request_id":"..."}

event: message_stop
data: {"content":"{\"city\":\"Paris\"}","parsed":{"city":"Paris"},"finish_reason":"stop","end_to_end_latency_ms":200.0}
```

结构化输出在流式里也是先逐块吐文本，全部到齐后再解析。`message_stop.data["parsed"]` 是校验后的对象。解析失败时，已输出的 `content_delta` 后面会再来一条 `usage` 和一条 `error`，然后抛出 `GatewayError`。

```python
from model_gateway import decode_sse

frames = []
for frame in gateway.stream_sse(ChatRequest(
    provider="deepseek",
    messages=[ChatMessage(role="user", content="Return json")],
    response_format=ResponseFormat(type="json_object"),
)):
    event, data = decode_sse(frame)
    frames.append((event, data))
```

流一旦已经向调用方吐出过字节，中途失败不再重试，避免把同一段回答发两遍。还没吐出任何块时，可重试错误仍会退避后重来。

`stream_events()` 返回同样的事件，只是 Python 对象，不是 SSE 文本。

## 花费、时间和错误率

每一次尝试写入 `call_records` 一行，包括被退避掉的失败尝试。字段覆盖供应商、模型、提示词版本、开始时间、`latency_ms`、状态、错误码、各类 token、`cost_usd`、`price_tier`、供应商请求 id、是否流式、`finish_reason`。

```python
summary = gateway.usage.summary(provider="deepseek")
print(summary.cost_usd, summary.avg_latency_ms, summary.max_latency_ms)
print(summary.error_rate, summary.request_error_rate)

rows = gateway.usage.list_calls(logical_request_id=response.logical_request_id)
```

两个错误率不一样：

- `error_rate` = 失败尝试数 / 全部尝试数。第一次限流、第二次成功，这里是 0.5。
- `request_error_rate` = 最终失败的用户调用数 / 用户调用数。上面那次最终成功，这里是 0。

`list_calls()` 可按 `provider`、`model`、`logical_request_id`、`status`、`since`、`until` 过滤。`summary()` 支持 `provider`、`model`、`since`、`until`。时间比较用 ISO 字符串，传入带时区的 `datetime`。

### 价格

数字来自 2026-10-28 查询的公开文档，单位是美元 / 百万 token。官方调价后要改 `model_gateway/pricing.py`。

DeepSeek Flash：

| | 闲时 | 高峰 |
| --- | --- | --- |
| 输入，缓存未命中 | 0.15 | 0.30 |
| 输入，缓存命中 | 0.003 | 0.006 |
| 输出 | 0.60 | 1.20 |

高峰是周一到周五的 UTC 01:00–04:00 和 06:00–10:00，结束时刻不算在内。周末全天闲时。中国法定节假日不会自动识别；把日期放进 `ModelGateway(..., holidays={date(2026, 10, 1)})` 后，这些日期整天按闲时计。

Anthropic 按各模型的输入、输出、缓存读取、5 分钟缓存写入、1 小时缓存写入计价。`claude-haiku-5-5` 在提示词不超过 10 万 token 时用低档，超过则用高档。供应商没把缓存写入拆成 5 分钟和 1 小时时，全部按 5 分钟档计算。

计费用的是未命中输入、缓存读取、缓存写入和输出，不会把已经含在总输入里的 token 再乘一次单价。

## 韧性

### 指数退避

默认 `RetryPolicy`：最多 3 次尝试，基础间隔 0.5 秒，倍数 2，上限 30 秒，抖动比例 0.2。第 n 次失败后的等待是：

```
min(上限, 基础间隔 × 倍数^(n-1))，再加减抖动
```

响应里有 `Retry-After`（秒数或 HTTP 日期）时，等待不会短于这个值。

默认可重试：`RATE_LIMITED`、`TIMEOUT`、`OVERLOADED`、`NETWORK_ERROR`、`PROVIDER_ERROR`。

认证失败、参数错误、余额不足、JSON 解析失败不重试。

### 限流

`RateLimitPolicy` 按供应商单独配置。不传则不限。

| 字段 | 含义 |
| --- | --- |
| `requests_per_minute` | 每分钟请求数 |
| `request_burst` | 请求桶容量，默认等于每分钟请求数 |
| `input_tokens_per_minute` | 按估算输入 token 限流，估算值是字符数除以 4 |
| `token_burst` | token 桶容量，默认等于每分钟 token 数 |

桶一开始是满的。把 `request_burst` 设为 1，就是匀速放行：立刻通过一次，之后按速率等待。等待算进 `latency_ms`。

### 错误码

业务代码只看 `GatewayError.code`，不必分支两家原始报错。`str(exc)` 形如 `[RATE_LIMITED] slow down`。

| 错误码 | 典型来源 | 默认重试 |
| --- | --- | --- |
| `INVALID_REQUEST` | 参数、提示词、未知模型 | 否 |
| `AUTHENTICATION` | 缺密钥或 401 | 否 |
| `PERMISSION_DENIED` | 403 | 否 |
| `QUOTA_EXCEEDED` | 402、余额不足 | 否 |
| `RATE_LIMITED` | 429 | 是 |
| `TIMEOUT` | 408、504、读超时 | 是 |
| `OVERLOADED` | 529、资源不足 | 是 |
| `PROVIDER_ERROR` | 5xx、空响应、坏掉的 SSE | 是 |
| `NETWORK_ERROR` | 连接失败 | 是 |
| `PARSE_ERROR` | 正文不是合法 JSON，或不符合 schema | 否 |
| `CONTENT_FILTER` | 内容过滤 | 否 |
| `CONTEXT_LENGTH` | 413、请求过大 | 否 |
| `CANCELLED` | 预留 | 否 |
| `UNKNOWN` | 未识别状态或意外异常 | 否 |

`finish_reason` 为 `content_filter` 时调用仍记成功，原因留在记录上。`insufficient_system_resource` 视为 `OVERLOADED`：流还没开始时会重试。

## 数据库

一个 SQLite 文件里有三张业务表，由 `model_gateway/db.py` 在打开时创建。

| 表 | 内容 |
| --- | --- |
| `prompt_versions` | 提示词 key、版本、角色、正文、槽位 JSON、状态、变更说明、创建时间 |
| `external_materials` | 资料 id、版本、标题、正文、类型、来源、创建时间 |
| `call_records` | 每一次尝试的用量、花费、延迟和错误 |

提示词库和账本各用一条连接，写操作有锁。默认开启 WAL。

## 测试

```bash
python3 -m pytest tests -q
```

`tests/test_live.py` 在环境里没有对应密钥时跳过。先 `source .env`，再跑真实接口，避免把密钥写进 shell 历史：

```bash
set -a
source .env
set +a
python3 -m pytest tests/test_live.py -q
```

其余测试使用 `httpx.MockTransport` 或内存里的假供应商，不访问网络。覆盖参数校验、提示词版本、变量和外部资料、价格时段、退避与限流、SSE、两家请求体、流式 JSON、失败入账。

## 已知边界

- 只有同步客户端，没有 async 接口。
- 结构化校验实现了常用 JSON Schema 约束（类型、必填、枚举、额外属性、数组元素），没有实现 `minimum`、`pattern` 这类数值和字符串格式约束。
- DeepSeek 的 schema 约束是「JSON 模式 + 本地校验」，不是供应商侧的严格 schema 解码。
- 节假日高峰豁免需要调用方传入日期。
- 价格是文档快照。调价后更新 `pricing.py`，或用 `price_overrides={"deepseek:your-model": FlatPrice(...)}` 覆盖单个模型。`FlatPrice` 来自 `model_gateway.pricing`。
