def test_skeleton_imports() -> None:
    from observability import labels, registry, metrics, middleware, endpoint, setup, factory

    assert labels.ROUTE_UNMATCHED
    assert registry.MetricsRegistry
    assert metrics.HTTP_REQUESTS_TOTAL
    assert middleware.MetricsASGIMiddleware
    assert endpoint.build_metrics_router
    assert setup.setup_observability
    assert factory.create_app
