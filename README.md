# fastapi-service-observability-python

在 FastAPI 服务上内置的一套并发安全、标签有界的 HTTP 可观测性采集与
Prometheus 文本暴露链路。

## 快速开始

```bash
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
curl -s localhost:8000/items/1
curl -s localhost:8000/metrics
```

启用请求处理超时（超时计入 `status="504",outcome="timeout"`）：

```bash
REQUEST_TIMEOUT_SECONDS=2 uv run uvicorn app.main:app --port 8000
```

## 测试

```bash
uv run pytest
```

用例的“输入 + 判定依据”会写入 `test-observability.log`。

## 文档

指标语义、标签约定（含异常/断开/超时的区分）、基数有界性、并发安全与
验证方法见 [docs/observability.md](docs/observability.md)。
