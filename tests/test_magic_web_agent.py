import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests import install_optional_dependency_stubs

install_optional_dependency_stubs()

from pinit.api.services.magic_web_agent import (
    MagicWebAgentSuggestion,
    _build_vibe_summary,
    _normalise_web_agent_suggestion,
    build_magic_web_agent_prompt,
)
from pinit.api.services.magic_google_service import resolve_magic_web_agent_suggestions


class MagicWebAgentTests(unittest.TestCase):
    def test_build_vibe_summary_keeps_top_positive_dimensions(self) -> None:
        order = {"romantic": 0, "trendy": 1, "quiet": 2, "cafe": 3}
        summary = _build_vibe_summary([91, 77, 10, 65], order, limit=3)

        self.assertEqual(
            summary,
            {"romantic": 0.91, "trendy": 0.77, "cafe": 0.65},
        )

    def test_prompt_requires_resolution_query_and_does_not_request_google_place_ids(self) -> None:
        prompt = build_magic_web_agent_prompt(
            user_prompt="hot new restaurants",
            lat=51.4941,
            lng=-0.1738,
            radius_km=2.0,
            vibe_summary={},
            max_candidates=8,
        )

        self.assertIn("place_resolution_query", prompt)
        self.assertIn("Do not invent Google Place IDs", prompt)
        self.assertNotIn("include google_place_id", prompt)

    def test_normalise_web_agent_suggestion_clamps_confidence(self) -> None:
        suggestion = MagicWebAgentSuggestion(
            name="  Test Place  ",
            place_resolution_query=" Test Place Soho London ",
            address_hint="Soho",
            neighbourhood_hint="Soho",
            reason="Mentioned as a hot date-night spot",
            confidence=2.0,
            source_claims=["ai_hotspot", "social_buzz", "unknown"],
            citations=[{"url": "https://example.com", "title": "List"}],
        )

        normalised = _normalise_web_agent_suggestion(suggestion)

        self.assertEqual(normalised["name"], "Test Place")
        self.assertEqual(normalised["place_resolution_query"], "Test Place Soho London")
        self.assertEqual(normalised["neighbourhood_hint"], "Soho")
        self.assertNotIn("google_place_id", normalised)
        self.assertEqual(normalised["confidence"], 1.0)
        self.assertEqual(normalised["source_claims"], ["ai_hotspot", "social_buzz"])

    def test_xai_web_search_response_requires_place_resolution_query(self) -> None:
        from pinit.api.services import magic_web_agent

        class _Response:
            output_text = (
                '{"suggestions":[{"name":"Agent Place",'
                '"place_resolution_query":"Agent Place South Kensington London",'
                '"address_hint":"South Kensington","neighbourhood_hint":"South Kensington",'
                '"reason":"Recent mention","confidence":0.8,'
                '"source_claims":["ai_hotspot"],"citations":[]}]}'
            )

        fake_client = AsyncMock()
        fake_client.responses.create = AsyncMock(return_value=_Response())

        with patch.dict("os.environ", {"XAI_API_KEY": ""}), \
             patch.object(magic_web_agent, "XAI_API_KEY", "xai-test-key"), \
             patch.object(magic_web_agent, "AsyncOpenAI", return_value=fake_client):
            result = asyncio_run(
                magic_web_agent.fetch_magic_web_agent_suggestions(
                    prompt="hot restaurants nearby",
                    lat=51.4941,
                    lng=-0.1738,
                    radius_km=2.0,
                    user_profile={"vibe_vector": []},
                    vibe_tag_order={},
                    cache=None,
                )
            )

        create_kwargs = fake_client.responses.create.call_args.kwargs
        self.assertEqual(create_kwargs["tools"], [{"type": "web_search"}])
        self.assertNotIn("google_place_id", create_kwargs["input"])
        self.assertEqual(
            result.suggestions[0]["place_resolution_query"],
            "Agent Place South Kensington London",
        )
        self.assertNotIn("google_place_id", result.suggestions[0])

    def test_provider_logs_prompt_and_raw_response(self) -> None:
        from pinit.api.services import magic_web_agent

        class _Response:
            output_text = (
                '{"suggestions":[{"name":"Agent Place",'
                '"place_resolution_query":"Agent Place South Kensington London",'
                '"address_hint":"South Kensington","neighbourhood_hint":null,'
                '"reason":"Recent mention","confidence":0.8,'
                '"source_claims":["ai_hotspot"],"citations":[]}]}'
            )

        fake_client = AsyncMock()
        fake_client.responses.create = AsyncMock(return_value=_Response())

        with patch.dict("os.environ", {"XAI_API_KEY": ""}), \
             patch.object(magic_web_agent, "XAI_API_KEY", "xai-test-key"), \
             patch.object(magic_web_agent, "AsyncOpenAI", return_value=fake_client), \
             self.assertLogs("pinit.api.services.magic_web_agent", level="INFO") as logs:
            result = asyncio_run(
                magic_web_agent.fetch_magic_web_agent_suggestions(
                    prompt="hot restaurants nearby",
                    lat=51.4941,
                    lng=-0.1738,
                    radius_km=2.0,
                    user_profile={"vibe_vector": []},
                    vibe_tag_order={},
                    cache=None,
                )
            )

        self.assertEqual(len(result.suggestions), 1)
        log_text = "\n".join(logs.output)
        self.assertIn("Grok magic agent prompt", log_text)
        self.assertIn("hot restaurants nearby", log_text)
        self.assertIn("lat=51.49410", log_text)
        self.assertIn("Grok magic agent raw response", log_text)
        self.assertIn("Agent Place", log_text)

    def test_default_timeout_can_be_configured_for_xai_web_search_call(self) -> None:
        from pinit.api.services import magic_web_agent

        async def slow_create(**kwargs):
            import asyncio

            await asyncio.sleep(0.05)
            class _Response:
                output_text = '{"suggestions":[]}'

            return _Response()

        fake_client = AsyncMock()
        fake_client.responses.create = AsyncMock(side_effect=slow_create)

        with patch.dict(
            "os.environ",
            {
                "XAI_API_KEY": "",
                "XAI_MAGIC_SEARCH_TIMEOUT_SECONDS": "0.01",
            },
        ), \
             patch.object(magic_web_agent, "XAI_API_KEY", "xai-test-key"), \
             patch.object(magic_web_agent, "AsyncOpenAI", return_value=fake_client):
            result = asyncio_run(
                magic_web_agent.fetch_magic_web_agent_suggestions(
                    prompt="hot restaurants nearby",
                    lat=51.4941,
                    lng=-0.1738,
                    radius_km=2.0,
                    user_profile={"vibe_vector": []},
                    vibe_tag_order={},
                    cache=None,
                )
            )

        self.assertTrue(result.timed_out)


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


