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
