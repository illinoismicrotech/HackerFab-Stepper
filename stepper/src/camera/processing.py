"""Image processing for the live camera view: preview scaling, stripe reduction,
frame averaging, exposure statistics and focus scores. Pure numpy/OpenCV, no GUI."""
from __future__ import annotations

import cv2
import numpy as np

from camera.stripes import analyse, apply_gains


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
    """Remove the projector's horizontal stripes (rolling shutter vs. pulsed DLP light).

    Only the evenly spaced, repeating part of the row brightness is removed (see camera.stripes),
    so the chip edge, letters and the dark area around the chip are left as they are. Returns the
    same array when there is nothing to remove. `degree` is kept for compatibility."""
    if image.ndim != 3 or image.shape[0] < 16:
        return image
    return apply_gains(image, analyse(image).gains)


def stripe_strength(image: np.ndarray):
    """How striped the image is: RMS of the repeating row-to-row brightness change (0.01 = 1 %).
    None when the picture is too dark to tell (a dark picture is not a stripe-free one)."""
    if image.ndim != 3 or image.shape[0] < 16:
        return None
    return analyse(image).strength


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


FOCUS_LEVELS = 6        # full resolution, 1/2, 1/4, 1/8, 1/16, 1/32
FOCUS_FRACTION = 0.5    # focus is judged on the middle half (width and height) of the picture
FOCUS_DEFAULT_WIDTH = 700


def gradient_score(gray: np.ndarray) -> float:
    """Sharpness of one grey image: mean squared horizontal gradient, each row divided by its own
    brightness so projector stripes (whole rows brighter or darker) cannot change the score.
    ×1000 only to give readable numbers."""
    if gray.size == 0 or float(gray.mean()) <= 0.5:
        return 0.0
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=1)   # [-1 0 1] along the row: rows are never mixed
    row = gray.mean(axis=1, keepdims=True) + 1.0
    return float(np.mean(np.square(gx / row))) * 1000.0


def focus_pyramid(channel: np.ndarray, levels: int = FOCUS_LEVELS, half: np.ndarray | None = None) -> list:
    """Sharpness at full resolution and at 1/2, 1/4, ... of it (None once the picture gets tiny).

    Which scale is best depends on the optics and the chip: at full resolution a 20 MP sensor
    mostly sees noise (and under red light only every other pixel has a red filter); at a coarse
    scale the score barely changes with focus. Autofocus measures them all and uses the one where
    focus changes the score the most compared with frame-to-frame noise.
    `half`, if given, is the same picture already reduced to half size (saves recomputing it)."""
    scores = [gradient_score(channel.astype(np.float32)) if min(channel.shape[:2]) >= 24 else None]
    img = (half if half is not None else cv2.pyrDown(channel)).astype(np.float32)
    for level in range(1, levels):
        if level > 1:
            img = cv2.pyrDown(img)
        scores.append(gradient_score(img) if min(img.shape[:2]) >= 24 else None)
    return scores


def default_focus_level(crop_width: int) -> int:
    """The scale shown on the live focus bar until an autofocus picks one: about 700 px wide."""
    if crop_width <= FOCUS_DEFAULT_WIDTH:
        return 0
    return int(min(FOCUS_LEVELS - 1, max(0, round(np.log2(crop_width / FOCUS_DEFAULT_WIDTH)))))


def focus_measurements(frame_bgr: np.ndarray, fraction: float = FOCUS_FRACTION, half: np.ndarray | None = None) -> dict:
    """Focus scores for the red and blue channels at every scale, from the middle of the
    full-resolution frame. `half` is the whole frame at half size if the caller has it already.
    A channel that is essentially dark (blue under red light) scores 0 without being measured."""
    h, w = frame_bgr.shape[:2]
    cw, ch = max(16, int(w * fraction)), max(16, int(h * fraction))
    crop = center_crop(frame_bgr, cw, ch)
    half_crop = center_crop(half, max(8, cw // 2), max(8, ch // 2)) if half is not None else None
    means = crop[::16, ::16].reshape(-1, 3).mean(axis=0)
    result = {"crop": (crop.shape[1], crop.shape[0]), "default": default_focus_level(crop.shape[1])}
    for name, index in (("red", 2), ("blue", 0)):
        if means[index] < 0.04 * max(float(means.max()), 1.0) or means[index] < 0.5:
            result[name] = [0.0] * FOCUS_LEVELS
            continue
        channel = np.ascontiguousarray(crop[:, :, index])
        sub = np.ascontiguousarray(half_crop[:, :, index]) if half_crop is not None else None
        result[name] = focus_pyramid(channel, half=sub)
    return result


def center_crop(image: np.ndarray, max_w: int = 1200, max_h: int = 1200) -> np.ndarray:
    h, w = image.shape[:2]
    cw, ch = min(w, max_w), min(h, max_h)
    x, y = (w - cw) // 2, (h - ch) // 2
    return image[y:y + ch, x:x + cw]
