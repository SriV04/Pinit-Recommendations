import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from pinit.integrations.supabase import SupabaseService


class _Rpc:
    def __init__(self, data=True):
        self.data = data

    def execute(self):
        return SimpleNamespace(data=self.data)


class _Client:
    def __init__(self):
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _Rpc()


def _service():
    service = object.__new__(SupabaseService)
    service.client = _Client()
    return service


def test_claim_location_processing_serializes_cooldown_and_lease() -> None:
    service = _service()

    assert service.claim_location_processing(
        42,
        "request-42",
        cooldown_seconds=2_592_000,
        claim_stale_after_seconds=300,
    )

    assert service.client.calls == [
        (
            "claim_location_processing",
            {
                "p_location_id": 42,
                "p_request_id": "request-42",
                "p_cooldown_seconds": 2_592_000,
                "p_claim_stale_after_seconds": 300,
            },
        )
    ]


def test_complete_and_release_use_owned_request_id() -> None:
    service = _service()

    assert service.complete_location_processing_queue(42, "request-42")
    assert service.release_location_processing_claim(42, "request-42")

    assert service.client.calls == [
        (
            "complete_location_processing_queue",
            {"p_location_id": 42, "p_request_id": "request-42"},
        ),
        (
            "release_location_processing_claim",
            {"p_location_id": 42, "p_request_id": "request-42"},
        ),
    ]
