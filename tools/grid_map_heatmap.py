#!/usr/bin/env python3
"""
grid_map_heatmap.py — geo-grid heatmap composited on a real Mapbox basemap.

Same rank data as grid_heatmap.py, but instead of circles on blank paper the pins
sit on an actual street map (Mapbox Static Images API, streets-v12) so you can see
*which* town is dark and which is covered. Pins are drawn by us (solid, white-bordered,
rank number inside — green = you rank, brick-red = absent) because Mapbox/Google
server-side markers only allow a single-character label — they can't show a two-digit rank.

Licensing: the Mapbox Static Images API is licensed for static/embedded images
(unlike Google Static Maps, whose terms forbid storing/redistributing the imagery).
The returned PNG carries Mapbox's baked logo + "© Mapbox © OpenStreetMap"; we keep
it and add a text credit — do not crop either.

Auth: set MAPBOX_TOKEN in .env (a public token with the styles:tiles scope).

Usage:
    python tools/grid_map_heatmap.py --place-id ChIJ... --scan-id 4 \
        --title "Local Search Coverage — Nashville" \
        --out output/nashville-grid-map.svg
    # keyword-specific instead of the all-keyword aggregate:
    python tools/grid_map_heatmap.py --place-id ChIJ... --scan-id 5 --keyword "climbing gym nashville"
"""

from __future__ import annotations

import argparse
import base64
import math
import sys
from pathlib import Path
from typing import Optional, Sequence

DB_PATH = Path("data/leads.sqlite")
MAPBOX_STATIC = "https://api.mapbox.com/styles/v1/{style}/static/{lng},{lat},{zoom}/{w}x{h}@2x"
DEFAULT_STYLE = (
    "mapbox/streets-v12"  # a real, recognizable street map (light-v11 read as a gray smudge)
)

from lib.common import load_env, slug  # noqa: E402
import grid_heatmap as gh  # reuse tier colours + helpers  # noqa: E402

# Map-specific pin palette: the traditional green→red coverage ramp. Green = you rank
# (bright = 3-pack, fading as rank deepens), brick-red = not found — so a low-coverage grid
# reads as a problem at a glance. White borders + a drop shadow lift the pins off the map.
BRICK = "#B4462F"
GREEN = "#1F9C52"  # 1–3 (3-pack)
GREEN_MED = "#5FB98A"  # 4–10
GREEN_PALE = "#B7D9C4"  # 11–20
MAP_TIER_COLOR = {"top3": GREEN, "page1": GREEN_MED, "deep": GREEN_PALE, "absent": BRICK}
MAP_TIER_TEXT = {
    "top3": gh.PAPER,
    "page1": gh.STEEL_DARK,
    "deep": gh.STEEL_DARK,
    "absent": gh.PAPER,
}

# --------------------------------------------------------------------------- #
# Web Mercator — pure, testable (no network). Matches Mapbox/Google projection. #
# Work in "zoom-0 world pixels" (world = 256 px); multiply by 2**zoom to scale. #
# --------------------------------------------------------------------------- #
_WORLD = 256.0


def world_xy(lat: float, lng: float) -> tuple[float, float]:
    x = (lng + 180.0) / 360.0 * _WORLD
    siny = math.sin(math.radians(lat))
    siny = min(max(siny, -0.9999), 0.9999)
    y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * _WORLD
    return x, y


def _unproject_lat(y0: float) -> float:
    n = math.pi - 2.0 * math.pi * y0 / _WORLD
    return math.degrees(math.atan(math.sinh(n)))


