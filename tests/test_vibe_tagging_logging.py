from pathlib import Path


VIBE_TAGGING = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "pinit"
    / "api"
    / "services"
    / "vibe_tagging.py"
)


def test_vibe_tagging_never_logs_api_key_material() -> None:
    source = VIBE_TAGGING.read_text()

    assert "XAI_API_KEY[:" not in source
    assert "XAI_API_KEY=%s" not in source
