"""Static infrastructure checks for independent shard persistence."""

from pathlib import Path


def test_compose_defines_two_apps_databases_and_persistent_volumes() -> None:
    compose = Path("infra/standalone/compose.yaml").read_text(encoding="utf-8")

    for service in ("bank-a", "bank-b", "postgres-a", "postgres-b"):
        assert f"  {service}:" in compose
    assert "postgres-a-data:/var/lib/postgresql/data" in compose
    assert "postgres-b-data:/var/lib/postgresql/data" in compose
    assert "postgres-a-data:" in compose
    assert "postgres-b-data:" in compose
    assert "5432:5432" not in compose
    assert "@postgres-a:5432/" in compose
    assert "@postgres-b:5432/" in compose