def fit_view(
    pins: Sequence[dict], w: int, h: int, pad: int
) -> tuple[float, float, float, float, float]:
    """Centre + fractional zoom that fit every pin inside (w-2pad, h-2pad).
    Returns (center_lat, center_lng, zoom, cx0, cy0) where cx0/cy0 are the zoom-0
    world-pixel centre used later to place pins consistently with the fetched image."""
    xs = [world_xy(p["lat"], p["lng"]) for p in pins]
    x0 = [a for a, _ in xs]
    y0 = [b for _, b in xs]
    minx, maxx, miny, maxy = min(x0), max(x0), min(y0), max(y0)
    cx0, cy0 = (minx + maxx) / 2, (miny + maxy) / 2
    spanx, spany = max(maxx - minx, 1e-9), max(maxy - miny, 1e-9)
    zoom = min(math.log2((w - 2 * pad) / spanx), math.log2((h - 2 * pad) / spany))
    zoom = max(0.0, min(zoom, 20.0))
    return _unproject_lat(cy0), cx0 / _WORLD * 360.0 - 180.0, zoom, cx0, cy0


def pixel_of(lat: float, lng: float, cx0: float, cy0: float, zoom: float, w: int, h: int):
    x0, y0 = world_xy(lat, lng)
    scale = 2.0**zoom
    return w / 2.0 + (x0 - cx0) * scale, h / 2.0 + (y0 - cy0) * scale


# --------------------------------------------------------------------------- #
# Data                                                                          #
# --------------------------------------------------------------------------- #
def pins_from_points(points: Sequence[dict], keyword: Optional[str] = None) -> list[dict]:
    """One pin per (row,col) keeping lat/lng; rank = mean over keywords (or one kw)."""
    acc: dict = {}
    for p in points:
        if keyword and p["keyword"] != keyword:
            continue
        k = (p["row"], p["col"])
        d = acc.setdefault(k, {"lat": p["point_lat"], "lng": p["point_lng"], "ranks": []})
        d["ranks"].append(p["rank"])
    out = []
    for (r, c), d in acc.items():
        found = [x for x in d["ranks"] if x is not None]
        out.append(
            {
                "row": r,
                "col": c,
                "lat": d["lat"],
                "lng": d["lng"],
                "rank": (sum(found) / len(found)) if found else None,
            }
        )
    return out


def fetch_basemap(
    lat: float, lng: float, zoom: float, w: int, h: int, style: str, token: str
) -> bytes:
    import requests

    url = MAPBOX_STATIC.format(
        style=style, lng=f"{lng:.6f}", lat=f"{lat:.6f}", zoom=f"{zoom:.4f}", w=w, h=h
    )
    # Keep Mapbox's baked logo + attribution (do not pass logo=false/attribution=false).
    r = requests.get(url, params={"access_token": token}, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"Mapbox static request failed [{r.status_code}]: {r.text[:200]}")
    return r.content


# --------------------------------------------------------------------------- #
# Render                                                                         #
# --------------------------------------------------------------------------- #
_FONT = gh._FONT
_HEAD = 66
_FOOT = 54


def _biz_marker(bx: float, by: float, w: int, label: str) -> list[str]:
    """A distinct star pin + label pill for the business's own GBP location."""
    # keep the label pill on-canvas
    pill_w = 20 + gh._approx_text_w(label, 12)
    lx = min(max(bx - pill_w / 2, 6), w - pill_w - 6)
    ly = by - 40
    return [
        f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="13" fill="#FFFFFF" fill-opacity="0.95" '
        f'stroke="{gh.STEEL_DARK}" stroke-width="1.5" filter="url(#pinsh)"/>',
        f'<text x="{bx:.1f}" y="{by + 0.5:.1f}" fill="{gh.STEEL_DARK}" font-family="{_FONT}" '
        f'font-size="20" font-weight="700" text-anchor="middle" dominant-baseline="central">★</text>',
        f'<rect x="{lx:.1f}" y="{ly:.1f}" width="{pill_w}" height="22" rx="5" fill="{gh.STEEL_DARK}" filter="url(#pinsh)"/>',
        f'<text x="{lx + pill_w / 2:.1f}" y="{ly + 11:.1f}" fill="{gh.PAPER}" font-family="{_FONT}" '
        f'font-size="12" font-weight="700" text-anchor="middle" dominant-baseline="central">{gh._esc(label)}</text>',
    ]


