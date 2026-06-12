#!/usr/bin/env python3
"""
toolcut — turn a photo of a tool into a true-scale outline (SVG + DXF)
for cutting foam inserts (Milwaukee PACKOUT, tool drawers, Kaizen foam, etc.)

Workflow the engine assumes:
  1. Lay the tool on a sheet of printer paper (US Letter or A4) on a
     contrasting surface. The paper does double duty: it sets the scale
     AND gives a clean white background for segmentation.
  2. Shoot roughly top-down. The 4 paper corners give us a homography to
     rectify perspective and lock real-world scale (mm per pixel).
  3. We segment the tool, extract its outline, add a clearance offset
     (so it drops in/out of foam easily), optionally add finger scoops,
     and export SVG + DXF at true 1:1 scale.

Run `python toolcut.py --selftest` to validate the pipeline with a
synthetic known-size object (no input photo needed).
"""

import argparse
import math
import sys

import cv2
import numpy as np
from shapely.geometry import Polygon, Point
from shapely.ops import unary_union
import ezdxf

# Standard paper sizes in millimeters (width, height as placed = portrait)
PAPER = {
    "letter": (215.9, 279.4),
    "a4": (210.0, 297.0),
}


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------
def order_corners(pts):
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    pts = np.asarray(pts, dtype=np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([
        pts[np.argmin(s)],   # tl  (smallest x+y)
        pts[np.argmin(d)],   # tr  (smallest y-x)
        pts[np.argmax(s)],   # br  (largest  x+y)
        pts[np.argmax(d)],   # bl  (largest  y-x)
    ], dtype=np.float32)


def detect_paper(img):
    """Find the largest bright 4-corner quad (the sheet of paper)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)))
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        raise RuntimeError("No paper found — check lighting/contrast.")
    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)
    for c in cnts[:5]:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and cv2.contourArea(approx) > 0.10 * img.shape[0] * img.shape[1]:
            return order_corners(approx.reshape(4, 2))
    raise RuntimeError("Couldn't isolate a 4-corner paper quad. "
                       "Try --manual-corners or a cleaner background.")


def rectify(img, corners, paper_wh_mm, px_per_mm):
    """Warp so the paper fills a top-down image at a known mm scale."""
    w_mm, h_mm = paper_wh_mm
    W, H = int(round(w_mm * px_per_mm)), int(round(h_mm * px_per_mm))
    dst = np.array([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]],
                   dtype=np.float32)
    M = cv2.getPerspectiveTransform(corners, dst)
    return cv2.warpPerspective(img, M, (W, H))


def segment_tool(warped, px_per_mm, border_mm=6.0):
    """Binary mask of the tool sitting on the (white) paper."""
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    # Tool is darker than paper -> invert so tool = foreground (white)
    _, mask = cv2.threshold(gray, 0, 255,
                            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Kill a border ring so we never grab paper-edge artifacts
    b = int(round(border_mm * px_per_mm))
    mask[:b, :] = 0; mask[-b:, :] = 0; mask[:, :b] = 0; mask[:, -b:] = 0
    # Clean up speckle / close gaps
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    return mask


def extract_polygon(mask, px_per_mm, simplify_mm):
    """Largest contour -> shapely Polygon in millimeters."""
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        raise RuntimeError("No tool contour found inside the paper.")
    c = max(cnts, key=cv2.contourArea)
    eps = max(simplify_mm * px_per_mm, 1.0)
    c = cv2.approxPolyDP(c, eps, True).reshape(-1, 2).astype(float)
    pts_mm = c / px_per_mm                      # px -> mm
    poly = Polygon(pts_mm)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly


def postprocess(poly, clearance_mm, finger_holes, finger_dia_mm):
    """Outward clearance offset + optional finger scoops, unioned in."""
    if clearance_mm:
        poly = poly.buffer(clearance_mm, join_style=1)  # round joins
    if finger_holes:
        minx, miny, maxx, maxy = poly.bounds
        cx = (minx + maxx) / 2
        r = finger_dia_mm / 2
        inset = min((maxy - miny) * 0.18, 25)
        scoops = [Point(cx, miny + inset).buffer(r),
                  Point(cx, maxy - inset).buffer(r)]
        poly = unary_union([poly] + scoops)
    return poly


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def _rings(poly):
    geoms = poly.geoms if poly.geom_type == "MultiPolygon" else [poly]
    out = []
    for g in geoms:
        out.append(list(g.exterior.coords))
        for hole in g.interiors:
            out.append(list(hole.coords))
    return out


def export_svg(poly, path, margin=10.0):
    minx, miny, maxx, maxy = poly.bounds
    W, H = (maxx - minx) + 2 * margin, (maxy - miny) + 2 * margin
    paths = []
    for ring in _rings(poly):
        d = "M " + " L ".join(f"{x - minx + margin:.3f},{y - miny + margin:.3f}"
                              for x, y in ring) + " Z"
        paths.append(f'<path d="{d}" fill="none" stroke="black" '
                     f'stroke-width="0.3"/>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" '
           f'width="{W:.2f}mm" height="{H:.2f}mm" '
           f'viewBox="0 0 {W:.3f} {H:.3f}">\n' + "\n".join(paths) + "\n</svg>\n")
    with open(path, "w") as f:
        f.write(svg)


def export_dxf(poly, path):
    doc = ezdxf.new()
    msp = doc.modelspace()
    for ring in _rings(poly):
        msp.add_lwpolyline([(x, y) for x, y in ring], close=True)
    doc.units = ezdxf.units.MM
    doc.saveas(path)


def write_debug(warped, poly, px_per_mm, path):
    img = warped.copy()
    for ring in _rings(poly):
        pts = (np.array(ring) * px_per_mm).astype(np.int32)
        cv2.polylines(img, [pts], True, (0, 90, 255), 3)
    cv2.imwrite(path, img)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------
def run(img, paper, px_per_mm, clearance_mm, simplify_mm,
        finger_holes, finger_dia_mm, manual_corners=None, debug_path=None):
    corners = (order_corners(manual_corners) if manual_corners is not None
               else detect_paper(img))
    warped = rectify(img, corners, PAPER[paper], px_per_mm)
    mask = segment_tool(warped, px_per_mm)
    poly = extract_polygon(mask, px_per_mm, simplify_mm)
    raw_bounds = poly.bounds
    poly = postprocess(poly, clearance_mm, finger_holes, finger_dia_mm)
    if debug_path:
        write_debug(warped, poly, px_per_mm, debug_path)
    return poly, raw_bounds


# --------------------------------------------------------------------------
# Self-test: synthesize a known-size object, warp it, recover it, verify scale
# --------------------------------------------------------------------------
def selftest():
    px = 6.0  # render scale for the synthetic scene
    paper = "letter"
    w_mm, h_mm = PAPER[paper]
    pw, ph = int(w_mm * px), int(h_mm * px)
    mx, my = int(pw * 0.22), int(ph * 0.16)          # gray margin around paper
    W, H = pw + 2 * mx, ph + 2 * my

    scene = np.full((H, W, 3), 90, np.uint8)          # gray table
    cv2.rectangle(scene, (mx, my), (mx + pw, my + ph),
                  (255, 255, 255), -1)                 # white paper, inset

    # Known "tool": 50 x 180 mm bar with a 60 mm round head -> draw in black
    cx = mx + pw // 2
    bar_w, bar_h, head_d = 50 * px, 180 * px, 60 * px
    y0 = my + int(ph * 0.22)
    cv2.rectangle(scene, (int(cx - bar_w / 2), y0),
                  (int(cx + bar_w / 2), int(y0 + bar_h)), (20, 20, 20), -1)
    cv2.circle(scene, (cx, int(y0 + bar_h)), int(head_d / 2), (20, 20, 20), -1)

    # Simulate a phone angle: warp the whole scene with a perspective skew
    src = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    dst = np.float32([[W * 0.04, H * 0.02], [W * 0.99, H * 0.06],
                      [W * 0.94, H * 0.99], [W * 0.01, H * 0.93]])
    Mwarp = cv2.getPerspectiveTransform(src, dst)
    photo = cv2.warpPerspective(scene, Mwarp, (W, H), borderValue=(90, 90, 90))

    poly, (minx, miny, maxx, maxy) = run(
        photo, paper, px_per_mm=8.0, clearance_mm=0.0,
        simplify_mm=0.8, finger_holes=False, finger_dia_mm=35,
        debug_path="/home/claude/selftest_debug.png")

    meas_w, meas_h = maxx - minx, maxy - miny
    exp_w, exp_h = 60.0, 180.0 + 30.0  # head sets width 60; bar+half-head height
    print(f"  expected  ~{exp_w:.0f} x {exp_h:.0f} mm")
    print(f"  measured   {meas_w:.1f} x {meas_h:.1f} mm")
    print(f"  error      {abs(meas_w-exp_w)/exp_w*100:.1f}% W, "
          f"{abs(meas_h-exp_h)/exp_h*100:.1f}% H")
    ok = abs(meas_w - exp_w) < 4 and abs(meas_h - exp_h) < 6
    export_svg(poly, "/home/claude/selftest.svg")
    export_dxf(poly, "/home/claude/selftest.dxf")
    print("  exported selftest.svg / selftest.dxf / selftest_debug.png")
    print("  RESULT:", "PASS ✅" if ok else "FAIL ❌")
    return ok


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Photo -> foam-cutout outline (SVG/DXF)")
    ap.add_argument("image", nargs="?", help="input photo")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--paper", choices=PAPER, default="letter")
    ap.add_argument("--px-per-mm", type=float, default=8.0)
    ap.add_argument("--clearance-mm", type=float, default=1.5,
                    help="outward offset so the tool drops in/out easily")
    ap.add_argument("--simplify-mm", type=float, default=0.6)
    ap.add_argument("--finger-holes", action="store_true")
    ap.add_argument("--finger-dia-mm", type=float, default=35.0)
    ap.add_argument("--out-prefix", default="cutout")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)
    if not args.image:
        ap.error("provide an image or use --selftest")

    img = cv2.imread(args.image)
    if img is None:
        ap.error(f"could not read {args.image}")

    poly, raw = run(img, args.paper, args.px_per_mm, args.clearance_mm,
                    args.simplify_mm, args.finger_holes, args.finger_dia_mm,
                    debug_path=f"{args.out_prefix}_debug.png" if args.debug else None)
    export_svg(poly, f"{args.out_prefix}.svg")
    export_dxf(poly, f"{args.out_prefix}.dxf")
    minx, miny, maxx, maxy = poly.bounds
    print(f"tool ~{raw[2]-raw[0]:.1f} x {raw[3]-raw[1]:.1f} mm; "
          f"cutout {maxx-minx:.1f} x {maxy-miny:.1f} mm "
          f"(+{args.clearance_mm} mm clearance)")
    print(f"wrote {args.out_prefix}.svg and {args.out_prefix}.dxf")


if __name__ == "__main__":
    main()
