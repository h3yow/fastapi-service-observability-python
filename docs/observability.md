# 可观测性指标说明

## 1. 暴露方式

* 端点：`GET /metrics`（不写入 OpenAPI schema）
* 内容类型：`text/plain; version=0.0.4; charset=utf-8`（Prometheus 文本格式 0.0.4）
* 采集目标可直接配置 Prometheus scrape，或使用任何兼容该格式的采集器。
* `/metrics` 请求本身也会被采集（自抓取的那一刻，它的在途数会显示为 1，
  这是正常且自洽的现象）。

## 2. 指标清单

业务指标：

| 指标 | 类型 | 标签 | 语义 |
| --- | --- | --- | --- |
| `http_requests_total` | counter | `method`, `route`, `status`, `outcome` | 已完成 HTTP 请求总数 |
| `http_request_duration_seconds` | histogram | `method`, `route`, `outcome` | 请求处理墙钟耗时（秒），固定桶：`0.005 … 10` 及 `+Inf` |
| `http_inflight_requests` | gauge | `method`, `route` | 当前正在处理、尚未完成的请求数 |

自监控指标（描述容量防护层自身状态，详见第 5 节）：

| 指标 | 类型 | 标签 | 语义 |
| --- | --- | --- | --- |
| `observability_series_current` | gauge | `metric` | 各业务指标族当前的序列数（含兜底序列） |
| `observability_series_limit` | gauge | 无 | 配置的序列总量上限（`+Inf` 表示不设限） |
| `observability_overflow_total` | counter | `metric` | 因超限被收敛进兜底序列的记录次数 |
| `observability_render_duration_seconds` | gauge | 无 | 最近一次抓取中渲染业务指标的墙钟耗时（秒） |

## 3. 标签约定（有界且稳定）

### 3.1 `method`

HTTP 方法大写形式（`GET`/`POST`/…），属于协议固定的小集合；缺失时为
`UNKNOWN`。

### 3.2 `route` —— 按路由模板归类，不按路径展开

* 匹配到路由时取**路由模板**：`/items/1` 与 `/items/42` 都计入
  `route="/items/{item_id}"`，不会因 id 数量而膨胀序列。
* 未匹配任何路由（404）时固定为 `__unmatched__`。任意数量、任意形态的
  未知 URL 只产生这一个序列，**不存在无界标签组合**。
* 方法不允许（405）时路径确实匹配某个模板，计入该模板
  （如 `route="/items/{item_id}",status="405"`），同样有界。
* `/metrics` 固定为 `route="/metrics"`。
* 触发容量上限时，新组合统一收敛到 `__overflow__`（见第 5 节）。

模板在请求进入时通过路由表预解析（供在途 gauge 使用），请求结束后再以
框架写入 `scope["route"]` 的结果作为计数/直方图的权威标签（覆盖嵌套
`include_router` 的场景）。

### 3.3 `status`

* 标准区间 100–599：三位数字状态码字符串，如 `"200"`、`"500"`、`"504"`。
* **非标准状态码**（上游或代理自行发明的、超出 100–599 的取值，如
  `99`、`777`、`999`）：一律收敛为固定值 `"other"`，不会按取值展开序列。
* 客户端在任何响应产生前断开：固定 `"none"`。
* 超时尚未开始响应时中间件补发 `504`；未处理异常尚未开始响应时补发
  `500`（在 FastAPI 标准栈中，500 通常已由框架错误中间件产生）。

### 3.4 `outcome` —— 失败面可逐类下钻

固定取值集合（不会出现其它值）：

| `outcome` | 含义 | 典型 `status` |
| --- | --- | --- |
| `ok` | 响应完整发送且状态码 < 400 | 200/302/… |
| `client_error` | 响应完整发送且状态码为 4xx | 404/405/422/… |
| `error` | 响应完整发送且状态码 ≥ 500（无异常逃逸） | 503 等 |
| `exception` | 处理过程中有异常逃逸出应用 | 500 |
| `timeout` | 处理超过配置的请求超时（`request_timeout`） | 504 或已发送的状态 |
| `client_disconnect` | 响应完成前客户端断开/连接失效 | `none` 或已发送的状态 |

**判定优先级（多条件同时成立时，顺序固定、结果可解释）：**

```
timeout > client_disconnect > exception > error(≥500) > client_error(4xx) > ok
```

关键边界：

* **响应已完整发送后**才到达的断开事件或超时信号，不会改写该请求的
  outcome（避免 keep-alive 连接拆除污染统计）。
