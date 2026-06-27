import re
from pathlib import Path

NGINX_CONF = Path("deploy/nginx/default.conf")


def _location_block(config: str, location: str) -> str:
    match = re.search(
        rf"location\s+{re.escape(location)}\s+\{{(?P<body>.*?)\n    \}}",
        config,
        flags=re.DOTALL,
    )
    assert match is not None, f"missing nginx location {location}"
    return match.group("body")


def test_public_nginx_only_proxies_owntracks_api_endpoints() -> None:
    config = NGINX_CONF.read_text(encoding="utf-8")

    for location in (
        "= /api/owntracks",
        "= /api/owntracks/",
        "= /api/pub",
        "= /api/pub/",
    ):
        block = _location_block(config, location)
        assert "proxy_pass http://mileage_logger_app;" in block
        assert "limit_except POST" in block

    assert "location /api/ {\n        return 404;\n    }" in config
    assert "location = /api {\n        return 404;\n    }" in config
    assert "location = /openapi.json {\n        return 404;\n    }" in config
    assert "location ^~ /docs {\n        return 404;\n    }" in config
    assert "location ^~ /redoc {\n        return 404;\n    }" in config


def test_nginx_keeps_web_routes_available_behind_web_access_rules() -> None:
    config = NGINX_CONF.read_text(encoding="utf-8")

    for location in ("= /api/health", "= /api/locations"):
        assert location not in config

    static_block = _location_block(config, "/static/")
    web_block = _location_block(config, "/")
    assert "include /etc/nginx/includes/web-access.conf;" in static_block
    assert "include /etc/nginx/includes/web-access.conf;" in web_block
    assert "proxy_pass http://mileage_logger_app;" in static_block
    assert "proxy_pass http://mileage_logger_app;" in web_block


def test_nginx_does_not_forward_spoofable_client_ip_headers() -> None:
    config = NGINX_CONF.read_text(encoding="utf-8")

    assert "map $remote_addr $trusted_cf_connecting_ip" in config
    assert 'default "";' in config
    assert "127.0.0.1 $http_cf_connecting_ip;" in config
    assert "::1 $http_cf_connecting_ip;" in config

    for location in (
        "= /api/owntracks",
        "= /api/owntracks/",
        "= /api/pub",
        "= /api/pub/",
        "/static/",
        "/",
    ):
        block = _location_block(config, location)
        assert "proxy_set_header X-Real-IP $remote_addr;" in block
        assert "proxy_set_header X-Forwarded-For $remote_addr;" in block
        assert "proxy_set_header CF-Connecting-IP $trusted_cf_connecting_ip;" in block

    assert "$proxy_add_x_forwarded_for" not in config
