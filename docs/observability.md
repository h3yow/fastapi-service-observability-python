# 可观测性指标说明

## 1. 暴露方式

* 端点：`GET /metrics`（不写入 OpenAPI schema）
* 内容类型：`text/plain; version=0.0.4; charset=utf-8`（Prometheus 文本格式 0.0.4）
* 采集目标可直接配置 Prometheus scrape，或使用任何兼容该格式的采集器。
* `/metrics` 请求本身也会被采集（自抓取的那一刻，它的在途数会显示为 1，
  这是正常且自洽的现象）。

## 2. 指标清单

| 指标 | 类型 | 标签 | 语义 |
| --- | --- | --- | --- |
| `http_requests_total` | counter | `method`, `route`, `status`, `outcome` | 已完成 HTTP 请求总数 |
| `http_request_duration_seconds` | histogram | `method`, `route`, `outcome` | 请求处理墙钟耗时（秒），固定桶：`0.005 … 10` 及 `+Inf` |
| `http_inflight_requests` | gauge | `method`, `route` | 当前正在处理、尚未完成的请求数 |

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

模板在请求进入时通过路由表预解析（供在途 gauge 使用），请求结束后再以
框架写入 `scope["route"]` 的结果作为计数/直方图的权威标签（覆盖嵌套
`include_router` 的场景）。

### 3.3 `status`

* 响应已开始：三位数字状态码字符串，如 `"200"`、`"500"`、`"504"`。
* 客户端在任何响应产生前断开：固定 `"none"`。
* 超时尚未开始响应时中间件补发 `504`；未处理异常尚未开始响应时补发
  `500`（在 FastAPI 标准栈中，500 通常已由框架错误中间件产生）。

### 3.4 `outcome` —— 异常链路可区分

固定取值集合（不会出现其它值）：

| `outcome` | 含义 | 典型 `status` |
| --- | --- | --- |
| `ok` | 响应完整发送且状态码 < 500 | 200/404/405/… |
| `error` | 响应完整发送且状态码 ≥ 500（无异常逃逸） | 503 等 |
| `exception` | 处理过程中有异常逃逸出应用 | 500 |
| `timeout` | 处理超过配置的请求超时（`request_timeout`） | 504 或已发送的状态 |
| `client_disconnect` | 响应完成前客户端断开/连接失效 | `none` 或已发送的状态 |

**判定优先级（多条件同时成立时，顺序固定、结果可解释）：**

```
timeout > client_disconnect > exception > error(5xx) > ok
```

关键边界：

* **响应已完整发送后**才到达的断开事件或超时信号，不会改写该请求的
  outcome（避免 keep-alive 连接拆除污染统计）。
* 超时优先于断开：超时与断开同时出现时记 `timeout`。
* 异常被框架转成 500 响应时，`status="500", outcome="exception"`，与
  业务主动返回的 `status="503", outcome="error"` 可明确区分。
* 客户端断开后处理协程会被立即取消（anyio cancel scope），不会悬挂资源；
  无响应可发时状态为 `none`，与 4xx/5xx 可区分。

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

## 5. 并发与性能

* 注册表所有读写由一把 `threading.Lock` 保护；热路径为 O(1) 字典操作
  （外加与标签数量成正比的键构造），高并发下同一序列计数不丢失、不重复。
* 计数成本只与桶数/标签数有关，**不随历史请求量增长**（有测试校验）。
* 序列数量由有界标签词汇决定，与请求数量、URL 数量无关。
* 转发客户端消息使用有限缓冲内存流，保证框架从不调用 `receive` 的普通
  GET 请求也能及时收到 `http.disconnect` 并取消处理。

## 6. 初始化与生命周期

* `setup_observability(app, ...)` **幂等**：对同一个 app 重复调用直接
  返回既有注册表，不重复挂中间件、不重复注册 `/metrics`。
* `create_app()` 每次重建应用都会创建**独立的新注册表**，计数器从零
  开始，不会出现重复序列或启动失败。
* `duration_enabled=False` 时：耗时直方图既不出现在 `/metrics` 输出中，
  写操作也为空操作；counter 与 gauge 照常工作。指标被关闭后重新启用，
  关闭期间的观测不会被补记。

## 7. 配置

```python
setup_observability(
    app,
    request_timeout=5.0,       # 秒；None 表示不启用请求超时
    duration_enabled=True,     # False 关闭耗时直方图
    registry=None,             # 可注入自定义注册表（同规格指标）
)
```

## 8. 验证方法

```bash
uv run pytest -q                       # 全部测试
uv run pytest tests/test_middleware.py -s
uv run pytest tests/test_real_disconnect.py -s
```

* `tests/test_labels.py`：标签归一与 outcome 判定优先级（含多条件并存）。
* `tests/test_registry.py`：16 线程 × 500 次同序列递增严格守恒；
  gauge 增减抵消；histogram 累积桶正确；禁用指标不出现在输出且写入空转；
  重复注册幂等；文本格式与转义。
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
curl -s http://127.0.0.1:8000/metrics
```

## 9. 设计边界

* 中间件位于 FastAPI 用户中间件层（在框架错误中间件内侧）。若在
  `setup_observability` 之后再向更外层添加自定义中间件，且该外层中间件
  在调用内层应用之前就抛出异常，则这类请求不会被本采集链路记录。
* 请求超时作用于整个应用处理过程；超时会取消处理协程并尽量补发 504，
  若客户端在此之前已断开则不再尝试发送。
