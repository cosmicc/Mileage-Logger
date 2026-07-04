import re
from pathlib import Path

COMPOSE_FILE = Path("docker-compose.yml")
STACK_FILE = Path("docker-stack.yml")
STACK_LOCAL_POSTGRES_FILE = Path("docker-stack.local-postgres.yml")
DOCKER_ENV_FILE = Path(".env.docker.example")


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


def test_app_container_exposes_pushover_health_settings() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    stack_text = STACK_FILE.read_text(encoding="utf-8")
    env_text = DOCKER_ENV_FILE.read_text(encoding="utf-8")
    compose_app_block = _service_block(compose_text, "app")
    stack_app_block = _service_block(stack_text, "app")

    for app_block in (compose_app_block, stack_app_block):
        assert 'PUSHOVER_ENABLED: "${PUSHOVER_ENABLED:-false}"' in app_block
        assert 'PUSHOVER_TOKEN: "${PUSHOVER_TOKEN:-}"' in app_block
        assert 'PUSHOVER_USER: "${PUSHOVER_USER:-}"' in app_block
        assert 'PUSHOVER_APP_KEY: "${PUSHOVER_APP_KEY:-}"' in app_block
        assert 'PUSHOVER_USER_KEY: "${PUSHOVER_USER_KEY:-}"' in app_block
        assert (
            'APP_HEALTH_MONITOR_INTERVAL_SECONDS: '
            '"${APP_HEALTH_MONITOR_INTERVAL_SECONDS:-60}"'
            in app_block
        )
        assert (
            'APP_HEALTH_STATE_PATH: "${APP_HEALTH_STATE_PATH:-'
            '/data/logs/app-health-state.json}"'
            in app_block
        )

    assert "PUSHOVER_ENABLED=false" in env_text
    assert "PUSHOVER_TOKEN=" in env_text
    assert "PUSHOVER_USER=" in env_text
    assert "APP_HEALTH_DB_LATENCY_WARNING_MS=500" in env_text
    assert "APP_HEALTH_DISK_CRITICAL_PERCENT=95.0" in env_text


def test_bundled_postgres_is_default_optional_compose_profile() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    env_text = DOCKER_ENV_FILE.read_text(encoding="utf-8")
    postgres_block = _service_block(compose_text, "postgres")
    app_block = _service_block(compose_text, "app")

    assert 'profiles: ["local-postgres"]' in postgres_block
    assert "COMPOSE_PROFILES=local-postgres" in env_text
    assert (
        'DATABASE_URL: "${DATABASE_URL:-postgresql+psycopg://'
        'mileage:mileage@postgres:5432/mileage_logger}"'
        in app_block
    )
    assert "depends_on" not in app_block


def test_owntracks_buffer_is_enabled_and_persisted_for_limp_mode() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    app_block = _service_block(compose_text, "app")
    expected_buffer_path = (
        'OWNTRACKS_BUFFER_PATH: "${OWNTRACKS_BUFFER_PATH:-'
        '/data/owntracks-buffer/owntracks-buffer.sqlite3}"'
    )
    expected_fallback_path = (
        'OWNTRACKS_BUFFER_FALLBACK_PATH: "${OWNTRACKS_BUFFER_FALLBACK_PATH:-'
        '/data/owntracks-buffer-fallback/owntracks-buffer.sqlite3}"'
    )
    expected_replay_interval = (
        'OWNTRACKS_BUFFER_REPLAY_INTERVAL_SECONDS: '
        '"${OWNTRACKS_BUFFER_REPLAY_INTERVAL_SECONDS:-15}"'
    )
    expected_buffer_mount = (
        "${HOST_OWNTRACKS_BUFFER_DIR:-/var/lib/mileage-logger/owntracks-buffer}:"
        "/data/owntracks-buffer"
    )

    assert 'OWNTRACKS_BUFFER_ENABLED: "${OWNTRACKS_BUFFER_ENABLED:-true}"' in app_block
    assert expected_buffer_path in app_block
    assert expected_fallback_path in app_block
    assert expected_replay_interval in app_block
    assert (
        'OWNTRACKS_BUFFER_REPLAY_BATCH_SIZE: "${OWNTRACKS_BUFFER_REPLAY_BATCH_SIZE:-100}"'
        in app_block
    )
    assert 'start_period: "${APP_HEALTHCHECK_START_PERIOD:-90s}"' in app_block
    assert expected_buffer_mount in app_block
    assert "owntracks_buffer_fallback:/data/owntracks-buffer-fallback" in app_block
    assert re.search(r"^  owntracks_buffer_fallback:\n", compose_text, re.MULTILINE)


def test_swarm_stack_avoids_compose_only_features() -> None:
    stack_text = STACK_FILE.read_text(encoding="utf-8")
    app_block = _service_block(stack_text, "app")
    nginx_block = _service_block(stack_text, "nginx")
    cloudflared_block = _service_block(stack_text, "cloudflared")

    assert "\n    build:" not in stack_text
    assert "\n    profiles:" not in stack_text
    assert "\n    depends_on:" not in stack_text
    assert "\n    network_mode:" not in stack_text
    assert "\n    restart:" not in stack_text
    assert 'image: "${APP_IMAGE:-mileage-logger-app:latest}"' in app_block
    assert 'image: "${NGINX_IMAGE:-mileage-logger-nginx:latest}"' in nginx_block
    assert "ports:" not in nginx_block
    assert "TUNNEL_TOKEN:" in cloudflared_block
    assert 'start_period: "${APP_HEALTHCHECK_START_PERIOD:-90s}"' in app_block
    assert "restart_policy:" in stack_text


def test_swarm_stack_has_optional_local_postgres_overlay() -> None:
    stack_text = STACK_FILE.read_text(encoding="utf-8")
    local_postgres_text = STACK_LOCAL_POSTGRES_FILE.read_text(encoding="utf-8")
    env_text = DOCKER_ENV_FILE.read_text(encoding="utf-8")

    assert "\n  postgres:" not in stack_text
    assert "\n  postgres:" in local_postgres_text
    assert "postgres_data:/var/lib/postgresql/data" in local_postgres_text
    assert "APP_IMAGE=mileage-logger-app:latest" in env_text
    assert "NGINX_IMAGE=mileage-logger-nginx:latest" in env_text
