# FastAPI 服务可观测性：指标采集与暴露

本服务在 FastAPI 应用上内置了一条并发安全的 HTTP 指标采集链路，并以
Prometheus 文本格式（text exposition format 0.0.4）对外暴露。

* 所有外部 HTTP 请求统一采集：计数、耗时分布、在途请求数。
* 按**路由模板**归类（`/items/{item_id}`），未知路由收敛为固定标签
  `__unmatched__`，标签组合有界。
* 正常响应、5xx 响应、未处理异常、处理超时、客户端提前断开可通过
  `outcome` 标签稳定区分。
* 指标语义与标签约定详见 [`docs/observability.md`](docs/observability.md)。

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

| 路由 | 用途 | 预期 `outcome` |
| --- | --- | --- |
| `GET /` | 正常响应 | `ok` |
| `GET /items/{item_id}` | 验证路径参数归一（任意 id 共用同一序列） | `ok` |
| `GET /slow?wait=10` | 长耗时，超过请求超时（默认 5s，工厂演示设为较短值） | `timeout` |
| `GET /boom` | 处理函数抛出未捕获异常 | `exception`（状态 500） |
| `GET /http-error` | 显式返回 503 | `error` |
| `GET /metrics` | 指标抓取端点 | `ok` |

## 测试

```bash
uv run pytest -q            # 全部单测（含并发守恒、异常/超时/断开区分、路由归一）
uv run pytest -s            # 查看每个用例打印的输入与判定依据
```

关键用例：

* `tests/test_registry.py`：线程级并发计数守恒（不丢不重）、禁用语义、文本格式。
* `tests/test_middleware.py`：路由归一、各 `outcome` 区分、高并发下
  `计数器总和 == 直方图 count 总和`、在途数归零、幂等装配、指标关闭。
* `tests/test_real_disconnect.py`：启动真实 uvicorn，请求处理中途直接关闭
  TCP 连接，验证被计入 `client_disconnect` 且状态为 `none`。

## 在自己的应用中接入

```python
from fastapi import FastAPI
from observability.setup import setup_observability

app = FastAPI()
setup_observability(app, request_timeout=5.0, duration_enabled=True)
```

`setup_observability` 幂等：对同一应用重复调用不会重复挂载中间件或
`/metrics` 路由；重建应用则得到独立、干净的注册表，不会产生重复序列。
