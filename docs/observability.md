# 可观测性指标（HTTP Metrics）

本服务在 ASGI 层统一采集所有对外 HTTP 请求，并以 Prometheus 文本格式
（text exposition format, version `0.0.4`）在 `GET /metrics` 暴露。
采集与业务代码解耦，位于 `app/observability/`。

## 1. 指标一览

| 指标名 | 类型 | 含义 |
| --- | --- | --- |
| `http_requests_total` | counter | 已处理 HTTP 请求总数，单调递增 |
| `http_request_duration_seconds` | histogram | 请求处理耗时（秒），含 `_bucket` / `_sum` / `_count` |
| `http_requests_in_progress` | gauge | 当前在途（尚未结束）的请求数 |

每个请求在结束时贡献：counter 恰好 +1、histogram 恰好一次观测；在整个
生命周期内对 gauge 贡献一次平衡的 `+1`/`-1`。三者因此自洽：

```
sum(http_requests_total)                         == 业务请求总数
http_request_duration_seconds_count（按标签求和） == http_requests_total
http_requests_in_progress（任意时刻）             == 在途请求数（稳态为 0）
```

### 标签

`http_requests_total` 与 `http_request_duration_seconds` 共享四个标签：

| 标签 | 取值（有界） | 说明 |
| --- | --- | --- |
| `method` | `GET POST PUT PATCH DELETE HEAD OPTIONS CONNECT TRACE`，其余统一为 `OTHER` | HTTP 方法已归一 |
| `route` | 路由**模板**，未匹配为 `unmatched` | 见“路由归一” |
| `status` | 常见状态码原文（如 `200`/`404`/`499`/`504`），其余归并为 `1xx`…`5xx`，非 HTTP 归并为 `0xx` | 状态已归一 |
| `outcome` | `success` / `client_error` / `server_error` / `exception` / `client_disconnect` / `timeout` | 处理结果类别 |

`http_requests_in_progress` 仅按 `method`、`route` 两个标签聚合。

### `outcome` 语义（异常链路可区分）

| outcome | status | 触发条件 |
| --- | --- | --- |
| `success` | 真实 1xx/2xx/3xx 或其类桶 | 正常响应 |
| `client_error` | 真实 4xx 或 `4xx` | 如 404、405、422 等客户端错误 |
| `server_error` | 真实 5xx 或 `5xx` | 端点主动返回的 5xx（如 `HTTPException(503)`） |
| `exception` | `500`（响应未开始）或已发送的状态（响应已开始后失败） | 处理过程中抛出未被捕获的异常；与“主动返回 500”严格区分 |
| `client_disconnect` | `499` | 客户端提前断开：收到 `http.disconnect`，或向对端写入时连接已失效（`BrokenResourceError` / 连接错误） |
| `timeout` | `504` | 超过 `request_timeout` 仍未完成；中间件为客户端合成 `504 Gateway Timeout` |

> 同样是 5xx：`outcome="server_error"` 表示**服务主动产生的 HTTP 响应**，
> `outcome="exception"` 表示**代码异常逃逸**，两者可分别告警/定位。
> 客户端断开（499）与服务端超时（504）也互不混淆。

## 2. 路由归一（cardinality 有界）

* 请求按 Starlette/FastAPI 的**路由模板**归类，例如 `/items/1` 与
  `/items/2` 都聚合到 `route="/items/{item_id}"`，具体 id 永不进入标签。
* 未匹配任何路由的请求统一为 `route="unmatched"`，不会出现
  `/random/13`、`/random/97` 这类随路径无限增长的标签。
* 方法不允许（405）时路径仍匹配，因此保留模板（如 `/items/{item_id}`），
  只把状态记为 405。
* `method`、`status`、`outcome` 均为启动时预注册的**闭集**；任何未注册
  的标签组合会被指标原语直接拒绝（抛 `ValueError`），运行期不可能新增
  时间序列。
* 抓取端点 `/metrics` 自身不计入上述业务指标，避免“抓取时永远观察到
  自己在途 1 个”的自指噪声（与 prometheus-client 官方中间件的处理一致）。

## 3. 并发安全与性能

