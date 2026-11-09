"""Cloud-edge bridge: URL parsing, discovery freshness, CORS SSE."""

from __future__ import annotations

import json
import socket
import threading
import urllib.request

from market.cloud_edge import binary_name, download_url, parse_tunnel_url, record_is_fresh

SAMPLE = (
    "2026-11-09T09:12:03Z INF Thank you for trying Cloudflare Tunnel.\n"
    "2026-11-09T09:12:04Z INF +--------------------------------------------------------------+\n"
    "2026-11-09T09:12:04Z INF |  https://silent-river-abc123.trycloudflare.com               |\n"
    "2026-11-09T09:12:04Z INF +--------------------------------------------------------------+\n"
    "2026-11-09T09:12:04Z INF Registered tunnel connection connIndex=0\n"
)


def test_parse_tunnel_url_from_banner():
    assert parse_tunnel_url(SAMPLE) == "https://silent-river-abc123.trycloudflare.com"


def test_parse_tunnel_url_ignores_plain_urls():
    assert parse_tunnel_url("INF https://api.upstox.com/v2/login") == ""
    assert parse_tunnel_url("") == ""


def test_parse_tunnel_url_strips_trailing_slash():
    assert parse_tunnel_url("see https://a-b-c9.trycloudflare.com/ now") == (
        "https://a-b-c9.trycloudflare.com"
    )


def test_record_fresh():
    assert record_is_fresh({"url": "https://x-y-z.trycloudflare.com", "ts": 1000.0}, now=1100.0)


def test_record_stale_or_inactive():
    url = "https://x-y-z.trycloudflare.com"
    assert not record_is_fresh({"url": url, "ts": 1000.0}, now=1000.0 + 901)
    assert not record_is_fresh({"url": url, "ts": 1000.0, "active": False}, now=1010.0)
    assert not record_is_fresh({"url": "https://example.com", "ts": 1000.0}, now=1010.0)
    assert not record_is_fresh(None)


def test_binary_naming_matches_platform():
    assert binary_name("win32") == "cloudflared.exe"
    assert binary_name("linux") == "cloudflared"
    assert "cloudflared-windows" in download_url("win32")
    assert "cloudflared-linux" in download_url("linux")


def _serve():
    from http.server import ThreadingHTTPServer

    from dashboard.server import Handler

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_api_ping_is_cors_readable():
    server = _serve()
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/ping",
            headers={"Origin": "https://vetukurisuresh.github.io"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert response.headers.get("Access-Control-Allow-Origin") == "*"
            assert json.load(response)["ok"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_tick_stream_allows_cross_origin_event_source():
    server = _serve()
    connection = socket.create_connection((server.server_address[0], server.server_address[1]), timeout=5)
    try:
        connection.sendall(
            b"GET /api/tick-stream?symbol=NIFTY HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Origin: https://vetukurisuresh.github.io\r\n\r\n"
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = connection.recv(4096)
            assert chunk, "server closed before headers"
            head += chunk
        text = head.split(b"\r\n\r\n")[0].decode("latin-1").lower()
        assert "200 ok" in text
        assert "text/event-stream" in text
        assert "access-control-allow-origin: *" in text
        assert "x-accel-buffering: no" in text
        assert "transfer-encoding: chunked" in text
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
