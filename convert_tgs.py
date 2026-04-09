#!/usr/bin/env python3
"""
TGS → GIF converter via rlottie-python.

Usage: python convert_tgs.py <input.tgs> <width> <height> <output.gif>
"""
import gzip
import io
import os
import sys


def main() -> None:
    if len(sys.argv) != 5:
        print("Usage: convert_tgs.py <input.tgs> <w> <h> <output.gif>", file=sys.stderr)
        sys.exit(1)

    input_path  = sys.argv[1]
    w, h        = int(sys.argv[2]), int(sys.argv[3])
    output_path = sys.argv[4]

    try:
        import rlottie_python as rl
    except ImportError:
        print("rlottie-python not installed: pip install rlottie-python", file=sys.stderr)
        sys.exit(2)

    from PIL import Image

    # Распаковываем .tgs (gzip JSON) во временный файл
    json_path = input_path + ".json"
    with gzip.open(input_path, "rb") as f_in, open(json_path, "wb") as f_out:
        f_out.write(f_in.read())

    try:
        anim = rl.LottieAnimation.from_file(json_path)
        total_frames: int = anim.lottie_animation_get_totalframe()
        fps: float = anim.lottie_animation_get_framerate()
        duration_ms = max(1, int(1000 / fps))

        frames = []
        for i in range(total_frames):
            buf = anim.lottie_animation_render(i, w, h)
            img = Image.frombytes("RGBA", (w, h), bytes(buf))
            r, g, b, a = img.split()
            # rlottie renders BGRA — swap R↔B
            frames.append(Image.merge("RGBA", (b, g, r, a)))
    finally:
        if os.path.exists(json_path):
            os.remove(json_path)

    if not frames:
        print("No frames rendered", file=sys.stderr)
        sys.exit(3)

    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        loop=0,
        duration=duration_ms,
        optimize=False,
        format="GIF",
    )

    size_kb = os.path.getsize(output_path) // 1024
    print(f"OK: {total_frames} frames, {size_kb} KB", file=sys.stderr)


if __name__ == "__main__":
    main()
