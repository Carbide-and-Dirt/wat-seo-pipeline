"""Tests for grid_map_heatmap.py — pure Web Mercator projection, no network, no spend."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import grid_map_heatmap as gm

W, H, PAD = 600, 560, 54


def _grid(center_lat=36.1627, center_lng=-86.7816, step=0.02, n=3):
    """(n x n) lat/lng grid; row 0 = north (higher lat), col 0 = west (lower lng)."""
    mid = (n - 1) / 2
    pins = []
    for r in range(n):
        for c in range(n):
            pins.append(
                {
                    "row": r,
                    "col": c,
                    "lat": center_lat + (mid - r) * step,
                    "lng": center_lng + (c - mid) * step,
                    "rank": None,
                }
            )
    return pins


def _px(pins):
    lat0, lng0, zoom, cx0, cy0 = gm.fit_view(pins, W, H, PAD)
    return {
        (p["row"], p["col"]): gm.pixel_of(p["lat"], p["lng"], cx0, cy0, zoom, W, H) for p in pins
    }, (
        lat0,
        lng0,
        zoom,
    )


def test_single_pin_centers():
    pins = [{"row": 0, "col": 0, "lat": 36.16, "lng": -86.78, "rank": 1}]
    lat0, lng0, zoom, cx0, cy0 = gm.fit_view(pins, W, H, PAD)
    px, py = gm.pixel_of(pins[0]["lat"], pins[0]["lng"], cx0, cy0, zoom, W, H)
    assert abs(px - W / 2) < 1e-6 and abs(py - H / 2) < 1e-6
    assert abs(lat0 - 36.16) < 1e-6 and abs(lng0 + 86.78) < 1e-6


def test_center_pin_at_image_center():
    px, _ = _px(_grid())
    cx, cy = px[(1, 1)]
    assert abs(cx - W / 2) < 1.0 and abs(cy - H / 2) < 1.0


def test_east_west_north_south_orientation():
    px, _ = _px(_grid())
    cx, cy = px[(1, 1)]
    assert px[(1, 2)][0] > cx > px[(1, 0)][0]  # east is right of centre, west is left
    assert px[(0, 1)][1] < cy < px[(2, 1)][1]  # north is above centre, south is below


def test_symmetry_about_center():
    px, _ = _px(_grid())
    cx, cy = px[(1, 1)]
    # east/west equidistant in x; north/south equidistant in y
    assert abs((px[(1, 2)][0] - cx) - (cx - px[(1, 0)][0])) < 1.0
    assert abs((cy - px[(0, 1)][1]) - (px[(2, 1)][1] - cy)) < 1.0


def test_all_pins_within_padding():
    px, _ = _px(_grid(n=7))
    for (r, c), (x, y) in px.items():
        assert PAD - 1 <= x <= W - PAD + 1, (r, c, x)
        assert PAD - 1 <= y <= H - PAD + 1, (r, c, y)


def test_pins_from_points_mean_and_none():
    pts = [
        {"row": 0, "col": 0, "keyword": "a", "point_lat": 36.16, "point_lng": -86.78, "rank": 2},
        {"row": 0, "col": 0, "keyword": "b", "point_lat": 36.16, "point_lng": -86.78, "rank": 4},
        {"row": 0, "col": 1, "keyword": "a", "point_lat": 36.16, "point_lng": -86.68, "rank": None},
    ]
    pins = {(p["row"], p["col"]): p for p in gm.pins_from_points(pts)}
    assert pins[(0, 0)]["rank"] == 3.0  # mean of 2 and 4
    assert pins[(0, 1)]["rank"] is None  # nowhere found
    # keyword filter
    only_a = {(p["row"], p["col"]): p for p in gm.pins_from_points(pts, keyword="a")}
    assert only_a[(0, 0)]["rank"] == 2.0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok ", fn.__name__)
    print(f"\n{len(fns)} tests passed")