* 超时优先于断开：超时与断开同时出现时记 `timeout`。
* 异常被框架转成 500 响应时，`status="500", outcome="exception"`，与
  业务主动返回的 `status="503", outcome="error"` 可明确区分。
* 非标准状态码按同样的数值区间归类（如 `777` → `error`、`99` → `ok`），
  因此六个分类之和恒等于已完成请求总数。
* 客户端断开后处理协程会被立即取消（anyio cancel scope），不会悬挂资源；
  无响应可发时状态为 `none`，与 4xx/5xx 可区分。

## 4. 容量防护：序列上限与兜底收敛

公网环境下扫描器/探测器会用大量杂乱请求形态试探服务。虽然标签词汇本身
有界（路由模板 + 固定 outcome + 标准状态码），为防万一（如路由表过大、
动态注册路由），注册表提供**序列总量上限**作为硬防护：

* `max_series` 配置所有业务指标族**共享**的序列（标签组合）总数预算，
  默认 `1000`；传 `None` 表示不设限。
* 预算耗尽后，带新标签组合的记录统一收敛到该指标族的**固定兜底序列**
  （所有标签取值为 `__overflow__`），并在 `observability_overflow_total`
  中按指标族累计被收敛的记录次数。
* 每个指标族注册时为其兜底序列**预留一个槽位**，因此兜底序列永远建得
  出来：**任何时刻对外暴露的序列总数都不超过上限**（上限应不小于业务
  指标族数量 3，更小的配置属于误配，可通过自监控指标观察到）。
* 被收敛的请求**不少算也不重复算**：它们照常递增兜底序列的
  counter/gauge/histogram，第 5 节的守恒关系不受影响。
* 写路径的超限判断是一次字典成员测试加一次整数比较（O(1)），高并发下
  不会因为接近上限而变慢；超限不报错、不丢请求。
* 自监控指标族（`observability_*`）本身豁免上限、不参与计数，其标签
  取值仅为指标族名称，有界。

### 自监控指标怎么用

* **是否接近吃满**：`observability_series_current` 合计值 /
  `observability_series_limit`。
* **是否开始走兜底**：`observability_overflow_total` 大于 0 即已有记录
  被收敛；业务指标里出现 `__overflow__` 序列是同一事件的直接证据。
* **抓取成本**：`observability_render_duration_seconds` 为本次抓取渲染
  业务指标的耗时（在同一份抓取输出中即可读到）。
* 三个指标族各有一条零值序列会预先创建（`observability_overflow_total`
  按族各一条），取值稳定、缺席即零，便于直接配告警。

## 5. 指标自洽（守恒关系）

对每个“已完成”的请求，中间件恰好执行：一次 counter 递增、一次 histogram
观测；在途 gauge 在进入时 `+1`、任意退出路径（含外层取消）`-1`。

因此任意时刻：

```
sum(http_requests_total)                 == 已完成请求总数
sum(http_request_duration_seconds_count) == 已完成请求总数   （恒等）
sum(http_inflight_requests)              == 当时仍在处理的请求数（抓取 /metrics 自身时为 1）
sum by (outcome) (http_requests_total)   == 已完成请求总数   （六类之和）
```

被容量防护收敛进 `__overflow__` 的记录同样计入上述各式（它们递增的是
兜底序列），即：状态、耗时、在途三类指标相互自洽，异常/断开/超时/超限
链路同样参与守恒。

## 6. 并发与性能

* 注册表所有读写由一把 `threading.Lock` 保护；热路径为 O(1) 字典操作
  （外加与标签数量成正比的键构造），高并发下同一序列计数不丢失、不重复。
* 计数成本只与桶数/标签数有关，**不随历史请求量增长**（有测试校验）。
* 序列数量有双重上界：有界标签词汇 + `max_series` 硬上限，与请求数量、
  URL 数量无关。
* 转发客户端消息使用有限缓冲内存流，保证框架从不调用 `receive` 的普通
  GET 请求也能及时收到 `http.disconnect` 并取消处理。

## 7. 初始化与生命周期

* `setup_observability(app, ...)` **幂等**：对同一个 app 重复调用直接
  返回既有注册表，不重复挂中间件、不重复注册 `/metrics`。
* `create_app()` 每次重建应用都会创建**独立的新注册表**，计数器从零
  开始，不会出现重复序列或启动失败。
* `enabled=False`（总开关）：所有指标族——包括 `observability_*` 自监控
  ——写入为空操作且不出现在 `/metrics` 输出中，抓取返回空 body。
