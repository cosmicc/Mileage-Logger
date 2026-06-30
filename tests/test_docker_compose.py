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


def test_nginx_host_port_is_loopback_only_for_cloudflared_host_network() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    nginx_block = _service_block(compose_text, "nginx")
    cloudflared_block = _service_block(compose_text, "cloudflared")

    assert '"127.0.0.1:${HTTP_PORT:-80}:80"' in nginx_block
    assert "BIND_ADDRESS" not in compose_text
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
    assert "WEB_API_KEY: \"${WEB_API_KEY:?" in app_block
    assert "OWNTRACKS_USERNAME: \"${OWNTRACKS_USERNAME:?" in app_block
    assert "OWNTRACKS_PASSWORD: \"${OWNTRACKS_PASSWORD:?" in app_block
    assert "OWNTRACKS_ENCRYPTION_KEY: \"${OWNTRACKS_ENCRYPTION_KEY:?" in app_block
    assert 'PASSKEY_RP_NAME: "${PASSKEY_RP_NAME:-Mileage Logger}"' in app_block
    assert 'PASSKEY_RP_ID: "${PASSKEY_RP_ID:-}"' in app_block
    assert 'PASSKEY_ORIGIN: "${PASSKEY_ORIGIN:-}"' in app_block