class _FakeGoogleResponse:
    def __init__(self, places: list[dict]) -> None:
        self._places = places

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"places": self._places}


class _FakeGoogleClient:
    def __init__(self, batches: list[list[dict]]) -> None:
        self.batches = list(batches)
        self.calls: list[dict] = []

    async def post(self, url: str, *, json: dict, headers: dict, timeout: float):
        self.calls.append(json)
        return _FakeGoogleResponse(self.batches.pop(0))


class MagicWebAgentResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolver_uses_place_resolution_query_not_google_place_id_from_xai(self) -> None:
        suggestions = [
            {
                "name": "Test Place",
                "place_resolution_query": "Test Place South Kensington London",
                "address_hint": "South Kensington",
                "neighbourhood_hint": "South Kensington",
                "reason": "Recent list mention",
                "confidence": 0.8,
                "source_claims": ["ai_hotspot"],
                "citations": [{"url": "https://example.com", "title": "List"}],
            }
        ]
        client = _FakeGoogleClient(
            [
                [
                    {
                        "id": "gid-1",
                        "displayName": {"text": "Test Place"},
                        "types": ["restaurant"],
                        "location": {"latitude": 51.494, "longitude": -0.174},
                    }
                ]
            ]
        )

        places = await resolve_magic_web_agent_suggestions(
            suggestions,
            known_google_place_ids=set(),
            lat=51.4941,
            lng=-0.1738,
            radius_km=2.0,
            api_key="test-key",
            client=client,
        )

        self.assertEqual(places[0]["id"], "gid-1")
        self.assertEqual(
            client.calls[0]["textQuery"],
            "Test Place South Kensington London",
        )
        self.assertEqual(
            places[0]["web_agent"]["place_resolution_query"],
            "Test Place South Kensington London",
        )

    async def test_resolver_skips_known_place_ids_and_searches_only_misses(self) -> None:
        suggestions = [
            {
                "name": "Known Place",
                "google_place_id": "known-gid",
                "address_hint": "Soho",
                "reason": "Already in Supabase",
                "confidence": 0.9,
                "source_claims": ["ai_hotspot"],
                "citations": [],
            },
            {
                "name": "Test Place",
                "place_resolution_query": "Test Place Soho",
                "address_hint": "Soho",
                "neighbourhood_hint": "Soho",
                "reason": "Recent list mention",
                "confidence": 0.8,
                "source_claims": ["ai_hotspot"],
                "citations": [{"url": "https://example.com", "title": "List"}],
            },
        ]
        client = _FakeGoogleClient(
            [
                [
                    {
                        "id": "gid-1",
                        "displayName": {"text": "Test Place"},
                        "types": ["restaurant"],
                        "location": {"latitude": 51.51, "longitude": -0.13},
                    }
                ]
            ]
        )

        places = await resolve_magic_web_agent_suggestions(
            suggestions,
            known_google_place_ids={"known-gid"},
            lat=50.8225,
            lng=-0.1372,
            radius_km=1.5,
            api_key="test-key",
            client=client,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(places[0]["id"], "gid-1")
        self.assertEqual(places[0]["web_agent"]["confidence"], 0.8)
        self.assertIn("Test Place Soho", client.calls[0]["textQuery"])
        self.assertEqual(
            client.calls[0]["locationBias"],
            {
                "circle": {
                    "center": {"latitude": 50.8225, "longitude": -0.1372},
                    "radius": 1500.0,
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