* `duration_enabled=False` 时：仅耗时直方图关闭（输出缺失、写入空转），
  counter 与 gauge 照常工作。指标被关闭后重新启用，关闭期间的观测不会
  被补记。

## 8. 配置

```python
setup_observability(
    app,
    request_timeout=5.0,       # 秒；None 表示不启用请求超时
    enabled=True,              # 总开关；False 关闭全部指标（含自监控）
    duration_enabled=True,     # False 关闭耗时直方图
    max_series=1000,           # 业务指标族共享的序列上限；None 表示不设限
    registry=None,             # 可注入自定义注册表（此时 max_series/enabled
                               # 由注入的注册表自身决定）
)
```

`create_app(...)` 与 `setup_observability(...)` 接受同一组参数。

## 9. 与上一版的兼容范围

* **不变**：指标名、标签名、文本格式；`ok`/`error`/`exception`/`timeout`/
  `client_disconnect` 五个 outcome 的语义；路由模板归一；守恒关系；
  幂等装配；`request_timeout`/`duration_enabled` 配置语义。
* **行为变化（需注意）**：
  * 4xx 响应的 `outcome` 由 `ok` 改为 `client_error`（此前 4xx 计入 `ok`）。
    依赖 `outcome="ok"` 统计成功量的看板/告警如需旧口径，应改为
    `outcome=~"ok|client_error"` 或按 `status` 过滤。
  * 非标准状态码（超出 100–599）的 `status` 标签由原始数字改为 `"other"`。
* **纯新增（不破坏旧消费方）**：
  * `/metrics` 输出新增四个 `observability_*` 自监控指标族；
  * 序列上限默认 1000（此前无上限）。标签词汇本身有界，正常服务远达
    不到该值；如需旧行为可传 `max_series=None`。

## 10. 验证方法

```bash
uv run pytest -q                       # 全部测试
uv run pytest tests/test_middleware.py -s
uv run pytest tests/test_cardinality.py -s
uv run pytest tests/test_real_disconnect.py -s
```

* `tests/test_labels.py`：标签归一（含非标准状态码 → `other`）与 outcome
  判定优先级（含 4xx → `client_error`、多条件并存）。
* `tests/test_registry.py`：16 线程 × 500 次同序列递增严格守恒；
  gauge 增减抵消；histogram 累积桶正确；禁用指标不出现在输出且写入空转；
  重复注册幂等；文本格式与转义。
* `tests/test_cardinality.py`：容量防护与自监控——
  * 16 线程 × 200 个不同标签组合冲击 `max_series=50`：序列数恰为 50
    不被突破，counter 总和 == 写入总数（守恒），兜底序列值与
    `observability_overflow_total` 精确一致；
  * 兜底序列标签全为 `__overflow__`；直方图兜底分桶守恒；在途 gauge
    兜底序列增减抵消归零；
  * `max_series=None` 不设限且上限指标显示 `+Inf`；
  * 自监控四件套出现在抓取输出中，且随总开关关闭而消失；
  * 应用级：30 条路由 + `max_series=20`，抓取输出中序列数 ≤ 20、
    请求总数/直方图计数仍守恒、兜底计数正确；
  * 非标准状态码 777/99 经中间件收敛为 `status="other"`。
* `tests/test_middleware.py`：路由归一、各 `outcome` 区分（含 4xx →
  `client_error`）、高并发守恒、幂等装配、指标关闭、记录成本不随
  历史增长。
* `tests/test_real_disconnect.py`：真实 uvicorn + 裸 TCP，请求处理中途
  关闭连接，断言记录为 `status=none, outcome=client_disconnect`。

所有用例均通过 `pytest -s` 打印“输入 + 判定依据 + 实际样本”。

手动抓取验证：

```bash
curl -s http://127.0.0.1:8000/items/1 >/dev/null
curl -s http://127.0.0.1:8000/nope/x >/dev/null
curl -s http://127.0.0.1:8000/metrics
# 关注输出尾部的 observability_* 自监控区块
```

## 11. 设计边界

* 中间件位于 FastAPI 用户中间件层（在框架错误中间件内侧）。若在
  `setup_observability` 之后再向更外层添加自定义中间件，且该外层中间件
  在调用内层应用之前就抛出异常，则这类请求不会被本采集链路记录。
* 请求超时作用于整个应用处理过程；超时会取消处理协程并尽量补发 504，
  若客户端在此之前已断开则不再尝试发送。
* `max_series` 是三个业务指标族共享的总预算，且应不小于指标族数量（3），
  否则兜底槽位本身就会占满预算（属于误配，自监控指标可直接观察到）。
