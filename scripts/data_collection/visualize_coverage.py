#!/usr/bin/env python3
"""
Visualize Stage-A discovery coverage on an interactive London map.

Reads the state file and renders:
  🟢 Green circles  – completed / searched grid cells
  🔴 Red circles    – queued / not-yet-searched grid cells

Optionally overlays London borough boundaries if the GeoJSON is available.

Usage:
    python visualize_coverage.py                        # defaults
    python visualize_coverage.py --state path/to/state.json --out coverage.html
"""

import argparse
import json
import os
import sys

import folium

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE = os.path.join(SCRIPT_DIR, "data", "raw", "stage_a_state.json")
DEFAULT_OUT = os.path.join(SCRIPT_DIR, "data", "raw", "coverage_map.html")
BOROUGHS_GEOJSON = os.path.join(
    SCRIPT_DIR, "..", "..", "data", "raw", "london_boroughs.geojson"
)


def parse_completed_keys(keys: list[str]) -> list[dict]:
    """Convert 'lat|lng|radius' strings into dicts."""
    results = []
    for key in keys:
        parts = key.split("|")
        results.append(
            {"lat": float(parts[0]), "lng": float(parts[1]), "radius_m": float(parts[2])}
        )
    return results


def build_map(state: dict, boroughs_path: str | None = None) -> folium.Map:
    completed = parse_completed_keys(state.get("completed_keys", []))
    queued = state.get("queue", [])

    # ---- stats ----
    n_done = len(completed)
    n_queue = len(queued)
    n_total = n_done + n_queue
    pct = (n_done / n_total * 100) if n_total else 0

    print(f"Completed tiles : {n_done}")
    print(f"Queued tiles    : {n_queue}")
    print(f"Total tiles     : {n_total}")
    print(f"Coverage        : {pct:.1f}%")
    print(f"Places found    : {state.get('discovered_place_ids', '?')}")
    print(f"API calls used  : {state.get('api_calls', '?')}")

    # ---- centre on London ----
    m = folium.Map(location=[51.5074, -0.1278], zoom_start=11, tiles="CartoDB positron")

    # ---- borough boundaries (optional) ----
    if boroughs_path and os.path.isfile(boroughs_path):
        folium.GeoJson(
            boroughs_path,
            name="London Boroughs",
            style_function=lambda _: {
                "fillColor": "transparent",
                "color": "#555",
                "weight": 1.5,
                "dashArray": "4 3",
            },
        ).add_to(m)

    # ---- queued (red) – draw first so completed layer is on top ----
    queued_group = folium.FeatureGroup(name=f"Queued – not searched ({n_queue})")
    for pt in queued:
        folium.Circle(
            location=[pt["lat"], pt["lng"]],
            radius=pt["radius_m"],
            color="#e74c3c",
            fill=True,
            fill_color="#e74c3c",
            fill_opacity=0.18,
            weight=0.6,
            popup=f"Queued  r={pt['radius_m']:.0f}m",
        ).add_to(queued_group)
    queued_group.add_to(m)

    # ---- completed (green) ----
    done_group = folium.FeatureGroup(name=f"Completed – searched ({n_done})")
    for pt in completed:
        folium.Circle(
            location=[pt["lat"], pt["lng"]],
            radius=pt["radius_m"],
            color="#27ae60",
            fill=True,
            fill_color="#27ae60",
            fill_opacity=0.25,
            weight=0.6,
            popup=f"Done  r={pt['radius_m']:.0f}m",
        ).add_to(done_group)
    done_group.add_to(m)

    # ---- legend / title ----
    legend_html = f"""
    <div style="position:fixed; top:12px; left:60px; z-index:9999;
                background:white; padding:12px 16px; border-radius:8px;
                box-shadow:0 2px 6px rgba(0,0,0,.3); font-family:sans-serif;
                font-size:13px; line-height:1.6;">
      <b>Stage A – Discovery Coverage</b><br>
      <span style="color:#27ae60;">⬤</span> Searched ({n_done} tiles)<br>
      <span style="color:#e74c3c;">⬤</span> Not yet searched ({n_queue} tiles)<br>
      <hr style="margin:4px 0;">
      Coverage: <b>{pct:.1f}%</b> &nbsp;|&nbsp;
      Places: <b>{state.get('discovered_place_ids', '?')}</b> &nbsp;|&nbsp;
      API calls: <b>{state.get('api_calls', '?')}</b>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    # ---- layer control ----
    folium.LayerControl(collapsed=False).add_to(m)

    return m


def main():
    parser = argparse.ArgumentParser(description="Visualize Stage-A coverage")
    parser.add_argument("--state", default=DEFAULT_STATE, help="Path to stage_a_state.json")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output HTML file")
    args = parser.parse_args()

    if not os.path.isfile(args.state):
        sys.exit(f"State file not found: {args.state}")

    with open(args.state) as f:
        state = json.load(f)

    fmap = build_map(state, boroughs_path=BOROUGHS_GEOJSON)
    fmap.save(args.out)
    print(f"\n✅  Map saved to {args.out}")
    print(f"    Open in browser: file://{os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
