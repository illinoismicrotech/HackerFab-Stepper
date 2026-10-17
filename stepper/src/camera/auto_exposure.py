"""Automatic camera exposure and projector red brightness.

Runs only while the projector shows red light (focus mode). Two jobs:

1. Find the stripe-free exposures. A DLP projector pulses its light; the camera's rolling
   shutter turns the pulses into lines unless the exposure spans a whole number of light
   cycles. The cycle isn't known in advance, so every exposure in CANDIDATES is tried and its
   lines measured (camera.stripes). Every exposure about as clean as the best one is kept: the
   "stripe-free set". A remembered exposure is tried first and kept straight away if it is clean.
2. Keep the picture well exposed without bringing the lines back:
   - the exposure only ever moves within the stripe-free set;
   - the red light stays at 100 % whenever it can. A dimmed red is drawn by the projector's
     mirrors with fewer, unevenly spread pulses, and that brings lines back at any exposure;
   - so: full red, then the longest stripe-free exposure that does not need less than the
     minimum gain, then gain. Only if the picture is too bright at the shortest stripe-free
     exposure and minimum gain is the red light dimmed;
   - it acts only when brightness has left a wide band for a few frames in a row, then aims for
     the middle of it (hysteresis), so it does not keep adjusting while the stage moves.

Pure logic: feed it frame statistics, apply the actions it returns.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

CANDIDATES = (-6, -5, -4, -3)   # DirectShow units: 15.6, 31.3, 62.5, 125 ms
TARGET_P99 = 200                # brightest 1% of the picture: bright, not clipped
LOW_P99, HIGH_P99 = 150, 238    # settled: act only when brightness leaves this band ...
AIM_LOW, AIM_HIGH = 178, 224    # ... then adjust until it is back in this narrower one
MEASURE_LOW, MEASURE_HIGH = 90, 245   # bright enough (and not clipped) to measure stripes
GAMMA = 2.2                     # projectors apply ~2.2 gamma: light ∝ pixel_value^2.2
SETTLE_FRAMES = 4               # frames to ignore after any change (camera + display latency)
MEASURE_FRAMES = 6              # frames per stripe measurement (median)
DEBOUNCE_FRAMES = 3             # frames out of band in a row before reacting
MIN_RED, MAX_RED = 0.02, 1.0
GAIN_CAP_FACTOR = 4             # more than 4x the minimum gain mostly adds noise
CLEAN = 0.006                   # RMS row-to-row brightness change (0.6 %) nobody can see


def exposure_ms(value: float) -> float:
    return 1000.0 * 2.0 ** value


def _is_clipped(stats: dict) -> bool:
    return stats.get("saturated", 0.0) > 0.003 or stats.get("p99", 0.0) >= 250


def choose_exposure(results: dict):
    """The exposure with the fewest stripes. When several are about equally good (within a tenth of
    the spread between the best and the worst), take the shortest: faster and less motion blur.
    Exposures whose picture was too dark to measure (None) are never chosen; None if all were."""
    valid = {e: s for e, s in results.items() if s is not None}
    if not valid:
        return None
    best, worst = min(valid.values()), max(valid.values())
    limit = best + 0.1 * (worst - best) + 0.002
    return min(e for e, s in valid.items() if s <= limit)


def good_exposures(results: dict) -> list:
    """Every exposure about as clean as the best one (or clean outright), shortest first."""
    valid = {e: s for e, s in results.items() if s is not None}
    if not valid:
        return []
    best = min(valid.values())
    limit = max(CLEAN, best + max(0.002, 0.25 * best))
    return sorted(e for e, s in valid.items() if s <= limit)


def exposure_limits(low: float, high: float) -> tuple[int, int]:
    """Exposures the automatic brightness control may use: the stripe-search range, inside the
    camera's range. Shorter exposures only make the projector stripes worse (and once drove the
    picture to black), longer ones run past the frame time."""
    lo = max(int(low), min(CANDIDATES))
    hi = min(int(high), max(CANDIDATES))
    return (lo, hi) if lo <= hi else (int(low), int(high))


def refresh_hint(refresh_hz) -> str:
    """Advice when the projector display's frame time does not fit the camera's exposure steps.

    The projector repeats its light pattern once per input frame (or a fraction of one), so an
    exposure lasting a whole number of frames is line-free whatever the projector does inside a
    frame. The camera only offers 15.6, 31.3, 62.5 and 125 ms: none is a whole number of 60 Hz
    frames (16.7 ms), while at 48 Hz 62.5 and 125 ms are exactly 3 and 6 frames, and at 24 Hz
    125 ms is exactly 3."""
    try:
        hz = float(refresh_hz)
    except (TypeError, ValueError):
        return ""
    if not 1 < hz < 1000:
        return ""
    if round(hz) + 1 in (24, 30, 48, 60, 120):
        hz = (round(hz) + 1) / 1.001    # Windows reports 23.976 Hz as 23, 59.94 Hz as 59
    frame_ms = 1000.0 / hz
    for e in CANDIDATES:
        frames = exposure_ms(e) / frame_ms
        if frames >= 0.99 and abs(frames - round(frames)) < 0.02:
            return ""
    return (f" The projector display runs at {hz:.0f} Hz, and none of the camera's exposure steps (15.6 to 125 ms) "
            f"lasts a whole number of its {frame_ms:.1f} ms frames. If Windows offers 48 Hz or 24 Hz for the "
            "projector display (Settings → System → Display → choose the projector → Advanced display → refresh "
            "rate), try it and click Find stripe-free exposure again: 125 ms is then exactly 6 or 3 frames.")


def describe_lines(value) -> str:
    return "too dark" if value is None else f"{100 * value:.1f} %"


@dataclass
class Action:
    kind: str            # "red" (projector red brightness 0.02-1) or "camera" (dict of controls)
    value: object


@dataclass
class AutoCamera:
    ranges: dict = field(default_factory=dict)
    auto_exposure: bool = True     # search + choose exposure/gain
    auto_light: bool = True        # adjust projector red brightness
    phase: str = "idle"            # idle | search | track | paused
    status: str = "Auto off"
    summary: str = ""              # result of the last stripe search
    results: dict = field(default_factory=dict)      # exposure -> stripe strength (None = too dark)
    brightness: dict = field(default_factory=dict)   # exposure -> p99 expected at full red, min gain
    best_exposure: int | None = None
    good: list = field(default_factory=list)         # the stripe-free set
    _candidates: list = field(default_factory=list)
    _verify: int | None = None     # remembered exposure being checked first
    _wait_until: int = -1
    _samples: list = field(default_factory=list)
    _last_index: int = -1
    _resume_phase: str = "track"
    _outside: int = 0              # frames in a row outside the settled band
    _adjusting: bool = False

    # ------------------------------------------------------------------ helpers
    def _range(self, name, default):
        info = self.ranges.get(name) or {}
        return info.get("min", default[0]), info.get("max", default[1])

    def _gain_limits(self):
        gmin, gmax = self._range("gain", (100, 2239))
        return gmin, max(gmin, min(gmax, gmin * GAIN_CAP_FACTOR))

    def _wait(self, index):
        self._wait_until = index + SETTLE_FRAMES
        self._samples = []

    def allowed_exposures(self, current: int) -> list:
        """Exposures the brightness control may switch between: the stripe-free set."""
        low, high = exposure_limits(*self._range("exposure", (-13, -1)))
        allowed = [e for e in (self.good or ([self.best_exposure] if self.best_exposure is not None else []))
                   if low <= e <= high]
        return allowed or [current]

    def start_search(self, index: int = 0, remembered: int | None = None):
        low, high = self._range("exposure", (-13, -1))
        candidates = [e for e in CANDIDATES if low <= e <= high] or [max(low, min(high, -4))]
        self._verify = None
        if remembered is not None:
            try:
                remembered = int(remembered)
            except (TypeError, ValueError):
                remembered = None
        if remembered is not None and low <= remembered <= high:
            # Check the remembered exposure first; if it is still clean, that is the whole search.
            candidates = [remembered] + [e for e in candidates if e != remembered]
            self._verify = remembered
        self._candidates = candidates
        self.results, self.brightness = {}, {}
        self.best_exposure, self.good = None, []
        self.summary = ""
        self.phase = "search"
        self._adjusting, self._outside = False, 0
        self._wait(index)
        self.status = ("Checking the remembered stripe-free exposure…" if self._verify is not None
                       else "Finding the stripe-free exposure…")

    def pause(self, reason="Auto paused (projector not red)"):
        if self.phase in ("search", "track"):
            self._resume_phase = self.phase
            self.phase = "paused"
            self.status = reason

    def resume(self, index: int):
        if self.phase == "paused":
            self.phase = self._resume_phase or "track"
            self._wait(index)
            self._outside, self._adjusting = 0, False
            self.status = "Auto: adjusting…"

    def cancel_search(self):
        """Auto exposure was switched off: stop trying exposures, keep what the user set."""
        if self.phase == "search" or self._resume_phase == "search":
            self._resume_phase = "track"
            if self.phase == "search":
                self.phase = "track"
            self._candidates = []
            self.status = "Stripe search stopped: auto exposure is off."

    # ------------------------------------------------------------------ main step
    def update(self, index: int, stats: dict, red: float, exposure: int, gain: int) -> list[Action]:
        """One frame. `red` is the projector's red brightness (0-1), `exposure`/`gain` the camera's."""
        if index <= self._last_index:
            return []  # same frame twice
        self._last_index = index
        if self.phase in ("idle", "paused") or index < self._wait_until:
            return []
        if self.phase == "search":
            if not self.auto_exposure:
                self.cancel_search()
                return []
            return self._search(index, stats, red, exposure, gain)
        return self._track(index, stats, red, exposure, gain)

    # ------------------------------------------------------------------ brightness planning
    def plan(self, stats: dict, red: float, exposure: int, gain: int, exposures: list) -> tuple[float, int, int]:
        """(red, exposure, gain) expected to bring the brightest 1 % to TARGET_P99.

        Preference: full red; then the longest allowed exposure that does not need less than the
        minimum gain; then the least gain. Dim red only when even the shortest exposure at the
        minimum gain is too bright."""
        gmin, cap = self._gain_limits()
        p99 = float(stats.get("p99", 0.0))
        if _is_clipped(stats):
            want = 0.5                                   # true brightness unknown: halve, then look again
        else:
            want = min(8.0, TARGET_P99 / max(p99, 2.0))  # very dark readings are unreliable: at most 8x
        budget = (max(red, MIN_RED) ** GAMMA) * (2.0 ** exposure) * max(gain, 1e-6) * want
        reds = MAX_RED if self.auto_light else red
        exps = sorted(exposures) if self.auto_exposure else [exposure]
        low_gain, high_gain = (gmin, cap) if self.auto_exposure else (gain, gain)
        for e in reversed(exps):                         # longest first
            g = budget / (reds ** GAMMA * 2.0 ** e)
            if g >= low_gain * 0.94:                     # (rounding: a hair under the minimum is fine)
                return reds, e, int(min(high_gain, max(low_gain, round(g))))
        e, g = exps[0], low_gain                         # too bright even at the shortest, least gain
        r = reds
        if self.auto_light:
            r = max(MIN_RED, min(MAX_RED, (budget / (2.0 ** e * g)) ** (1.0 / GAMMA)))
        return r, e, int(g)

    def _actions_for(self, red, exposure, gain, target) -> list[Action]:
        new_red, new_exposure, new_gain = target
        actions = []
        controls = {}
        if new_exposure != exposure:
            controls["exposure"] = new_exposure
        if new_gain != gain:
            controls["gain"] = new_gain
        if controls:
            actions.append(Action("camera", controls))
        if self.auto_light and abs(new_red - red) > max(0.004, 0.01 * red):
            actions.append(Action("red", new_red))
        return actions

    # ------------------------------------------------------------------ phases
    def _search(self, index, stats, red, exposure, gain):
        target = self._candidates[0]
        if exposure != target:
            gmin, _cap = self._gain_limits()
            if not self.results and self.auto_light:
                # First exposure: measure at full red (dimmed red has more lines of its own).
                plan = (MAX_RED, target, gmin)
            elif stats:
                plan = self.plan(stats, red, exposure, gain, [target])
            else:
                plan = (red, target, gmin)
            actions = self._actions_for(red, exposure, gain, plan) or [Action("camera", {"exposure": target})]
            if not any(a.kind == "camera" and "exposure" in a.value for a in actions):
                actions.append(Action("camera", {"exposure": target}))
            self.status = f"Finding the stripe-free exposure… trying {target} ({exposure_ms(target):g} ms)"
            self._wait(index)
            return actions
        p99 = float(stats.get("p99", 0.0))
        if _is_clipped(stats) or p99 > MEASURE_HIGH or p99 < MEASURE_LOW:
            plan = self.plan(stats, red, exposure, gain, [target])
            actions = self._actions_for(red, exposure, gain, plan)
            if actions:
                self._wait(index)
                return actions
            # Nothing left to adjust: measure as it is (judged too dark below if it is).
        self._samples.append(stats.get("stripes"))
        if len(self._samples) < MEASURE_FRAMES:
            return []
        measured = sorted(v for v in self._samples if v is not None)
        too_dark = p99 < 0.5 * LOW_P99 or len(measured) < len(self._samples) // 2 + 1
        self.results[target] = None if too_dark else measured[len(measured) // 2]
        gmin, _cap = self._gain_limits()
        self.brightness[target] = p99 / max(1e-6, (max(red, MIN_RED) ** GAMMA) * (gain / max(gmin, 1e-6)))
        self._candidates.pop(0)
        if self._verify is not None and target == self._verify:
            self._verify = None
            if self.results[target] is not None and self.results[target] <= CLEAN:
                self._candidates = []      # the remembered exposure is still clean: done
        self._samples = []
        if self._candidates:
            return self._search(index, stats, red, exposure, gain)
        return self._finish_search(index, stats, red, exposure, gain)

    def _finish_search(self, index, stats, red, exposure, gain):
        self.good = good_exposures(self.results)
        measured = ", ".join(f"{e}: {describe_lines(s)}" for e, s in sorted(self.results.items()))
        if not self.good:
            # Every candidate was too dark to judge: keep the usual stripe-free value for a 240 Hz DLP.
            low, high = self._range("exposure", (-13, -1))
            self.best_exposure = max(low, min(high, -4))
            self.good = [self.best_exposure]
            self.summary = (f"The picture was too dark at every exposure to measure lines ({measured}); using "
                            f"{self.best_exposure}. Turn the red light up and check the light reaches the chip.")
        else:
            self.best_exposure = self._brightest_fit(self.good)
            names = ", ".join(f"{e} ({exposure_ms(e):g} ms)" for e in self.good)
            best = min(s for s in self.results.values() if s is not None)
            if len(self.results) == 1:
                self.summary = f"Remembered exposure {self.good[0]} ({exposure_ms(self.good[0]):g} ms) is still line-free ({measured})."
            else:
                self.summary = f"Line-free: {names}. Lines measured: {measured}."
            if best > 2 * CLEAN:
                self.summary += (" Faint lines remain at every exposure; the stripe filter evens them out "
                                 "in the picture.")
        self.phase = "track"
        self._adjusting, self._outside = True, 0
        self.status = "Auto: adjusting…"
        self._wait(index)
        plan = self.plan(stats, red, exposure, gain, [self.best_exposure]) if stats else (red, self.best_exposure, gain)
        actions = self._actions_for(red, exposure, gain, plan)
        if exposure != self.best_exposure and not any(a.kind == "camera" and "exposure" in a.value for a in actions):
            actions.append(Action("camera", {"exposure": self.best_exposure}))
        return actions

    def _brightest_fit(self, good):
        """Of the stripe-free exposures, the one whose picture at full red and minimum gain was
        closest to the target brightness (the longest if all were dark, the shortest if all bright)."""
        scored = []
        for e in good:
            b = self.brightness.get(e)
            if b is not None and b > 0:
                scored.append((abs(math.log(b / TARGET_P99)) + (0.3 if b > HIGH_P99 else 0.0), e))
        return min(scored)[1] if scored else good[0]

    def _track(self, index, stats, red, exposure, gain):
        p99 = float(stats.get("p99", 0.0))
        clipped = _is_clipped(stats)
        settled_text = (f"Auto: settled · exposure {exposure} ({exposure_ms(exposure):g} ms) · red {red:.0%} · "
                        f"gain {gain}")
        if self._adjusting:
            if not clipped and AIM_LOW <= p99 <= AIM_HIGH:
                self._adjusting, self._outside = False, 0
                self.status = settled_text + self._red_note(red)
                return []
        else:
            if not clipped and LOW_P99 <= p99 <= HIGH_P99:
                self._outside = 0
                self.status = settled_text + self._red_note(red)
                return []
            self._outside += 1
            if self._outside < DEBOUNCE_FRAMES and stats.get("saturated", 0.0) < 0.05:
                return []
            self._adjusting = True
        plan = self.plan(stats, red, exposure, gain, self.allowed_exposures(exposure))
        actions = self._actions_for(red, exposure, gain, plan)
        if not actions:
            # Already at the limit in that direction: say so instead of adjusting forever.
            self._adjusting, self._outside = False, 0
            if clipped or p99 > HIGH_P99:
                self.status = settled_text + " · still too bright at the dimmest red."
            else:
                self.status = (settled_text + " · picture dim even at full red and the longest line-free "
                               "exposure; it is fine to focus like this.")
            return []
        self.status = "Auto: adjusting…"
        self._wait(index)
        return actions

    @staticmethod
    def _red_note(red):
        if red < MAX_RED - 0.01:
            return " · red dimmed because the picture is bright; faint lines can appear"
        return ""
