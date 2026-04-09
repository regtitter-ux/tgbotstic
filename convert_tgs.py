#!/usr/bin/env python3
"""
TGS → GIF converter via lottie + cairosvg.

Requires libcairo-2.dll from node-canvas (found at desktop/index project).

Usage: python convert_tgs.py <input.tgs> <width> <height> <output.gif>
"""
import os
import sys

# cairosvg needs libcairo-2.dll — use the one bundled with node-canvas
_CAIRO_DIR = r'C:\Users\pudwe\Desktop\index\node_modules\canvas\build\Release'
if os.path.isdir(_CAIRO_DIR):
    os.environ['PATH'] = _CAIRO_DIR + os.pathsep + os.environ.get('PATH', '')


def main() -> None:
    if len(sys.argv) != 5:
        print("Usage: convert_tgs.py <input.tgs> <w> <h> <output.gif>", file=sys.stderr)
        sys.exit(1)

    input_path  = sys.argv[1]
    w, h        = int(sys.argv[2]), int(sys.argv[3])
    output_path = sys.argv[4]

    from lottie.parsers.tgs import parse_tgs
    from lottie.exporters.gif import export_gif
    import gzip

    with gzip.open(input_path, "rb") as f:
        anim = parse_tgs(f)

    # Calculate DPI so the output matches the requested w×h.
    # cairosvg scales SVG as: output_px = svg_px * dpi / 96
    # Lottie SVG width == anim.width (in lottie px units).
    anim_w = anim.width or 512
    anim_h = anim.height or 512
    dpi_w  = 96 * w / anim_w
    dpi_h  = 96 * h / anim_h
    dpi    = min(dpi_w, dpi_h)   # keep aspect ratio; fill to the smaller dimension

    with open(output_path, "wb") as fp:
        export_gif(anim, fp, dpi=dpi)

    import os as _os
    size_kb = _os.path.getsize(output_path) // 1024
    print(f"OK: {size_kb} KB", file=sys.stderr)


if __name__ == "__main__":
    main()
