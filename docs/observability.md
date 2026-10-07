# 可观测性指标说明

> v2 更新（容量防护 / 自监控 / 失败细分类）：新增**每族序列上限与兜底
> 收敛**、`observability_*` **自监控指标族**、面向看板的 **`result` 六分类
> 标签**，并把非标准方法/状态码收敛到固定取值。配置项、默认值与兼容范围见
> [第 7 节](#7-配置) 与 [第 10 节](#10-与上一版的兼容范围)。

## 1. 暴露方式

* 端点：`GET /metrics`（不写入 OpenAPI schema）
* 内容类型：`text/plain; version=0.0.4; charset=utf-8`（Prometheus 文本格式 0.0.4）
* 采集目标可直接配置 Prometheus scrape，或使用任何兼容该格式的采集器。
* `/metrics` 请求本身也会被采集（自抓取的那一刻，它的在途数会显示为 1，
  这是正常且自洽的现象）。

## 2. 指标清单

### 2.1 业务指标

| 指标 | 类型 | 标签 | 语义 |
| --- | --- | --- | --- |
| `http_requests_total` | counter | `method`, `route`, `status`, `outcome`, `result` | 已完成 HTTP 请求总数 |
| `http_request_duration_seconds` | histogram | `method`, `route`, `outcome` | 请求处理墙钟耗时（秒），固定桶：`0.005 … 10` 及 `+Inf` |
| `http_inflight_requests` | gauge | `method`, `route` | 当前正在处理、尚未完成的请求数 |

### 2.2 容量防护自监控指标

同一次抓取即可判断防护层是否接近吃满、是否已开始走兜底、渲染一次要多久。
这五个族**自身不受容量上限约束**（取值天然有界），并随总开关一起启停。

| 指标 | 类型 | 标签 | 语义 |
| --- | --- | --- | --- |
| `observability_metric_series` | gauge | `family` | 每个业务族当前实际暴露的序列数 |
| `observability_metric_series_cap` | gauge | `family` | 该族配置的序列上限；`0` 表示不设限 |
| `observability_metric_overflow_active` | gauge | `family` | 该族是否正在兜底：`1`=已到上限、新组合走兜底序列；`0`=尚未触发 |
| `observability_metric_overflow_total` | counter | `family` | 自启动以来被收敛到兜底序列的**写入次数**（按业务族区分） |
| `observability_render_duration_seconds` | histogram | 无 | 一次 `/metrics` 抓取渲染的墙钟耗时，固定桶 `0.0001 … 1.0` 及 `+Inf` |

`family` 的取值为业务指标族名（如 `http_requests_total`）；自监控族不描述
自身，已被单独禁用的业务族不会出现在这三个 gauge 中。

## 3. 标签约定（有界且稳定）

### 3.1 `method`

HTTP 方法大写形式，只保留 HTTP 语义标准定义的 9 个动词
（`GET/HEAD/POST/PUT/DELETE/CONNECT/OPTIONS/TRACE/PATCH`）；缺失/为空时为
`UNKNOWN`；扫描器发来的任何非标准动词（`PROPFIND`、`BREW`、乱码等）统一
收敛为 `OTHER`，不按动词取值展开序列。

### 3.2 `route` —— 按路由模板归类，不按路径展开

* 匹配到路由时取**路由模板**：`/items/1` 与 `/items/42` 都计入
  `route="/items/{item_id}"`，不会因 id 数量而膨胀序列。
* 未匹配任何路由（404）时固定为 `__unmatched__`。任意数量、任意形态的
  未知 URL 只产生这一个序列。
* 方法不允许（405）时路径确实匹配某个模板，计入该模板
  （如 `route="/items/{item_id}",status="405"`），同样有界。
* `/metrics` 固定为 `route="/metrics"`。

模板在请求进入时通过路由表预解析（供在途 gauge 使用），请求结束后再以
框架写入 `scope["route"]` 的结果作为计数/直方图的权威标签（覆盖嵌套
`include_router` 的场景）。

### 3.3 `status` —— 非标准码收敛

* 响应已开始且状态码在标准 HTTP 范围 **100–599**：三位数字符串，如
  `"200"`、`"500"`、`"504"`。
* 客户端在任何响应产生前断开：固定 `"none"`。
* 上游/代理返回的**越界或非数值状态码**（如 `0`、`99`、`612`、`999`）：
  统一收敛为 `"other"`，不会一个码撑开一个序列。
* 超时尚未开始响应时中间件补发 `504`；未处理异常尚未开始响应时补发
  `500`（在 FastAPI 标准栈中，500 通常已由框架错误中间件产生）。

### 3.4 `outcome` —— 异常链路可区分

固定取值集合（不会出现其它值）：

| `outcome` | 含义 | 典型 `status` |
| --- | --- | --- |
| `ok` | 响应完整发送且状态码在 1xx–4xx | 200/404/405/… |
| `error` | 响应完整发送且为 5xx，或为非标准状态码（无异常逃逸） | 503、`other` |
| `exception` | 处理过程中有异常逃逸出应用 | 500 |
| `timeout` | 处理超过配置的请求超时（`request_timeout`） | 504 或已发送的状态 |
| `client_disconnect` | 响应完成前客户端断开/连接失效 | `none` 或已发送的状态 |

**判定优先级（多条件同时成立时，顺序固定、结果可解释）：**

```
timeout > client_disconnect > exception > error(5xx / 非标准码) > ok
```

关键边界：

* **响应已完整发送后**才到达的断开事件或超时信号，不会改写该请求的
  outcome（避免 keep-alive 连接拆除污染统计）。
* 超时优先于断开：超时与断开同时出现时记 `timeout`。
* 异常被框架转成 500 响应时，`status="500", outcome="exception"`，与
  业务主动返回的 `status="503", outcome="error"` 可明确区分。
* 客户端断开后处理协程会被立即取消（anyio cancel scope），不会悬挂资源；
  无响应可发时状态为 `none`，与 4xx/5xx 可区分。

### 3.5 `result` —— 可直接配错误率/可用性看板的六分类

`outcome` 保留传输层终态语义；`result` 在其基础上把“正常响应”进一步按
状态码区间拆成成功与客户端出错，取值固定为以下六个，互不重叠且覆盖全部
终态：

| `result` | 含义 | 典型 `status` / 条件 |
| --- | --- | --- |
| `success` | 完整响应，1xx/2xx/3xx | 200/301 |
| `client_error` | 完整响应，4xx（客户端可修正的错） | 400/404/405/429 |
| `server_error` | 完整响应，5xx，或**非标准状态码** | 500/502/503、`other` |
| `exception` | 异常逃逸出应用（或应用未产生任何响应且无其它原因） | 500 / `none` |
| `timeout` | 超过 `request_timeout` | 504 或已发送状态 |
| `client_disconnect` | 响应完成前客户端断开 | `none` 或已发送状态 |

判定优先级与 `outcome` 的失败模式部分一致：

```
timeout > client_disconnect > exception > server_error > client_error > success
```

非标准码在 `result` 中归入 `server_error`：出现非预期状态码不是客户端能
修正的问题，应计入错误率/可用性看板的分子。

**守恒：** 每个请求恰好落入一个 `result` 类，因此

```
sum by result (http_requests_total) 之和 == sum(http_requests_total)
```

### 3.6 容量兜底标签 `__overflow__`

当某个指标族达到序列上限（见第 5 节），所有新标签组合统一收敛到该族**
唯一的兜底序列**，其每个标签位都取固定值 `__overflow__`。例如：

```
http_requests_total{method="__overflow__",route="__overflow__",status="__overflow__",
                    outcome="__overflow__",result="__overflow__"} 17
```

兜底序列只有一个，不会出现第二个。

## 4. 指标自洽（守恒关系）

对每个“已完成”的请求，中间件恰好执行：一次 counter 递增、一次 histogram
观测；在途 gauge 在进入时 `+1`、任意退出路径（含外层取消）`-1`。

因此任意时刻：

```
sum(http_requests_total)              == 已完成请求总数
sum(http_request_duration_seconds_count) == 已完成请求总数   （恒等）
sum(http_inflight_requests)           == 当时仍在处理的请求数（抓取 /metrics 自身时为 1）
```

即：状态、耗时、在途三类指标相互自洽，异常/断开/超时链路同样参与守恒。

容量收敛**不改变上述守恒**：被收敛的请求既计入兜底序列、也计入
`observability_metric_overflow_total{family=...}`，总量不少算也不重复算；
各 `result` 类相加仍等于总请求数。

## 5. 容量防护（每族序列上限 + 兜底收敛）

* **上限粒度与默认值**：上限按指标族分别配置（`max_series`），默认
  **500**。上限包含兜底序列本身：受保护族最多保留 `max_series - 1` 个
  自然序列，外加 1 个兜底序列，因此**任何时刻对外暴露的该族序列数恒
  `<= max_series`**，高并发下也成立（有 16 线程 × 100 写的专门用例）。
  请求计数与耗时直方图两个族受此保护；在途 gauge 例外（见下）。
* **在途 gauge 不设限（有意为之）**：`http_inflight_requests` 的标签空间
  是“路由模板 × 有界方法集”，天然有限；更关键的是它必须保证同一请求的
  `+1`（进入时按预解析路由）与 `-1`（退出时按权威路由）始终落在同一键。
  若对其设上限，极端情况下 +1/-1 可能被分别收敛，导致兜底序列残留计数。
  自监控中该族 `..._cap` 恒为 `0`（不设限）。
* **收敛规则**：自然序列预算用完后，所有此前没见过的标签组合（方法杂、
  路径杂、返回码杂等）都写入**同一个** `__overflow__` 兜底序列；超限
  既不报错也不丢请求，请求处理链路无感知。
* **计数不少算/不重算**：每次重定向到兜底序列的写入都照常 +1/入桶，
  并让 `observability_metric_overflow_total{family=...}` +1。受保护的
  counter/histogram 族只增不驱逐，同一标签组合一旦被接纳就永远写同一
  序列，因此计数严格守恒。
* **关闭上限**：`max_series=None` 表示该族不设限（自监控中
  `..._cap` 显示为 `0`）。非法取值（`0`、负数、非整数）在注册时直接
  抛 `ValueError`，属装配期错误而非请求期错误。
* **热路径开销**：是否超限的判断在既有写锁内完成，只是一次字典
  membership 判断 + 一次计数，O(1)，成本不随历史组合数增长。
* **可见性**：同一次 `/metrics` 抓取里，`observability_metric_series`
  （当前序列数）、`..._cap`（上限）、`..._overflow_active`（是否已兜底）
  即可算出“吃满程度”（`series / cap`）；`..._overflow_total` 给出累计
  被收敛量；`observability_render_duration_seconds` 给出单次渲染耗时。
  自监控只在抓取时刷新，不给请求热路径增加额外工作。

## 6. 并发与性能

* 注册表所有读写由一把 `threading.Lock` 保护；热路径为 O(1) 字典操作
  （外加与标签数量成正比的键构造），高并发下同一序列计数不丢失、不重复。
* 计数成本只与桶数/标签数有关，**不随历史请求量增长**（有测试校验）。
* 序列数量由“有界标签词汇 + 每族容量上限”双重保证，与请求数量、URL
  数量无关；达到上限后只保留固定数量的序列。
* 转发客户端消息使用有限缓冲内存流，保证框架从不调用 `receive` 的普通
  GET 请求也能及时收到 `http.disconnect` 并取消处理。

## 7. 初始化与生命周期

* `setup_observability(app, ...)` **幂等**：对同一个 app 重复调用直接
  返回既有注册表，不重复挂中间件、不重复注册 `/metrics`。
* `create_app()` 每次重建应用都会创建**独立的新注册表**，计数器从零
  开始，不会出现重复序列或启动失败。
* `duration_enabled=False` 时：耗时直方图既不出现在 `/metrics` 输出中，
  写操作也为空操作；counter 与 gauge 照常工作。指标被关闭后重新启用，
  关闭期间的观测不会被补记。
* `enabled=False` 是**总开关**：中间件仍挂载、请求照常处理，但所有
  指标写入为空操作，`/metrics` 返回空 body，**自监控族也一并隐藏**
  （不会漏出 `observability_*`）。重新打开后从当前时刻继续计数。

## 8. 配置

```python
setup_observability(
    app,
    request_timeout=5.0,       # 秒；None 表示不启用请求超时
    duration_enabled=True,     # False 关闭耗时直方图
    max_series=500,            # 每族序列上限（含兜底序列）；None 表示不设限
    enabled=True,              # False 关闭整条指标链路（含自监控）
    registry=None,             # 可注入自定义注册表（同规格指标）
)
```

`create_app(...)` 透传同样的 `max_series` / `enabled` 参数；
`create_http_registry(...)` 直接建注册表时也接受这两个参数。

| 配置项 | 默认值 | 取值 | 说明 |
| --- | --- | --- | --- |
| `max_series` | `500` | 正整数 或 `None` | 请求计数/耗时直方图两个族的最大序列数（含 1 个兜底序列）；在途 gauge 固定不设限；`None` 时三族均不设限 |
| `enabled` | `True` | `bool` | 指标链路总开关；关闭后 `/metrics` 为空且自监控隐藏 |
| `duration_enabled` | `True` | `bool` | 仅控制耗时直方图族 |
| `request_timeout` | `5.0` | 秒 或 `None` | 请求处理超时 |

## 9. 验证方法

```bash
uv run pytest -q                       # 全部测试
uv run pytest tests/test_capacity.py -s
uv run pytest tests/test_protection.py -s
uv run pytest tests/test_middleware.py -s
uv run pytest tests/test_real_disconnect.py -s
```

* `tests/test_labels.py`：方法/状态码归一（含非标准动词→`OTHER`、越界码→
  `other`）、`outcome` 与 `result` 的判定优先级（含多条件并存）。
* `tests/test_registry.py`：16 线程 × 500 次同序列递增严格守恒；
  gauge 增减抵消；histogram 累积桶正确；禁用指标不出现在输出且写入空转；
  重复注册幂等；文本格式与转义。
* `tests/test_capacity.py`（新增）：
  * 自然序列恰好用满 `cap-1`、第 `cap` 个新组合起进兜底，序列数恒
    `<= cap`（含 `cap=1` 只剩兜底、计数与直方图分桶各自守恒、gauge
    `+1/-1` 对在兜底下仍归零）；
  * **16 线程 × 100 个互不相同组合（共 1600 写）下序列数不被突破、
    存储总和严格等于 1600、兜底计数正确**；
  * `None` 不设限、非法 `max_series` 装配期报错；
  * 自监控 gauge/counter/渲染耗时内容正确、自身有界且跟随总开关。
* `tests/test_protection.py`（新增）：真实 ASGI 流量打满上限后总量与
  histogram count 守恒；非标准码 612/0/99/700/999 收敛为单个
  `status="other"` 序列；六类 `result` 齐全且相加等于总数；扫描器乱造
  动词→`OTHER`、乱路径→`__unmatched__`；`enabled=False` 时
  `/metrics` 全空（含自监控），重开后恢复。
* `tests/test_middleware.py`：
  * 多个不同 id/未知路径只产生模板序列与单个 `__unmatched__` 序列；
  * 80 并发 + 110 混合流量（正常/异常/404/超时）下 counter 总和、
    histogram count 总和、在途归零三者守恒；
  * `/boom`(exception/500)、`/http-error`(error/503)、`/slow`
    (timeout/504)、中途断开（client_disconnect/none）互不污染；
  * 幂等装配、应用重建隔离、关闭指标后输出缺失；
  * 同序列连续 5k/20k 次写入的单次摊销成本基本持平。
* `tests/test_real_disconnect.py`：真实 uvicorn + 裸 TCP，请求处理中途
  关闭连接，断言记录为 `status=none, outcome=client_disconnect`。

所有用例均通过 `pytest -s` 打印“输入 + 判定依据 + 实际样本”。

手动抓取验证：

```bash
curl -s http://127.0.0.1:8000/items/1 >/dev/null
curl -s http://127.0.0.1:8000/nope/x >/dev/null
curl -s http://127.0.0.1:8000/metrics | grep observability_
```

刻意打满上限（例如把 `create_app(max_series=2)` 跑起来后打多种路径），
可观察到 `observability_metric_overflow_active{family="http_requests_total"} 1`
与不断增长的 `..._overflow_total`，而业务序列总数始终不超过 2。

## 10. 与上一版的兼容范围

* **默认行为兼容**：不传新参数时，采集语义、指标名、默认超时、幂等装配
  与上一版一致；`http_inflight_requests`、
  `http_request_duration_seconds` 的标签集合**未变**。
* **有意的标签变更**：`http_requests_total` **新增**一个 `result` 标签
  维度（取值为固定六类）。原有按 `method/route/status/outcome` 聚合的
  PromQL 不受影响（Prometheus 按标签自由聚合），但精确匹配完整标签集的
  告警/录制规则需要补上 `result`。
* **更严格的收敛**：非标准方法由原来的“原样大写”改为 `OTHER`；越界
  状态码由原来的原样字符串改为 `other`，且其 `outcome` 为 `error`、
  `result` 为 `server_error`。标准方法与 100–599 状态码完全不变。
* **新增默认上限**：每个业务族默认最多 500 个序列（此前为隐式无界）。
  正常路由/状态/动作组合远低于此；确需放开用 `max_series=None`。
* **新增指标族**：`observability_*` 五个族为新增输出，可被抓取端直接
  忽略；它们随 `enabled` 总开关与 `/metrics` 同生共隐。

## 11. 设计边界

* 中间件位于 FastAPI 用户中间件层（在框架错误中间件内侧）。若在
  `setup_observability` 之后再向更外层添加自定义中间件，且该外层中间件
  在调用内层应用之前就抛出异常，则这类请求不会被本采集链路记录。
* 请求超时作用于整个应用处理过程；超时会取消处理协程并尽量补发 504，
  若客户端在此之前已断开则不再尝试发送。
* 容量上限按族独立计算，不做跨族共享配额；请求计数与耗时直方图两族各自
  不超过 `max_series`，故一次抓取中二者的业务序列总数不超过
  `2 × max_series`（histogram 被禁用时只剩一族）；在途 gauge 不设限但
  标签空间天然有界（路由模板数 × 方法数），自监控族另计且自身有界。
* 兜底是“只进不出”的：序列一旦建立不会被驱逐回收，避免热键抖动；需要
  重置可重建应用（得到全新注册表）。
