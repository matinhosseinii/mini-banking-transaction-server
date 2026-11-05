"""Static infrastructure checks for independent shard persistence."""

from pathlib import Path


def test_compose_defines_four_replicas_gateway_and_persistent_databases() -> None:
    compose = Path("infra/standalone/compose.yaml").read_text(encoding="utf-8")

    for service in (
        "bank-a1",
        "bank-a2",
        "bank-b1",
        "bank-b2",
        "gateway",
        "postgres-a",
        "postgres-b",
    ):
        assert f"  {service}:" in compose
    assert "postgres-a-data:/var/lib/postgresql/data" in compose
    assert "postgres-b-data:/var/lib/postgresql/data" in compose
    assert "postgres-a-data:" in compose
    assert "postgres-b-data:" in compose
    assert "5432:5432" not in compose
    assert "@postgres-a:5432/" in compose
    assert "@postgres-b:5432/" in compose
    assert compose.count("SHARD_ID: A") == 2
    assert compose.count("SHARD_ID: B") == 2
    assert '"${API_BIND_IP:?API_BIND_IP is required}:8000:8000"' in compose
