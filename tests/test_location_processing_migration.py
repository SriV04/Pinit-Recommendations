from pathlib import Path


MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"


def _cooldown_sql() -> str:
    matches = sorted(MIGRATIONS.glob("*_add_location_processing_cooldown.sql"))
    assert len(matches) == 1, matches
    return matches[0].read_text().lower()


def test_location_processing_cooldown_migration_is_atomic_and_private() -> None:
    sql = _cooldown_sql()
    assert "location_processing_queued_at" in sql
    assert "location_processing_claim_id" in sql
    assert "location_processing_claimed_at" in sql
    assert "claim_location_processing" in sql
    assert "complete_location_processing_queue" in sql
    assert "release_location_processing_claim" in sql
    assert sql.count("security invoker") == 3
    assert sql.count("revoke all on function") == 3
    assert sql.count("to service_role") == 3
    assert "to anon" not in sql
    assert "to authenticated" not in sql
    assert "2592000" in sql
    assert "300" in sql
