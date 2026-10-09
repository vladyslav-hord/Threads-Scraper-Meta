from threads_parser.traffic import TrafficMonitor, format_bytes


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
