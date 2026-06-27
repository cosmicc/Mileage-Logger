import re
from pathlib import Path

COMPOSE_FILE = Path("docker-compose.yml")


def _service_block(compose_text: str, service_name: str) -> str:
    match = re.search(
        rf"^  {re.escape(service_name)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|^volumes:|\Z)",
        compose_text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert match is not None, f"missing compose service {service_name}"
    return match.group("body")


def test_nginx_host_port_uses_bind_address_and_cloudflared_host_network() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    nginx_block = _service_block(compose_text, "nginx")
    cloudflared_block = _service_block(compose_text, "cloudflared")

    assert '"${BIND_ADDRESS:-0.0.0.0}:${HTTP_PORT:-80}:80"' in nginx_block
    assert "network_mode: host" in cloudflared_block


def test_gas_snapshot_runs_inside_app_container_without_sidecar() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    app_block = _service_block(compose_text, "app")

    assert "\n  gas-snapshot:" not in compose_text
    assert 'GAS_SNAPSHOT_ENABLED: "${GAS_SNAPSHOT_ENABLED:-true}"' in app_block
    assert (
        'GAS_SNAPSHOT_INTERVAL_SECONDS: "${GAS_SNAPSHOT_INTERVAL_SECONDS:-86400}"'
        in app_block
    )
    assert 'GAS_SNAPSHOT_RUN_ON_STARTUP: "${GAS_SNAPSHOT_RUN_ON_STARTUP:-true}"' in app_block


def test_app_container_requires_production_web_login_secrets() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    app_block = _service_block(compose_text, "app")

    assert "SECRET_KEY: \"${SECRET_KEY:?" in app_block
    assert "WEB_LOGIN_USERNAME: \"${WEB_LOGIN_USERNAME:?" in app_block
    assert "WEB_LOGIN_PASSWORD: \"${WEB_LOGIN_PASSWORD:?" in app_block
    assert 'TRUSTED_PROXY_CIDRS: "${TRUSTED_PROXY_CIDRS:-172.16.0.0/12}"' in app_block
