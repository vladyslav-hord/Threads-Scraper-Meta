from threads_parser.traffic import TrafficMonitor, estimate_request_bytes, format_bytes


class Request:
    url = "https://www.threads.com/api/graphql?secret=ignored-in-reports"
    method = "GET"
    headers = {"accept": "application/json"}
    post_data_buffer = None
    resource_type = "xhr"


def test_traffic_buckets_track_request_and_response_bytes_without_query_string() -> None:
    monitor = TrafficMonitor()
    request = Request()

    monitor.record_request("parser_01", request)
    monitor.record_response("parser_01", type("Response", (), {
        "url": request.url,
        "request": request,
        "status": 200,
        "headers": {"content-length": "12"},
    })())

    report = monitor.report()
    assert report["accounts"]["parser_01"]["requests"] == 1
    assert report["accounts"]["parser_01"]["responses"] == 1
    assert report["accounts"]["parser_01"]["response_bytes"] == 12
    assert "secret=ignored-in-reports" not in str(report)
    assert format_bytes(2048) == "2.0 KB"


def test_api_response_records_request_bytes_from_actual_request_metadata() -> None:
    monitor = TrafficMonitor()
    request = Request()
    expected_request_bytes = estimate_request_bytes(request)

    monitor.record_api_response(
        "parser_01",
        request.url,
        206,
        request.method,
        request.headers,
        12,
    )

    bucket = monitor.accounts["parser_01"]
    assert bucket.request_bytes == expected_request_bytes > 0
    assert bucket.request_count == 1
    assert bucket.response_count == 1
    assert bucket.response_bytes == 12
    assert bucket.statuses == {"206": 1}
