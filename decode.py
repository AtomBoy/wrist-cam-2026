"""
Offline decoder for WQV-1 raw image structs (.raw files saved by wqv_transfer.py).
Try different pixel layout interpretations without re-running the transfer.

Usage:
    uv run python decode.py images/<file>.raw
    uv run python decode.py images/<file>.raw --variant all
"""

import sys
import argparse
from pathlib import Path
from PIL import Image

W, H = 120, 120
PIXEL_BYTES = W * H // 2


def nibbles_normal(data):
    """Current decode: high nibble first, left-to-right every row."""
    px = []
    for b in data:
        px.append(255 - ((b >> 4) & 0xF) * 17)
        px.append(255 - ((b & 0xF) * 17))
    return px


def nibbles_swapped(data):
    """Low nibble first — tests if nibble pair order is reversed."""
    px = []
    for b in data:
        px.append(255 - ((b & 0xF) * 17))
        px.append(255 - ((b >> 4) & 0xF) * 17)
    return px


def reverse_odd_rows(px):
    result = px[:]
    for row in range(1, H, 2):
        s = row * W
        result[s:s + W] = result[s:s + W][::-1]
    return result


def reverse_even_rows(px):
    result = px[:]
    for row in range(0, H, 2):
        s = row * W
        result[s:s + W] = result[s:s + W][::-1]
    return result


def deinterlace_fields(px):
    """Treat top 60 rows as even-field, bottom 60 as odd-field, interleave."""
    result = [0] * (W * H)
    for i in range(H // 2):
        result[i * 2 * W:(i * 2 + 1) * W] = px[i * W:(i + 1) * W]           # even rows
        result[(i * 2 + 1) * W:(i * 2 + 2) * W] = px[(H // 2 + i) * W:(H // 2 + i + 1) * W]  # odd rows
    return result


def deinterlace_fields_swapped(px):
    """Same as deinterlace_fields but odd-field on top, even on bottom."""
    result = [0] * (W * H)
    for i in range(H // 2):
        result[i * 2 * W:(i * 2 + 1) * W] = px[(H // 2 + i) * W:(H // 2 + i + 1) * W]
        result[(i * 2 + 1) * W:(i * 2 + 2) * W] = px[i * W:(i + 1) * W]
    return result


VARIANTS = {
    "normal":             lambda d: nibbles_normal(d),
    "nibbles-swapped":    lambda d: nibbles_swapped(d),
    "reverse-odd":        lambda d: reverse_odd_rows(nibbles_normal(d)),
    "reverse-even":       lambda d: reverse_even_rows(nibbles_normal(d)),
    "deinterlace":        lambda d: deinterlace_fields(nibbles_normal(d)),
    "deinterlace-swap":   lambda d: deinterlace_fields_swapped(nibbles_normal(d)),
    "nibswap-rev-odd":    lambda d: reverse_odd_rows(nibbles_swapped(d)),
    "nibswap-deinterlace":lambda d: deinterlace_fields(nibbles_swapped(d)),
}


def decode(raw_path: Path, variant: str, out_dir: Path):
    data = raw_path.read_bytes()
    pixel_data = data[29:29 + PIXEL_BYTES]
    px = VARIANTS[variant](pixel_data)
    img = Image.frombytes('L', (W, H), bytes(px[:W * H]))
    out = out_dir / f"{raw_path.stem}_{variant}.png"
    img.save(out)
    print(f"  {variant:25s} → {out}")
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path, help=".raw file from wqv_transfer.py")
    parser.add_argument("--variant", default="all",
                        choices=list(VARIANTS) + ["all"],
                        help="Decode variant (default: all)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output directory (default: same as input)")
    args = parser.parse_args()

    if not args.raw.exists():
        sys.exit(f"File not found: {args.raw}")

    out_dir = args.out or args.raw.parent
    out_dir.mkdir(exist_ok=True)

    variants = list(VARIANTS) if args.variant == "all" else [args.variant]
    print(f"Decoding {args.raw.name} ({len(variants)} variant(s)):")
    for v in variants:
        decode(args.raw, v, out_dir)


if __name__ == "__main__":
    main()
