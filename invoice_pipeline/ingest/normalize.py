"""Step: image normalization (orientation, color mode, size, JPEG output)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    """Convert to RGB, compositing any transparency onto white."""
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if has_alpha:
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB")


def normalize_image(img: Image.Image, max_dimension: int) -> Image.Image:
    img = ImageOps.exif_transpose(img)  # fix phone-camera rotation
    img = _flatten_to_rgb(img)
    longest = max(img.size)
    if longest > max_dimension:  # downscale only, never upscale
        scale = max_dimension / longest
        new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    return img


def save_jpeg(img: Image.Image, path: Path, quality: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="JPEG", quality=quality, optimize=True)
