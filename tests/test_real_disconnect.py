"""End-to-end disconnect test against a real uvicorn server in a thread.

The in-process ASGI test transport cannot model a peer that closes the socket
mid-request, so this test starts uvicorn on an ephemeral port and drops the
TCP connection while the slow endpoint is still handling the request.
"""
from __future__ import annotations

import socket
import threading
import time

import uvicorn

from conftest import make_app, registry_of
from observability.metrics import HTTP_REQUESTS_TOTAL


def _wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_client_disconnect_on_real_server() -> None:
    app = make_app(request_timeout=None)  # the slow request must NOT timeout first
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # noqa: E731 (running in a worker thread)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: server.started), "uvicorn failed to start"
        listener = server.servers[0].sockets[0]
        port = listener.getsockname()[1]

        # Sanity check: ordinary requests still work through the real stack.
        with socket.create_connection(("127.0.0.1", port), timeout=2) as ok_sock:
            ok_sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = ok_sock.recv(4096)
        assert b"200 OK" in data
        print("input: baseline GET / over real TCP -> 200 OK")

        # The request under test: send the request line and immediately abort
        # the connection while /slow is sleeping.
        with socket.create_connection(("127.0.0.1", port), timeout=2) as gone_sock:
            gone_sock.sendall(b"GET /slow?wait=5 HTTP/1.1\r\nHost: x\r\n\r\n")
            time.sleep(0.2)  # let the endpoint start handling
        print("input: GET /slow?wait=5 then TCP connection abruptly closed after 0.2s")

        reg = registry_of(app)

        def disconnect_recorded() -> bool:
            return reg.counter_value(
                HTTP_REQUESTS_TOTAL,
                (("method", "GET"), ("route", "/slow"), ("status", "none"),
                 ("outcome", "client_disconnect")),
            ) == 1.0

        assert _wait_for(disconnect_recorded), "client_disconnect outcome was never recorded"
        print("judgement: http_requests_total{method=GET,route=/slow,status=none,"
              "outcome=client_disconnect} == 1")
    finally:
        server.should_exit = True
        thread.join(timeout=5)
