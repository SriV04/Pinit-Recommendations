"""
Validate that the configured Supabase service key is a `service_role` API key.

This is a local/dev sanity check to catch accidental use of the anon key which
causes 403 "new row violates row-level security policy" errors for Storage and
tables protected by RLS.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


def _decode_role(jwt: str) -> str:
    try:
        parts = (jwt or "").strip().split(".")
        if len(parts) < 2:
            return ""
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
        role = payload.get("role")
        return str(role) if role is not None else ""
    except Exception:
        return ""


def main() -> int:
    # Ensure `.env` + Secret Manager loading is applied consistently.
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from pinit.config.secrets import SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY  # noqa: WPS433

    service_role = _decode_role(SUPABASE_SERVICE_KEY)
    anon_role = _decode_role(SUPABASE_ANON_KEY)

    print(f"SUPABASE_SERVICE_KEY role: {service_role or '(unknown)'}")
    print(f"SUPABASE_ANON_KEY role:    {anon_role or '(unknown)'}")
    if service_role != "service_role":
        print(
            "ERROR: SUPABASE_SERVICE_KEY is not a service_role API key. "
            "Set it to the service_role key from Supabase (Settings → API).",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