* Counter / Gauge / Histogram 的每个标签组合在启动时预分配固定存储槽；
  更新是对固定槽位的 O(1) 字典定位 + 短临界区加锁，不扫描任何历史。
  因此采集耗时与累计请求量无关，不会随历史增长而变慢。
* Histogram 使用固定的累计桶（边界见
  `app/observability/metrics.py:DEFAULT_BUCKETS`），桶数量不随流量变化。
* gauge 的 `+1/-1` 与路由归移（进入时按模板预置位、必要时在响应开始/
  结束时纠正）保证在途数平衡，异常/断开/超时路径也必定归零。
* 文本输出只渲染**被触达过**的非零序列（零观测组合不产生样本行），
  在声明了大量有界组合的同时保持抓取体紧凑。

## 4. 重复初始化 / 应用重建

* `setup_observability(app)` 对同一个 app **幂等**：重复调用返回同一
  settings，不重复挂中间件、不重复添加 `/metrics` 路由、不重复注册指标。
* 每个 app 拥有独立的 `Registry`；通过 `create_app()` 重建应用会得到
  全新、空的指标集，不会与旧实例产生重复序列或启动失败。
* 可通过 `enable_requests_total` / `enable_request_duration_seconds` /
  `enable_requests_in_progress` 关闭单个指标；被关闭的指标不会出现在
  `/metrics` 输出中（连 HELP/TYPE 头都不会有）。

## 5. 抓取格式

```
# HELP http_requests_total Total number of HTTP requests handled, ...
# TYPE http_requests_total counter
http_requests_total{method="GET",route="/items/{item_id}",status="200",outcome="success"} 2.0
...
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{...,,le="0.1"} 2
http_request_duration_seconds_bucket{...,,le="+Inf"} 2
http_request_duration_seconds_sum{...} 0.0004
http_request_duration_seconds_count{...} 2
# TYPE http_requests_in_progress gauge
```

* `Content-Type: text/plain; version=0.0.4; charset=utf-8`
* 标签值按规范转义（`\\`、`\n`、`\"`）；数值支持 `NaN`/`+Inf`/`-Inf`。

## 6. 运行

```bash
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
# 产生一些流量（含异常链路）
curl -s localhost:8000/items/1
curl -s localhost:8000/items/2
curl -s localhost:8000/error                 # 500 exception
curl -s localhost:8000/http-error/503       # 503 server_error
curl -s localhost:8000/no/such/path         # 404 unmatched
curl -s localhost:8000/metrics
```

启用请求超时（超时记为 `outcome="timeout"` / `status="504"`）：

```bash
REQUEST_TIMEOUT_SECONDS=2 uv run uvicorn app.main:app --port 8000
# 或在代码中 create_app(request_timeout=2.0)
```

## 7. 验证方法

```bash
uv run pytest
```

测试会把每个用例的“输入 + 判定依据”写入 `test-observability.log`
（例如并发线程数、每次增量、期望值与实际值、异常类型、最终 gauge 值）。

覆盖范围：

* **指标原语**（`tests/test_metrics.py`）：多线程计数不丢不重、histogram
  桶/计数/和守恒、gauge 增减平衡、有界标签拒绝未知组合、重复注册报错、
  文本转义与数字格式。
* **归一与分类**（`tests/test_middleware.py`）：方法/状态闭集归一、模板
  聚合、405 保留模板、异常 500 与主动 500 可区分。
* **异常链路（原始 ASGI）**（`tests/test_asgi_paths.py`）：客户端断开记
  499/`client_disconnect`、超时记 504/`timeout` 并合成响应、流式响应中途
  失败的归因、响应前异常记 500/`exception` 且异常继续上抛；每条异常链路
  只影响本路由，且 gauge 归零。
* **端到端**（`tests/test_app.py`）：24 线程高并发下 counter/histogram
  精确守恒、在途 gauge 在并发期间可被实时观测且结束归零、随机海量路径下
  标签基数不增长且抓取延迟稳定、真实 HTTP 超时、幂等初始化与应用重建隔离、
  关闭指标不输出、`/metrics` 不计自身。

另含一次 100 线程 × 200 请求的独立压力验证，断言 20000 次请求计数精确
等于 20000、在途 gauge 归零、抓取延迟保持毫秒级。
