"""Projector stripes on a rolling-shutter camera: measure them, and take them out of the preview.

Why there are stripes. A DLP projector's light is not steady: it repeats a fixed sequence of
pulses (colour segments, bit-planes, the 4K pixel shift) every frame or fraction of a frame.
A rolling-shutter camera starts exposing each row a little later than the row above. Unless the
exposure lasts a whole number of light cycles, neighbouring rows catch slightly different
amounts of light, which shows up as horizontal lines.

Why they can be separated from the picture. Every row is exposed for the same time and the
start time moves down the picture at a constant rate, so the lines repeat with one fixed
spacing and the same shape all the way down. Letters, the chip edge and the dark area around
the chip do not. This module finds that spacing in the row-brightness spectrum, fits a periodic
profile (a fundamental and its harmonics) and removes only that. The previous filter flattened
every row towards a smooth curve, which failed whenever the lit chip covered less than half of
the picture's width, and also flattened real horizontal edges.

Limitation: a real, evenly spaced horizontal grating that fills most of the picture width
looks exactly like stripes. Snapshots are never filtered.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

COLUMNS = 320            # columns sampled per row (plenty for a robust median)
MIN_CYCLES = 4.0         # a stripe pattern repeats at least this often down the picture
MAX_FREQ = 0.45          # cycles per row (0.5 is the finest possible)
MAX_HARMONICS = 24
PEAK_FACTOR = 14.0       # a spectral line must stand this far above the noise floor
MAX_FAMILIES = 2         # independent stripe patterns fitted at once
MAX_SUBHARMONIC = 12     # the light cycle can be this many times longer than the strongest line
TREND_DEGREE = 1
MIN_STRENGTH = 0.002     # below this (0.2 % row-to-row) there is nothing worth correcting
REESTIMATE_SECONDS = 3.0 # time between full searches for the stripe spacing


@dataclass
class StripeFit:
    strength: Optional[float]                  # RMS of the periodic row brightness (0.01 = 1 %), None if too dark
    gains: Optional[np.ndarray] = None         # per-row brightness factor to divide out, None = nothing to fix
    periods: tuple = ()                        # stripe spacing(s) in rows
    lit_rows: float = 0.0                      # fraction of rows bright enough to judge


# --------------------------------------------------------------------------- row profile
def running_median(values: np.ndarray, window: int, stride: int = 4) -> np.ndarray:
    """Median over a sliding window (edges padded by reflection). Keeps steps, removes short bumps.
    Evaluated every `stride` samples and interpolated in between (it changes slowly by design)."""
    window = max(3, window | 1)
    half = window // 2
    padded = np.pad(values, half, mode="reflect") if len(values) > half else np.pad(values, half, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, window)
    at = np.arange(0, len(values), max(1, stride))
    if at[-1] != len(values) - 1:
        at = np.append(at, len(values) - 1)
    return np.interp(np.arange(len(values)), at, np.median(windows[at], axis=1))


def row_profile(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(log brightness of each row minus its slowly varying level, weight per row).

    A row's brightness is the mean of its lit pixels, so the dark area around the chip and dark
    letters do not count. The slowly varying level is a running median over many rows, which
    keeps steps such as the chip edge, so what is left is mostly the row-to-row stripes.
    Rows with too few lit pixels (dark, outside the chip) get weight 0."""
    step = max(1, image.shape[1] // COLUMNS)
    if image.ndim == 3:
        sample = image[:, ::step]
        flat = sample.reshape(-1, sample.shape[2])
        channel = int(np.argmax(flat[::max(1, len(flat) // 4096)].mean(axis=0)))
        g = sample[:, :, channel].astype(np.float32)
    else:
        g = image[:, ::step].astype(np.float32)
    h, w = g.shape
    threshold = max(8.0, 0.3 * float(np.percentile(g[::4], 99)))
    lit = g > threshold
    count = lit.sum(axis=1)
    # Mean of the lit pixels (a median per row would be a little more robust, but sorting every row
    # costs more than the whole rest of the analysis; the fit below ignores odd rows anyway).
    median = (g * lit).sum(axis=1) / np.maximum(count, 1)
    good = count >= max(4, int(0.04 * w))
    profile = np.zeros(h, np.float64)
    if good.sum() < 8:
        return profile, good.astype(np.float64)
    level = np.log(np.where(good, np.clip(median, 1.0, None), 1.0))
    # Fill unusable rows from their neighbours so the running median is not pulled down by them.
    idx = np.arange(h)
    level = np.interp(idx, idx[good], level[good])
    profile = level - running_median(level, max(31, h // 4))
    return np.where(good, profile, 0.0), good.astype(np.float64)


# --------------------------------------------------------------------------- periodic fit
def _trend_columns(n: int) -> np.ndarray:
    y = np.linspace(-1.0, 1.0, n)
    return np.stack([y ** d for d in range(TREND_DEGREE + 1)], axis=1)


def _harmonic_columns(n: int, families) -> np.ndarray:
    y = np.arange(n, dtype=np.float64)
    cols = []
    for f0, harmonics in families:
        for k in range(1, harmonics + 1):
            phase = 2.0 * np.pi * k * f0 * y
            cols.append(np.cos(phase))
            cols.append(np.sin(phase))
    return np.stack(cols, axis=1) if cols else np.zeros((n, 0))


def _solve(profile, weight, families):
    """Weighted least squares: profile ≈ trend + Σ harmonics. Returns (periodic part, rss, residuals).
    Normal equations: a few dozen columns, so this is fast enough to call many times per search."""
    n = len(profile)
    trend = _trend_columns(n)
    harm = _harmonic_columns(n, families)
    design = np.concatenate([trend, harm], axis=1)
    weighted = design * weight[:, None]
    gram = design.T @ weighted
    gram[np.diag_indices_from(gram)] += 1e-9 * (np.trace(gram) / len(gram) + 1.0)
    try:
        coef = np.linalg.solve(gram, weighted.T @ profile)
    except np.linalg.LinAlgError:
        coef = np.linalg.lstsq(design * np.sqrt(weight)[:, None], profile * np.sqrt(weight), rcond=None)[0]
    periodic = harm @ coef[trend.shape[1]:]
    residual = profile - design @ coef
    rss = float(np.sum(weight * residual ** 2))
    return periodic, rss, residual


def _robust_solve(profile, weight, families):
    """One bisquare reweighting so a few odd rows (a horizontal edge) cannot bend the fit."""
    periodic, rss, residual = _solve(profile, weight, families)
    used = weight > 0
    if used.sum() < 10:
        return periodic, rss, residual, weight
    scale = 1.4826 * float(np.median(np.abs(residual[used]))) + 1e-6
    u = residual / (4.685 * scale)
    robust = weight * np.where(np.abs(u) < 1.0, (1.0 - u ** 2) ** 2, 0.0)
    if robust.sum() < 0.5 * weight.sum():
        return periodic, rss, residual, weight
    periodic, rss, residual = _solve(profile, robust, families)
    return periodic, rss, residual, robust


def harmonics_for(f0: float) -> int:
    return max(1, min(MAX_HARMONICS, int(MAX_FREQ / f0)))


def _spectrum(residual, weight):
    n = len(residual)
    x = residual * weight
    if (weight > 0).any():
        x = (x - x[weight > 0].mean()) * (weight > 0)
    nfft = 1 << int(np.ceil(np.log2(n * 4)))
    spectrum = np.abs(np.fft.rfft(x * np.hanning(n), nfft)) ** 2
    band = slice(int(np.ceil(MIN_CYCLES / n * nfft)), int(MAX_FREQ * nfft) + 1)
    floor = float(np.median(spectrum[band])) + 1e-18 if spectrum[band].size else 1e-18
    return spectrum, nfft, band, floor


def _strongest_line(spectrum, nfft, band, floor):
    """Frequency (cycles/row) of the strongest line clearly above the noise floor, or None."""
    segment = spectrum[band]
    if not segment.size:
        return None
    i = band.start + int(np.argmax(segment))
    if spectrum[i] < PEAK_FACTOR * floor:
        return None
    if 0 < i < len(spectrum) - 1:   # parabolic interpolation on the log spectrum
        a, b, c = np.log(spectrum[i - 1:i + 2] + 1e-30)
        denom = a - 2 * b + c
        offset = 0.5 * (a - c) / denom if denom < 0 else 0.0
        return (i + float(np.clip(offset, -0.5, 0.5))) / nfft
    return i / nfft


def _refine(profile, weight, f0, others, harmonics, delta, steps=11, iterations=14):
    """Best fundamental within f0 ± delta (the spectrum only gives it roughly): grid, then golden section."""
    def cost(f):
        return _solve(profile, weight, others + [(f, harmonics)])[1]
    n = len(profile)
    lo, hi = max(f0 - delta, MIN_CYCLES / n), min(f0 + delta, MAX_FREQ)
    if hi <= lo:
        return f0, cost(f0)
    grid = np.linspace(lo, hi, steps)
    costs = [cost(f) for f in grid]
    j = int(np.argmin(costs))
    a, b = grid[max(0, j - 1)], grid[min(len(grid) - 1, j + 1)]
    ratio = (5 ** 0.5 - 1) / 2
    c, d = b - ratio * (b - a), a + ratio * (b - a)
    fc, fd = cost(c), cost(d)
    for _ in range(iterations):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - ratio * (b - a)
            fc = cost(c)
        else:
            a, c, fc = c, d, fd
            d = a + ratio * (b - a)
            fd = cost(d)
    return (c, fc) if fc < fd else (d, fd)


def _fundamental(profile, weight, peak, others):
    """The light cycle may be longer than the strongest line suggests (that line can be its 2nd,
    8th harmonic: a frame has 4 pixel-shift steps with 2 red segments each). Try cycles up to 12 times longer and keep the one that explains the rows best
    for the number of terms it needs (BIC)."""
    n = len(profile)
    n_eff = max(float(weight.sum()), 2.0)
    best = None
    for m in range(1, MAX_SUBHARMONIC + 1):
        f0 = peak / m
        if f0 < MIN_CYCLES / n:
            break
        k = harmonics_for(f0)
        rss = _solve(profile, weight, others + [(f0, k)])[1]
        bic = n_eff * np.log(max(rss, 1e-18) / n_eff) + 2 * k * np.log(n_eff)
        if best is None or bic < best[0]:
            best = (bic, f0, m)
    return (best[1], best[2]) if best else (peak, 1)


def find_families(profile, weight) -> list[tuple[float, int]]:
    """Stripe patterns present: [(fundamental in cycles/row, harmonics)], strongest first."""
    n = len(profile)
    n_eff = float(weight.sum())
    if n_eff < max(24, 0.25 * n):
        return []
    families: list[tuple[float, int]] = []
    _p, rss, residual = _solve(profile, weight, families)
    for _ in range(MAX_FAMILIES):
        spectrum, nfft, band, floor = _spectrum(residual, weight)
        peak = _strongest_line(spectrum, nfft, band, floor)
        if peak is None:
            break
        # Pin the strongest line down first (few harmonics: fast), then look for a longer cycle.
        peak, _ = _refine(profile, weight, peak, families, min(3, harmonics_for(peak)), 0.6 / n)
        f0, m = _fundamental(profile, weight, peak, families)
        k = harmonics_for(f0)
        f0, rss_new = _refine(profile, weight, f0, families, k, 0.15 / (n * m), steps=7, iterations=10)
        # Keep it only if it explains clearly more than its extra terms would explain in noise (BIC).
        n_eff = float(weight.sum())
        if n_eff * np.log(max(rss, 1e-18) / max(rss_new, 1e-18)) < 2 * k * np.log(n_eff):
            break
        families.append((f0, k))
        rss = rss_new
        _p, _rss, residual = _solve(profile, weight, families)
    return families


def fit(profile, weight, families) -> StripeFit:
    n = len(profile)
    n_eff = float(weight.sum())
    lit = n_eff / max(1, n)
    if n_eff < max(24, 0.25 * n):
        return StripeFit(None, None, (), lit)
    if not families:
        return StripeFit(0.0, None, (), lit)
    periodic, _rss, residual, robust = _robust_solve(profile, weight, families)
    used = robust > 0
    strength = float(np.sqrt(np.sum(robust * periodic ** 2) / max(robust.sum(), 1.0)))
    noise = 1.4826 * float(np.median(np.abs(residual[used]))) if used.any() else 0.0
    terms = sum(2 * k for _f, k in families)
    floor = 3.0 * noise * np.sqrt(terms / max(n_eff, 1.0))   # what fitting pure noise would give
    gains = None
    if strength > max(MIN_STRENGTH, floor):
        gains = np.exp(np.clip(periodic, -0.7, 0.7))
    return StripeFit(strength, gains, tuple(1.0 / f for f, _k in families), lit)


def analyse(image: np.ndarray) -> StripeFit:
    """Stateless: find the stripe pattern in this one picture and fit it."""
    if image.ndim < 2 or image.shape[0] < 16:
        return StripeFit(None)
    profile, weight = row_profile(image)
    return fit(profile, weight, find_families(profile, weight))


def apply_gains(image: np.ndarray, gains: Optional[np.ndarray]) -> np.ndarray:
    if gains is None:
        return image
    out = image.astype(np.float32)
    out /= gains.astype(np.float32).reshape((-1,) + (1,) * (image.ndim - 1))
    np.clip(out, 0, 255, out=out)
    return out.astype(np.uint8)


@dataclass
class StripeTracker:
    """Per-camera state. The stripe spacing does not change while the camera mode stays the same
    (it is the light cycle divided by the sensor's row time), so the slow search for it runs only
    every few seconds, or right after an exposure change, and the result is reused in between.
    Keeping the spacing through clean frames also means a stripe-free exposure is measured against
    the same pattern as a striped one."""
    interval: float = REESTIMATE_SECONDS
    families: list = field(default_factory=list)
    rows: int = 0
    next_search: float = 0.0

    def invalidate(self):
        self.next_search = 0.0

    def analyse(self, image: np.ndarray, now: Optional[float] = None) -> StripeFit:
        if image.ndim < 2 or image.shape[0] < 16:
            return StripeFit(None)
        now = time.monotonic() if now is None else now
        profile, weight = row_profile(image)
        if len(profile) != self.rows:
            self.rows, self.families, self.next_search = len(profile), [], 0.0
        if now >= self.next_search:
            found = find_families(profile, weight)
            if found:
                self.families = found
                self.next_search = now + self.interval
            else:
                # Nothing visible (or too dark): keep the old spacing and look again sooner.
                self.next_search = now + self.interval / 3
        return fit(profile, weight, self.families)
