"""Image processing for the live camera view: preview scaling, stripe reduction,
frame averaging, exposure statistics and focus scores. Pure numpy/OpenCV, no GUI."""
from __future__ import annotations

import cv2
import numpy as np


def fit_size(width: int, height: int, max_w: int, max_h: int) -> tuple[int, int]:
    """Largest size with the same aspect ratio that fits inside max_w x max_h (never upscales)."""
    scale = min(max_w / width, max_h / height, 1.0)
    return max(1, round(width * scale)), max(1, round(height * scale))


def downscale(image: np.ndarray, max_w: int, max_h: int) -> np.ndarray:
    h, w = image.shape[:2]
    size = fit_size(w, h, max_w, max_h)
    if size == (w, h):
        return image
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def destripe(image: np.ndarray, degree: int = 3) -> np.ndarray:
    """Remove horizontal bands (rolling shutter vs. a flickering light source).

    Each row is scaled so its median brightness follows a smooth top-to-bottom trend
    (a low-order polynomial), per colour channel. Real image content survives as long as
    it does not fill most of a row; full-width horizontal features are flattened, so this
    is an option, not a fix for the light source.
    """
    if image.ndim != 3 or image.shape[0] < 8:
        return image
    h = image.shape[0]
    step = max(1, image.shape[1] // 240)  # ~240 samples per row is plenty for a median
    rows = np.median(image[:, ::step, :], axis=1).astype(np.float32)  # H x C
    y = np.linspace(-1.0, 1.0, h)
    gain = np.ones_like(rows)
    for c in range(rows.shape[1]):
        profile = rows[:, c]
        if profile.max() < 4.0:  # channel essentially dark: nothing to correct
            continue
        trend = np.polyval(np.polyfit(y, profile, degree), y)
        valid = profile > 2.0
        gain[valid, c] = np.clip(trend[valid] / profile[valid], 0.25, 4.0)
    if np.allclose(gain, 1.0):
        return image
    out = image.astype(np.float32)
    out *= gain[:, None, :]
    np.clip(out, 0, 255, out=out)
    return out.astype(np.uint8)


def stripe_strength(image: np.ndarray):
    """How striped the image is: relative row-to-row variation of the brightest channel's
    row medians after removing the smooth trend (0 = none, ~0.1 = clearly visible).
    None when the picture is too dark to tell (a dark picture is not a stripe-free one)."""
    if image.ndim != 3 or image.shape[0] < 8:
        return None
    step = max(1, image.shape[1] // 240)
    rows = np.median(image[:, ::step, :], axis=1).astype(np.float32)
    c = int(np.argmax(rows.mean(axis=0)))
    profile = rows[:, c]
    if profile.mean() < 4.0:
        return None
    y = np.linspace(-1.0, 1.0, len(profile))
    trend = np.polyval(np.polyfit(y, profile, 3), y)
    return float(np.std(profile - trend) / max(1.0, float(np.mean(trend))))


class FrameAverager:
    """Running average of the last n frames; stripes that drift between frames fade."""

    def __init__(self, count: int = 1):
        self.count = max(1, int(count))
        self.frames: list[np.ndarray] = []

    def set_count(self, count: int):
        self.count = max(1, int(count))
        self.frames = self.frames[-self.count:]

    def add(self, frame: np.ndarray) -> np.ndarray:
        if self.count <= 1:
            self.frames = []
            return frame
        if self.frames and self.frames[0].shape != frame.shape:
            self.frames = []
        self.frames.append(frame.astype(np.float32))
        self.frames = self.frames[-self.count:]
        return (sum(self.frames) / len(self.frames)).astype(np.uint8)


def image_stats(image: np.ndarray) -> dict:
    """Brightness statistics used by auto exposure. `image` is 3-channel uint8.

    p99 and saturated are measured on blocks about 1/32 of the picture height tall, so the thin
    bright lines of projector stripes do not look like an over-exposed picture (that made the
    automatic exposure darken the picture until it was black). The raw values are kept too."""
    h, w = image.shape[:2]
    blocks = cv2.resize(image, (max(1, w // 16), max(1, h // 32)), interpolation=cv2.INTER_AREA).max(axis=2)
    sample = image[::4, ::4]
    peak = sample.max(axis=2)
    return {
        "mean": float(sample.mean()),
        "p50": float(np.percentile(blocks, 50)),
        "p99": float(np.percentile(blocks, 99)),
        "saturated": float((blocks >= 245).mean()),
        "p99_raw": float(np.percentile(peak, 99)),
        "saturated_raw": float((peak >= 250).mean()),
    }


def compute_focus_score(camera_image: np.ndarray, blue_only: bool) -> float:
    """Sharpness of an RGB image (higher is sharper). Green is ignored; blue_only for UV.

    Uses horizontal gradients normalised by each row's brightness, so horizontal stripes
    from the projector (rolling shutter) neither add false edges nor change the score."""
    img = camera_image.copy()
    img[:, :, 1] = 0  # green should never be used for focus
    if blue_only:
        img[:, :, 0] = 0  # disable red
    gray = cv2.resize(cv2.cvtColor(img, cv2.COLOR_RGB2GRAY), (0, 0), fx=0.5, fy=0.5).astype(np.float32)
    if float(gray.mean()) <= 0.5:
        return 0.0
    gx = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=1))
    row_brightness = gray.mean(axis=1, keepdims=True) + 1.0
    return float((gx / row_brightness).var())


def center_crop(image: np.ndarray, max_w: int = 1200, max_h: int = 1200) -> np.ndarray:
    h, w = image.shape[:2]
    cw, ch = min(w, max_w), min(h, max_h)
    x, y = (w - cw) // 2, (h - ch) // 2
    return image[y:y + ch, x:x + cw]
