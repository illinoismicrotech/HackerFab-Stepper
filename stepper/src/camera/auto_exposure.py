"""Automatic camera exposure and projector red brightness.

Two jobs, run only while the projector shows red light (focus mode):

1. Find the stripe-free exposure. A DLP projector pulses its light; the camera's rolling
   shutter turns the pulses into stripes unless the exposure spans a whole number of pulse
   cycles. The cycle isn't known in advance, so the controller tries each exposure in
   CANDIDATES, measures how striped the image is, and keeps the best.
2. Keep the picture well exposed. Mainly by dimming/brightening the red image (smooth, and
   it doesn't disturb the stripe-free exposure), then by camera gain, and only as a last
   resort by changing exposure.

The controller is pure logic: feed it frame statistics, apply the actions it returns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CANDIDATES = (-6, -5, -4, -3)   # DirectShow units: 15.6, 31.3, 62.5, 125 ms
TARGET_P99 = 205                # brightest 1% of pixels: bright but not clipped
LOW_P99, HIGH_P99 = 165, 238
GAMMA = 2.2                     # projectors apply ~2.2 gamma: light ∝ pixel_value^2.2
SETTLE_FRAMES = 4               # frames to ignore after any change (camera + display latency)
MEASURE_FRAMES = 6              # frames averaged per stripe measurement
MIN_RED, MAX_RED = 0.02, 1.0


def exposure_ms(value: float) -> float:
    return 1000.0 * 2.0 ** value


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


def exposure_limits(low: float, high: float) -> tuple[int, int]:
    """Exposures the automatic brightness control may use: the stripe-search range, inside the
    camera's range. Shorter exposures only make the projector stripes worse (and once drove the
    picture to black), longer ones run past the frame time."""
    lo = max(int(low), min(CANDIDATES))
    hi = min(int(high), max(CANDIDATES))
    return (lo, hi) if lo <= hi else (int(low), int(high))


@dataclass
class Action:
    kind: str            # "red" (projector red brightness 0.02-1) or "camera" (dict of controls)
    value: object


@dataclass
class AutoCamera:
    ranges: dict = field(default_factory=dict)
    auto_exposure: bool = True     # search + adjust exposure/gain
    auto_light: bool = True        # adjust projector red brightness
    phase: str = "idle"            # idle | search | track | paused
    status: str = "Auto off"
    summary: str = ""              # result of the last stripe search
    results: dict = field(default_factory=dict)   # exposure -> stripe strength
    best_exposure: int | None = None
    _candidates: list = field(default_factory=list)
    _wait_until: int = -1
    _samples: list = field(default_factory=list)
    _last_index: int = -1

    # ------------------------------------------------------------------ helpers
    def _range(self, name, default):
        info = self.ranges.get(name) or {}
        return info.get("min", default[0]), info.get("max", default[1])

    def _wait(self, index):
        self._wait_until = index + SETTLE_FRAMES
        self._samples = []

    def start_search(self, index: int = 0):
        low, high = self._range("exposure", (-13, -1))
        self._candidates = [e for e in CANDIDATES if low <= e <= high] or [max(low, min(high, -4))]
        self.results = {}
        self.best_exposure = None
        self.summary = ""
        self.phase = "search"
        self._wait(index)
        self.status = "Finding the stripe-free exposure…"

    def pause(self, reason="Auto paused (projector not red)"):
        if self.phase in ("search", "track"):
            self._resume_phase = self.phase
            self.phase = "paused"
            self.status = reason

    def resume(self, index: int):
        if self.phase == "paused":
            self.phase = getattr(self, "_resume_phase", "track")
            self._wait(index)
            self.status = "Auto: adjusting…"

    def cancel_search(self):
        """Auto exposure was switched off: stop trying exposures, keep what the user set."""
        if self.phase == "search" or getattr(self, "_resume_phase", "") == "search":
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

    # ------------------------------------------------------------------ brightness
    def _light_actions(self, index, stats, red, exposure, gain, allow_exposure: bool) -> list[Action] | None:
        """Actions to bring p99 into the target band, or None when it already is."""
        p99, clipped = stats.get("p99", 0.0), stats.get("saturated", 0.0) > 0.002 or stats.get("p99", 0) >= 250
        gmin, gmax = self._range("gain", (100, 2239))
        gain_cap = min(gmax, gmin * 4)  # more gain than 4x mostly adds noise
        if not clipped and LOW_P99 <= p99 <= HIGH_P99:
            return None
        too_bright = clipped or p99 > HIGH_P99
        factor = 0.35 if clipped else (TARGET_P99 / max(p99, 1.0))  # wanted change in light
        actions = []
        if too_bright:
            if self.auto_exposure and gain > gmin:
                actions.append(Action("camera", {"gain": int(max(gmin, gain * max(factor, 0.5)))}))
            elif self.auto_light and red > MIN_RED + 1e-6:
                actions.append(Action("red", max(MIN_RED, red * factor ** (1 / GAMMA))))
            elif self.auto_exposure and allow_exposure:
                low, _ = exposure_limits(*self._range("exposure", (-13, -1)))
                if exposure > low:
                    actions.append(Action("camera", {"exposure": exposure - 1}))
                    self.status = "Image too bright even at the dimmest red; shortened the exposure (stripes possible)."
        else:
            if self.auto_light and red < MAX_RED - 1e-6:
                actions.append(Action("red", min(MAX_RED, red * min(factor, 4.0) ** (1 / GAMMA))))
            elif self.auto_exposure and gain < gain_cap:
                actions.append(Action("camera", {"gain": int(min(gain_cap, gain * min(factor, 2.0)))}))
            elif self.auto_exposure and allow_exposure:
                _, high = exposure_limits(*self._range("exposure", (-13, -1)))
                if exposure < high:
                    actions.append(Action("camera", {"exposure": exposure + 1}))
        if actions:
            self._wait(index)
        return actions

    # ------------------------------------------------------------------ phases
    def _search(self, index, stats, red, exposure, gain):
        target = self._candidates[0]
        if exposure != target:
            # Predict the red brightness for the new exposure so it lands near the target quickly.
            actions = [Action("camera", {"exposure": target, "gain": self._range("gain", (100, 2239))[0]})]
            if self.auto_light:
                actions.append(Action("red", max(MIN_RED, min(MAX_RED, red * (2.0 ** (exposure - target)) ** (1 / GAMMA)))))
            self.status = f"Finding the stripe-free exposure… trying {target} ({exposure_ms(target):g} ms)"
            self._wait(index)
            return actions
        adjust = self._light_actions(index, stats, red, exposure, gain, allow_exposure=False)
        if adjust:
            return adjust
        self._samples.append(stats.get("stripes"))
        if len(self._samples) < MEASURE_FRAMES:
            return []
        measured = sorted(v for v in self._samples if v is not None)
        too_dark = stats.get("p99", 0.0) < 0.5 * LOW_P99 or len(measured) < len(self._samples) // 2 + 1
        self.results[target] = None if too_dark else measured[len(measured) // 2]
        self._candidates.pop(0)
        if self._candidates:
            return self._search(index, stats, red, exposure, gain)
        self.best_exposure = choose_exposure(self.results)
        self.phase = "track"
        measured = ", ".join(f"{e}: {'too dark' if s is None else f'{s:.3f}'}" for e, s in sorted(self.results.items()))
        if self.best_exposure is None:
            # Every candidate was too dark to judge: keep the usual stripe-free value for a 240 Hz DLP.
            low, high = self._range("exposure", (-13, -1))
            self.best_exposure = max(low, min(high, -4))
            self.summary = (f"The picture was too dark at every exposure to measure stripes ({measured}); using "
                            f"{self.best_exposure}. Turn the red light up and check the light reaches the chip.")
        else:
            self.summary = (f"Least stripes at exposure {self.best_exposure} ({exposure_ms(self.best_exposure):g} ms): "
                            + measured)
        self.status = "Auto: adjusting…"
        self._wait(index)
        actions = []
        if exposure != self.best_exposure:
            actions.append(Action("camera", {"exposure": self.best_exposure}))
            if self.auto_light:
                actions.append(Action("red", max(MIN_RED, min(MAX_RED, red * (2.0 ** (exposure - self.best_exposure)) ** (1 / GAMMA)))))
        return actions

    def _track(self, index, stats, red, exposure, gain):
        actions = self._light_actions(index, stats, red, exposure, gain, allow_exposure=True)
        if actions is None:
            self.status = f"Auto: settled · exposure {exposure} ({exposure_ms(exposure):g} ms) · red {red:.0%} · gain {gain}"
            return []
        if not self.status.startswith("Image too bright"):
            self.status = "Auto: adjusting…"
        return actions
