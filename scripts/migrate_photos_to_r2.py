"""
Copy photos from Supabase Storage to Cloudflare R2.

  locations  location_photos/{id}.ext, {id}_{n}.ext
             -> l/{id}/{n}_{thumb|card|hero}.webp + l/{id}/{n}_orig.ext   (via r2_photos)
  avatars    profile_photos/{path}      -> u/{path}   (copied as-is; resized by the CDN)
  covers     collection_covers/{path}   -> c/{path}   (copied as-is; resized by the CDN)

Idempotent and resumable: objects whose R2 keys already exist are skipped, so it
is safe to stop and re-run. Reads only from Supabase; writes only to R2.

Usage (from the repo root):
    python scripts/migrate_photos_to_r2.py --dry-run                 # counts + cost estimate, no writes
    python scripts/migrate_photos_to_r2.py --only locations --limit 50
    python scripts/migrate_photos_to_r2.py --workers 8               # everything
    python scripts/migrate_photos_to_r2.py --verify                  # compare R2 against image_stored flags
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import requests

from pinit.config import secrets
from pinit.integrations import r2_photos

LOCATION_BUCKET = "location_photos"
COPY_BUCKETS = {"avatars": ("profile_photos", "u/"), "covers": ("collection_covers", "c/")}
LOCATION_NAME = re.compile(r"^(\d+)(?:_(\d+))?\.[A-Za-z0-9]+$")

# Cloudflare R2 list prices and monthly free tier (check the pricing page before relying on these).
R2_STORAGE_USD_GB_MONTH = 0.015
R2_FREE_GB = 10.0
R2_CLASS_A_USD_PER_M = 4.50
R2_FREE_CLASS_A = 1_000_000
# Mean thumb+card+hero WebP bytes per photo, measured on a 25-photo sample of production.
MEASURED_VARIANT_BYTES_PER_PHOTO = (19.2 + 46.9 + 91.6) * 1024

COPY_CACHE_CONTROL = "public, max-age=86400"


def parse_location_object_name(name: str) -> Optional[Tuple[int, int]]:
    """``123.jpg`` -> (123, 0); ``123_4.png`` -> (123, 4); anything else -> None."""
    match = LOCATION_NAME.match(name)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def location_keys(location_id: int, index: int, content_type: Optional[str]) -> List[str]:
    keys = [r2_photos.variant_key(location_id, index, s) for s in r2_photos.PHOTO_VARIANTS]
    keys.append(r2_photos.original_key(location_id, index, content_type))
    return keys


@dataclass
class Plan:
    todo: List[Dict[str, Any]] = field(default_factory=list)
    already_done: int = 0
    unparseable: List[str] = field(default_factory=list)
    todo_bytes: int = 0


def plan_locations(
    objects: List[Dict[str, Any]], existing: Set[str], only_ids: Optional[Set[int]] = None
) -> Plan:
    """Plan location photo copies. ``only_ids`` restricts to those location ids (extras included)."""
    plan = Plan()
    done_originals: Set[Tuple[int, int]] = set()
    for key in existing:
        m = re.match(r"^l/(\d+)/(\d+)_orig\.", key)
        if m:
            done_originals.add((int(m.group(1)), int(m.group(2))))
    for obj in objects:
        parsed = parse_location_object_name(obj["name"])
        if parsed is None:
            plan.unparseable.append(obj["name"])
            continue
        location_id, index = parsed
        if only_ids is not None and location_id not in only_ids:
            continue
        variants_ok = all(
            r2_photos.variant_key(location_id, index, s) in existing for s in r2_photos.PHOTO_VARIANTS
        )
        if variants_ok and (location_id, index) in done_originals:
            plan.already_done += 1
            continue
        plan.todo.append({**obj, "location_id": location_id, "index": index})
        plan.todo_bytes += int((obj.get("metadata") or {}).get("size") or 0)
    return plan


def plan_copies(objects: List[Dict[str, Any]], prefix: str, existing: Set[str]) -> Plan:
    plan = Plan()
    for obj in objects:
        key = prefix + obj["name"]
        if key in existing:
            plan.already_done += 1
        else:
            plan.todo.append({**obj, "key": key})
            plan.todo_bytes += int((obj.get("metadata") or {}).get("size") or 0)
    return plan


def estimate_cost(
    location_photos: int, copy_objects: int, source_bytes: int, existing_r2_bytes: int = 0, list_pages: int = 0
) -> Dict[str, float]:
    """Predicted one-off requests and the resulting monthly R2 bill."""
    class_a = location_photos * (len(r2_photos.PHOTO_VARIANTS) + 1) + copy_objects + list_pages
    stored_bytes = (
        existing_r2_bytes
        + source_bytes  # originals / copies
        + location_photos * MEASURED_VARIANT_BYTES_PER_PHOTO
    )
    stored_gb = stored_bytes / 1024**3
    billable_class_a = max(0, class_a - R2_FREE_CLASS_A)
    return {
        "class_a_requests": class_a,
        "class_a_cost_usd": billable_class_a / 1_000_000 * R2_CLASS_A_USD_PER_M,
        "stored_gb": stored_gb,
        "storage_usd_month": max(0.0, stored_gb - R2_FREE_GB) * R2_STORAGE_USD_GB_MONTH,
        "supabase_egress_gb": source_bytes / 1024**3,
    }


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def list_bucket(client: Any, bucket: str, path: str = "") -> List[Dict[str, Any]]:
    """Recursively list a Supabase bucket. Folders are entries without an id."""
    out: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = client.storage.from_(bucket).list(
            path,
            {"limit": 1000, "offset": offset, "sortBy": {"column": "name", "order": "asc"}},
        )
        if not page:
            break
        for entry in page:
            full = f"{path}/{entry['name']}" if path else entry["name"]
            if entry.get("id") is None:
                out.extend(list_bucket(client, bucket, full))
            else:
                out.append({**entry, "name": full})
        if len(page) < 1000:
            break
        offset += 1000
    return out


def fetch_referenced_location_ids(client: Any) -> Set[int]:
    """Distinct location ids that appear in user_location_actions."""
    ids: Set[int] = set()
    start = 0
    while True:
        page = (
            client.table("user_location_actions")
            .select("location_id")
            .order("action_id")
            .range(start, start + 999)
            .execute()
            .data
            or []
        )
        ids.update(int(r["location_id"]) for r in page if r.get("location_id") is not None)
        if len(page) < 1000:
            return ids
        start += 1000


def list_r2_keys(prefix: str) -> Tuple[Set[str], int, int]:
    """Existing R2 keys under ``prefix`` plus their total bytes and the list pages used."""
    client = r2_photos._client()
    keys: Set[str] = set()
    total = 0
    pages = 0
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=secrets.R2_BUCKET_NAME, Prefix=prefix
    ):
        pages += 1
        for item in page.get("Contents", []):
            keys.add(item["Key"])
            total += item["Size"]
    return keys, total, pages


def download(bucket: str, name: str) -> Tuple[bytes, str]:
    url = f"{secrets.SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{bucket}/{quote(name)}"
    last: Optional[Exception] = None
    for attempt in range(4):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.content, response.headers.get("Content-Type", "image/jpeg")
        except Exception as exc:  # noqa: BLE001 - retried then re-raised
            last = exc
            time.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"download failed for {bucket}/{name}: {last}")


def migrate_location(item: Dict[str, Any]) -> int:
    body, content_type = download(LOCATION_BUCKET, item["name"])
    meta_type = (item.get("metadata") or {}).get("mimetype")
    r2_photos.upload_location_photo(
        item["location_id"], body, meta_type or content_type, item["index"]
    )
    return len(body)


def migrate_copy(bucket: str, item: Dict[str, Any]) -> int:
    body, content_type = download(bucket, item["name"])
    r2_photos._client().put_object(
        Bucket=secrets.R2_BUCKET_NAME,
        Key=item["key"],
        Body=body,
        ContentType=(item.get("metadata") or {}).get("mimetype") or content_type,
        CacheControl=COPY_CACHE_CONTROL,
    )
    return len(body)


def run_pool(label: str, items: List[Dict[str, Any]], fn, workers: int, failures: List[Dict[str, Any]]) -> int:
    done = 0
    moved = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                moved += future.result()
            except Exception as exc:  # noqa: BLE001 - recorded, run continues
                failures.append({"name": item["name"], "error": str(exc)[:300]})
            done += 1
            if done % 100 == 0 or done == len(items):
                rate = done / max(time.time() - started, 0.001)
                print(f"  {label}: {done}/{len(items)} ({rate:.1f}/s, {len(failures)} failed)", flush=True)
    return moved


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify(client: Any) -> int:
    """Every image_stored location must have its primary variants (and extras) in R2."""
    existing, _, _ = list_r2_keys("l/")
    rows: List[Dict[str, Any]] = []
    start = 0
    while True:
        page = (
            client.table("locations")
            .select("location_id, extra_photos_stored")
            .eq("image_stored", True)
            .order("location_id")
            .range(start, start + 999)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < 1000:
            break
        start += 1000
    missing_primary: List[int] = []
    missing_extras = 0
    for row in rows:
        lid = row["location_id"]
        if not all(r2_photos.variant_key(lid, 0, s) in existing for s in r2_photos.PHOTO_VARIANTS):
            missing_primary.append(lid)
        for i in range(1, int(row.get("extra_photos_stored") or 0) + 1):
            if r2_photos.variant_key(lid, i, "card") not in existing:
                missing_extras += 1
    print(f"verify: {len(rows)} image_stored locations")
    print(f"  missing primary variants : {len(missing_primary)} {missing_primary[:10]}")
    print(f"  missing extra photos     : {missing_extras}")
    return 0 if not missing_primary and not missing_extras else 1


# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Copy Supabase Storage photos to Cloudflare R2.")
    parser.add_argument("--only", choices=["locations", "avatars", "covers"], action="append",
                        help="Limit to these sets (repeatable). Default: all.")
    parser.add_argument("--dry-run", action="store_true", help="Plan and estimate cost; write nothing.")
    parser.add_argument("--limit", type=int, help="Process at most N objects per set (for trial runs).")
    parser.add_argument("--referenced-only", action="store_true",
                        help="Locations only: just photos of locations that appear in user_location_actions.")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--verify", action="store_true", help="Check R2 against image_stored flags and exit.")
    parser.add_argument("--failures-file", default="photo_migration_failures.json")
    args = parser.parse_args(argv)

    if not r2_photos.is_configured():
        print("R2 is not configured (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME).")
        return 2

    from pinit.integrations.supabase import get_supabase_service

    client = get_supabase_service().client
    if args.verify:
        return verify(client)

    sets = args.only or ["locations", "avatars", "covers"]
    failures: List[Dict[str, Any]] = []
    totals = {"photos": 0, "copies": 0, "bytes": 0, "r2_bytes": 0, "pages": 0}

    plans: Dict[str, Plan] = {}
    existing_l, l_bytes, l_pages = (set(), 0, 0)
    if "locations" in sets:
        existing_l, l_bytes, l_pages = list_r2_keys("l/")
        objects = list_bucket(client, LOCATION_BUCKET)
        print(f"location_photos: {len(objects)} objects in Supabase, {len(existing_l)} keys already in R2")
        only_ids = None
        if args.referenced_only:
            only_ids = fetch_referenced_location_ids(client)
            print(f"--referenced-only: {len(only_ids)} distinct locations in user_location_actions")
        plans["locations"] = plan_locations(objects, existing_l, only_ids)
        totals["r2_bytes"] += l_bytes
        totals["pages"] += l_pages
    for name in ("avatars", "covers"):
        if name in sets:
            bucket, prefix = COPY_BUCKETS[name]
            existing, nbytes, pages = list_r2_keys(prefix)
            objects = list_bucket(client, bucket)
            print(f"{bucket}: {len(objects)} objects in Supabase, {len(existing)} already in R2")
            plans[name] = plan_copies(objects, prefix, existing)
            totals["r2_bytes"] += nbytes
            totals["pages"] += pages

    for name, plan in plans.items():
        if args.limit:
            plan.todo = plan.todo[: args.limit]
            plan.todo_bytes = sum(int((o.get("metadata") or {}).get("size") or 0) for o in plan.todo)
        print(f"[{name}] to migrate: {len(plan.todo)} | already done: {plan.already_done} | "
              f"unparseable: {len(plan.unparseable)} | source bytes: {plan.todo_bytes / 1024**2:.0f} MB")
        if plan.unparseable:
            print(f"   unparseable names (skipped): {plan.unparseable[:10]}")
        if name == "locations":
            totals["photos"] += len(plan.todo)
        else:
            totals["copies"] += len(plan.todo)
        totals["bytes"] += plan.todo_bytes

    est = estimate_cost(totals["photos"], totals["copies"], totals["bytes"], totals["r2_bytes"], totals["pages"])
    print("\nPredicted cost of this run")
    print(f"  Cloudflare R2 Class A requests : {est['class_a_requests']:,.0f} (free tier {R2_FREE_CLASS_A:,}) -> ${est['class_a_cost_usd']:.2f}")
    print(f"  R2 stored after run            : {est['stored_gb']:.1f} GB -> ${est['storage_usd_month']:.2f}/month")
    print(f"  Supabase egress for downloads  : {est['supabase_egress_gb']:.1f} GB")
    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0

    for name, plan in plans.items():
        if not plan.todo:
            continue
        print(f"\nMigrating {name}...")
        if name == "locations":
            run_pool(name, plan.todo, migrate_location, args.workers, failures)
        else:
            bucket = COPY_BUCKETS[name][0]
            run_pool(name, plan.todo, lambda item, b=bucket: migrate_copy(b, item), args.workers, failures)

    if failures:
        with open(args.failures_file, "w") as handle:
            json.dump(failures, handle, indent=2)
        print(f"\n{len(failures)} failures written to {args.failures_file}; re-run to retry them.")
        return 1
    print("\nDone. Re-run with --verify to compare R2 against the image_stored flags.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