def render_map_svg(
    png: bytes,
    pins: Sequence[dict],
    *,
    rows: int,
    cols: int,
    w: int,
    h: int,
    pad: int,
    cx0: float,
    cy0: float,
    zoom: float,
    title: str,
    subtitle: str,
    solv: Optional[float],
    biz: Optional[tuple[float, float]] = None,
    biz_label: str = "★ Your Google listing",
) -> str:
    b64 = base64.b64encode(png).decode("ascii")
    cw, ch = w, _HEAD + h + _FOOT
    r_pin = max(
        11.0,
        min(15.0, 0.30 * min((w - 2 * pad) / max(cols - 1, 1), (h - 2 * pad) / max(rows - 1, 1))),
    )
    fs = round(r_pin * 0.85)

    parts = [
        '<defs><filter id="pinsh" x="-60%" y="-60%" width="220%" height="220%">'
        '<feDropShadow dx="0" dy="1.3" stdDeviation="1.5" flood-color="#000000" flood-opacity="0.5"/>'
        "</filter></defs>",
        f'<rect width="{cw}" height="{ch}" fill="{gh.PAPER}"/>',
        f'<text x="16" y="30" fill="{gh.STEEL_DARK}" font-family="{_FONT}" font-size="20" '
        f'font-weight="800">{gh._esc(title)}</text>',
        f'<rect x="16" y="40" width="52" height="4" fill="{gh.AMBER}"/>',
        f'<text x="16" y="58" fill="{gh.STEEL_GREY}" font-family="{_FONT}" font-size="12">{gh._esc(subtitle)}</text>',
    ]
    if solv is not None:
        parts.append(
            f'<text x="{w - 16}" y="57" fill="{gh.AMBER_DEEP}" font-family="{_FONT}" font-size="15" '
            f'font-weight="800" text-anchor="end">SoLV {solv:.1f}</text>'
        )
    # basemap + thin frame
    parts.append(
        f'<image x="0" y="{_HEAD}" width="{w}" height="{h}" href="data:image/png;base64,{b64}"/>'
    )
    parts.append(
        f'<rect x="0.5" y="{_HEAD + 0.5}" width="{w - 1}" height="{h - 1}" fill="none" '
        f'stroke="{gh.STEEL}" stroke-opacity="0.3" stroke-width="1"/>'
    )
    # rank pins — solid, white-bordered, drop-shadowed so they sit ON the map
    for p in pins:
        px, py = pixel_of(p["lat"], p["lng"], cx0, cy0, zoom, w, h)
        py += _HEAD
        tier = gh.rank_tier(p["rank"])
        label = "–" if p["rank"] is None or tier == "absent" else str(int(round(p["rank"])))
        parts.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r_pin:.1f}" fill="{MAP_TIER_COLOR[tier]}" '
            f'stroke="#FFFFFF" stroke-width="2.2" filter="url(#pinsh)"/>'
        )
        parts.append(
            f'<text x="{px:.1f}" y="{py:.1f}" fill="{MAP_TIER_TEXT[tier]}" font-family="{_FONT}" '
            f'font-size="{fs}" font-weight="700" text-anchor="middle" dominant-baseline="central">{label}</text>'
        )
    # optional "your Google listing" marker (drawn last, on top)
    if biz is not None:
        bx, by = pixel_of(biz[0], biz[1], cx0, cy0, zoom, w, h)
        if 0 <= bx <= w and 0 <= by <= h:
            parts.extend(_biz_marker(bx, by + _HEAD, w, biz_label))
    # legend + attribution footer
    ly = _HEAD + h + 24
    lx = 16
    for tier in ("top3", "page1", "deep", "absent"):
        parts.append(
            f'<circle cx="{lx}" cy="{ly}" r="8" fill="{MAP_TIER_COLOR[tier]}" stroke="#FFFFFF" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{lx + 14}" y="{ly}" fill="{gh.STEEL}" font-family="{_FONT}" font-size="12" '
            f'dominant-baseline="central">{gh.TIER_LABEL[tier]}</text>'
        )
        lx += 20 + gh._approx_text_w(gh.TIER_LABEL[tier], 12) + 26
    parts.append(
        f'<text x="16" y="{_HEAD + h + 46}" fill="{gh.STEEL_GREY}" font-family="{_FONT}" '
        f'font-size="10">Basemap © Mapbox © OpenStreetMap</text>'
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{cw}" height="{ch}" viewBox="0 0 {cw} {ch}">\n'
        + "\n".join(parts)
        + "\n</svg>\n"
    )


