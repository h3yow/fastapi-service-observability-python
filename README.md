# FastAPI 服务可观测性：指标采集与暴露

本服务在 FastAPI 应用上内置了一条并发安全的 HTTP 指标采集链路，并以
Prometheus 文本格式（text exposition format 0.0.4）对外暴露。

* 所有外部 HTTP 请求统一采集：计数、耗时分布、在途请求数。
* 按**路由模板**归类（`/items/{item_id}`），未知路由收敛为固定标签
  `__unmatched__`，非标准方法收敛为 `OTHER`，非标准/越界状态码收敛为
  `other`，标签组合有界。
* 正常成功、客户端出错（4xx）、服务端出错（5xx/非标准码）、未处理异常、
  处理超时、客户端提前断开通过 `result` 六分类标签可直接下钻配看板，
  另有传输层 `outcome` 五分类保持稳定。
* **容量防护**：每个指标族的序列数有可配置上限（默认 500，可设 `None`
  不设限），到顶后新组合统一收敛到单个 `__overflow__` 兜底序列，请求不
  报错、不丢弃、总量守恒；`observability_*` 自监控指标在同一次抓取中暴露
  当前序列数、上限、是否已兜底、累计收敛量与渲染耗时，并跟随总开关启停。
* 指标语义、标签约定、配置项与兼容范围详见
  [`docs/observability.md`](docs/observability.md)。

## 运行

```bash
uv run python main.py            # 默认监听 127.0.0.1:8000
# 或
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

## 快速验证

```bash
curl -s http://127.0.0.1:8000/items/1
curl -s http://127.0.0.1:8000/metrics | head
```

示例输出片段：

```text
# HELP http_requests_total Total number of completed HTTP requests.
# TYPE http_requests_total counter
http_requests_total{method="GET",route="/items/{item_id}",status="200",outcome="ok"} 1
```

## 内置演示路由

| 路由 | 用途 | 预期 `outcome` / `result` |
| --- | --- | --- |
| `GET /` | 正常响应 | `ok` / `success` |
| `GET /items/{item_id}` | 验证路径参数归一（任意 id 共用同一序列） | `ok` / `success` |
| `GET /slow?wait=10` | 长耗时，超过请求超时（默认 5s，工厂演示设为较短值） | `timeout` / `timeout` |
| `GET /boom` | 处理函数抛出未捕获异常 | `exception`（状态 500）/ `exception` |
| `GET /http-error` | 显式返回 503 | `error` / `server_error` |
| `GET /metrics` | 指标抓取端点 | `ok` / `success` |

## 测试

```bash
uv run pytest -q            # 全部单测（含容量上限、兜底收敛、自监控、并发守恒）
uv run pytest -s            # 查看每个用例打印的输入与判定依据
```

关键用例：

* `tests/test_labels.py`：方法/状态码归一（非标准动词→`OTHER`、越界码→
  `other`）、`outcome`/`result` 判定优先级。
* `tests/test_registry.py`：线程级并发计数守恒（不丢不重）、禁用语义、文本格式。
* `tests/test_capacity.py`：序列上限不被突破（含 16 线程 × 100 个互异组合
  的高并发校验）、兜底计数正确、总量与直方图守恒、自监控 gauge/counter/
  渲染耗时、`None` 不设限与非法参数报错、总开关隐藏自监控。
* `tests/test_protection.py`：真实流量打满上限后守恒、非标准码收敛、
  `result` 六类相加等于总数、扫描器乱造动词/路径不撑开序列、总开关。
* `tests/test_middleware.py`：路由归一、各 `outcome` 区分、高并发下
  `计数器总和 == 直方图 count 总和`、在途数归零、幂等装配、指标关闭。
* `tests/test_real_disconnect.py`：启动真实 uvicorn，请求处理中途直接关闭
  TCP 连接，验证被计入 `client_disconnect` 且状态为 `none`。

## 在自己的应用中接入

```python
from fastapi import FastAPI
from observability.setup import setup_observability

app = FastAPI()
setup_observability(
    app,
    request_timeout=5.0,     # 请求超时（秒），None 不设超时
    duration_enabled=True,   # 耗时直方图开关
    max_series=500,          # 每族序列上限（含兜底序列）；None 不设限
    enabled=True,            # 指标链路总开关（关闭后 /metrics 为空、自监控也隐藏）
)
```

`setup_observability` 幂等：对同一应用重复调用不会重复挂载中间件或
`/metrics` 路由；重建应用则得到独立、干净的注册表，不会产生重复序列。
各配置项默认值与新旧行为兼容范围见
[`docs/observability.md`](docs/observability.md) 的第 8、10 节。