# --------------------------------------------------------------------------- #
# CLI                                                                           #
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Geo-grid heatmap on a Mapbox basemap.")
    ap.add_argument("--place-id", required=True)
    ap.add_argument(
        "--scan-id", type=int, help="Exact grid_scans.id (else latest non-baseline, then latest)."
    )
    ap.add_argument("--keyword", help="Single keyword; omit for the all-keyword aggregate.")
    ap.add_argument(
        "--style", default=DEFAULT_STYLE, help=f"Mapbox style (default {DEFAULT_STYLE})."
    )
    ap.add_argument("--title", default="Local Search Coverage")
    ap.add_argument(
        "--size", default="760x700", help="Logical WxH of the map (max 1280 each; @2x fetched)."
    )
    ap.add_argument(
        "--pad", type=int, default=48, help="Inner padding (px) so edge pins aren't on the border."
    )
    ap.add_argument(
        "--biz", help="lat,lng of the business's own GBP pin to mark (e.g. 33.285,-111.862)."
    )
    ap.add_argument(
        "--biz-label", default="★ Your Google listing", help="Label for the --biz marker."
    )
    ap.add_argument(
        "--out", type=Path, help="Write SVG here (default output/<place>-grid-map.svg)."
    )
    args = ap.parse_args(argv)

    load_env()
    import os

    token = os.environ.get("MAPBOX_TOKEN")
    if not token:
        raise SystemExit(
            "MAPBOX_TOKEN not set in .env (need a public token with the styles:tiles scope)."
        )

    w, h = (int(v) for v in args.size.lower().split("x"))
    if not (1 <= w <= 1280 and 1 <= h <= 1280):
        raise SystemExit("--size dimensions must each be between 1 and 1280.")

    import sqlite3

    import leads_db_grid as gdb

    conn = sqlite3.connect(DB_PATH)
    scan_id = (
        args.scan_id
        or gdb.latest_scan_id(conn, args.place_id, exclude_baseline=True)
        or gdb.latest_scan_id(conn, args.place_id)
    )
    if scan_id is None:
        raise SystemExit(f"No completed scan for {args.place_id}.")
    scan = gdb.get_scan(conn, scan_id)
    if scan is None:
        raise SystemExit(f"No grid_scans row with id={scan_id}.")

    points = gdb.get_grid_points(conn, scan_id)
    pins = pins_from_points(points, args.keyword)
    if not pins:
        raise SystemExit("No pins for that scan/keyword.")

    biz = None
    if args.biz:
        blat, blng = (float(v) for v in args.biz.split(","))
        biz = (blat, blng)

    center_lat, center_lng, zoom, cx0, cy0 = fit_view(pins, w, h, args.pad)
    png = fetch_basemap(center_lat, center_lng, zoom, w, h, args.style, token)
    sub = f"Keyword: {args.keyword}" if args.keyword else "All keywords (aggregate)"
    svg = render_map_svg(
        png,
        pins,
        rows=int(scan["grid_rows"]),
        cols=int(scan["grid_cols"]),
        w=w,
        h=h,
        pad=args.pad,
        cx0=cx0,
        cy0=cy0,
        zoom=zoom,
        title=args.title,
        subtitle=sub,
        solv=scan.get("solv"),
        biz=biz,
        biz_label=args.biz_label,
    )
    out = args.out or Path("output") / f"{slug(args.place_id)}-grid-map.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg, encoding="utf-8")
    print(
        f"[map svg -> {out}]  center=({center_lat:.4f},{center_lng:.4f}) zoom={zoom:.2f} pins={len(pins)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
