"""Image processing (focus score, stripe filter, statistics) and the automatic camera settings."""
import unittest

import support  # noqa: F401

import cv2
import numpy as np

from camera.auto_exposure import (CANDIDATES, HIGH_P99, LOW_P99, AutoCamera, choose_exposure, exposure_ms,
                                  good_exposures, refresh_hint)
from camera.modes import (SENSOR_PRESETS, USB2_MODE, Mode, best_mode, dedupe_resolutions, ordered_candidates,
                          preset_modes, requested_mode)
from camera.processing import (FrameAverager, center_crop, compute_focus_score, default_focus_level, destripe,
                               downscale, focus_measurements, focus_pyramid, image_stats, stripe_strength)
from camera.stripes import StripeTracker, analyse


def chip_image(blur=0, stripes=0.0, h=300, w=400):
    """A red-lit test target: bars and dots, optionally blurred and with horizontal bands."""
    img = np.zeros((h, w, 3), np.float32)
    img[:, :, 0] = 60
    for x in range(20, w, 40):
        img[40:h - 40, x:x + 12, 0] = 200
    for y in range(30, h, 50):
        img[y:y + 6, 10:w - 10, 0] = 160
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    if stripes:
        band = 1.0 + stripes * np.sin(np.arange(h) / 6.0)
        img *= band[:, None, None]
    return np.clip(img, 0, 255).astype(np.uint8)


class FocusScoreTests(unittest.TestCase):
    def test_sharper_scores_higher(self):
        scores = [compute_focus_score(chip_image(blur=b), False) for b in (0, 1.5, 3, 6)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_projector_stripes_do_not_fake_sharpness(self):
        blurred = compute_focus_score(chip_image(blur=4), False)
        blurred_striped = compute_focus_score(chip_image(blur=4, stripes=0.4), False)
        sharp = compute_focus_score(chip_image(blur=0), False)
        self.assertLess(blurred_striped, sharp)
        self.assertLess(abs(blurred_striped - blurred), 0.5 * (sharp - blurred))

    def test_blue_only_ignores_red_and_dark_is_zero(self):
        self.assertEqual(compute_focus_score(chip_image(), True), 0.0)
        self.assertEqual(compute_focus_score(np.zeros((40, 40, 3), np.uint8), False), 0.0)

    def test_center_crop_and_downscale(self):
        image = np.zeros((3648, 5472, 3), np.uint8)
        self.assertEqual(center_crop(image, 1200, 1200).shape[:2], (1200, 1200))
        self.assertEqual(downscale(image, 1600, 1200).shape[:2], (1067, 1600))
        small = np.zeros((100, 100, 3), np.uint8)
        self.assertIs(downscale(small, 1600, 1200), small)


class StripeTests(unittest.TestCase):
    def test_strength_measures_bands(self):
        clean, striped = chip_image(), chip_image(stripes=0.3)
        self.assertGreater(stripe_strength(striped), stripe_strength(clean) + 0.05)

    def test_destripe_flattens_bands_and_keeps_detail(self):
        flat = np.full((240, 320, 3), 0, np.uint8)
        flat[:, :, 0] = 150
        flat[:, 100:110, 0] = 230  # a vertical feature that must survive
        band = 1.0 + 0.3 * np.sin(np.arange(240) / 5.0)
        striped = np.clip(flat * band[:, None, None], 0, 255).astype(np.uint8)
        fixed = destripe(striped)
        before = np.abs(striped[:, :, 0].astype(int) - flat[:, :, 0]).mean()
        after = np.abs(fixed[:, :, 0].astype(int) - flat[:, :, 0]).mean()
        self.assertLess(after, before / 4)
        self.assertGreater(fixed[:, 105, 0].mean(), fixed[:, 50, 0].mean() + 50)

    def test_clean_image_returned_unchanged(self):
        image = np.full((64, 64, 3), 120, np.uint8)
        self.assertIs(destripe(image), image)


def stepper_view(lines=0.0, coverage=0.45, h=900, w=1350, seed=0, period=23.0):
    """Roughly what the camera shows: a lit chip between dark margins, a dark area above the chip
    edge, dark letters, noise, and (optionally) thin bright projector lines with a fixed spacing."""
    rng = np.random.default_rng(seed)
    img = np.full((h, w), 4.0, np.float32)
    x0 = int(w * (0.5 - coverage / 2))
    img[:, x0:x0 + int(w * coverage)] = 170.0
    img[: h // 8] *= 0.25                                   # chip edge: everything above it is dim
    for i in range(6):                                      # dark letters on the chip
        y = h // 5 + i * h // 8
        img[y:y + h // 14, x0 + 40:x0 + 90] = 40.0
    row = np.ones(h)
    if lines:
        phase = (np.arange(h) + rng.uniform(0, period)) % period
        row += lines * np.exp(-0.5 * (phase - period / 2) ** 2 / 1.5 ** 2)    # thin lines
    img = img * row[:, None] + rng.normal(0, 2.0, (h, w))
    out = np.zeros((h, w, 3), np.uint8)
    out[:, :, 2] = np.clip(img, 0, 255).astype(np.uint8)
    return out, row


class StripeSeparationTests(unittest.TestCase):
    """Lines are found by their fixed spacing, so the chip edge, letters and dark margins do not count."""

    def test_measures_lines_not_the_chip(self):
        clean = analyse(stepper_view()[0])
        striped = analyse(stepper_view(lines=0.12)[0])
        self.assertLess(clean.strength, 0.003)
        self.assertGreater(striped.strength, 0.015)
        self.assertAlmostEqual(striped.periods[0], 23.0, delta=0.5)

    def test_works_when_the_chip_covers_little_of_the_picture(self):
        for coverage in (0.25, 0.45, 0.9):
            image, row = stepper_view(lines=0.12, coverage=coverage)
            truth = float(np.std(np.log(row)))
            self.assertAlmostEqual(analyse(image).strength, truth, delta=0.25 * truth, msg=coverage)

    def test_filter_removes_lines_and_keeps_the_chip_edge(self):
        image, row = stepper_view(lines=0.12)
        clean, _ = stepper_view(seed=0)
        fixed = destripe(image)
        chip = slice(400, 950)

        def rows(a):
            return a[:, chip, 2].astype(float).mean(axis=1)
        # Row-to-row variation compared with the line-free picture (the filter may shift the overall
        # brightness slightly: it evens rows out to their average, lines included).
        before = np.std(rows(image)[150:] / rows(clean)[150:])
        after = np.std(rows(fixed)[150:] / rows(clean)[150:])
        self.assertLess(after, before / 4)
        edge = slice(90, 135)   # around the chip edge at row 112: still a sharp step afterwards
        self.assertGreater(np.ptp(fixed[edge, chip, 2].astype(float).mean(axis=1)), 100)

    def test_tracker_reuses_the_spacing_and_measures_clean_frames_against_it(self):
        tracker = StripeTracker()
        striped = tracker.analyse(stepper_view(lines=0.12)[0], now=0.0)
        clean = tracker.analyse(stepper_view(seed=3)[0], now=1.0)
        self.assertTrue(tracker.families)
        self.assertGreater(striped.strength, 10 * clean.strength)
        self.assertIsNone(clean.gains)        # nothing to correct in a clean frame


class FocusScaleTests(unittest.TestCase):
    def test_every_scale_and_the_dark_channel(self):
        frame = np.zeros((800, 1200, 3), np.uint8)
        frame[:, :, 2] = chip_image(h=800, w=1200)[:, :, 0]
        result = focus_measurements(frame)
        self.assertEqual(result["crop"], (600, 400))
        self.assertEqual(len(result["red"]), 6)
        self.assertEqual(result["blue"], [0.0] * 6)          # blue is dark under red light
        self.assertIsNone(result["red"][-1])                  # 1/32 of 600 px is too small
        self.assertEqual(default_focus_level(2736), 2)
        self.assertEqual(default_focus_level(600), 0)

    def test_coarse_scale_sees_focus_when_full_resolution_is_noise(self):
        rng = np.random.default_rng(0)
        base = chip_image(h=600, w=800)[:, :, 0].astype(np.float32)

        def view(blur):
            img = cv2.GaussianBlur(base, (0, 0), blur) + rng.normal(0, 8, base.shape)
            return np.clip(img, 0, 255).astype(np.uint8)
        sharp = [focus_pyramid(view(3.0)) for _ in range(3)]
        soft = [focus_pyramid(view(5.0)) for _ in range(3)]

        def separation(level):
            a, b = [x[level] for x in sharp], [x[level] for x in soft]
            return (np.mean(a) - np.mean(b)) / (np.ptp(a) + np.ptp(b) + 1e-9)
        self.assertGreater(separation(2), 3 * separation(0))


class StatsTests(unittest.TestCase):
    def test_percentiles_and_saturation(self):
        image = np.zeros((640, 640, 3), np.uint8)
        image[:, :, 0] = 100
        image[:160, :, 0] = 255          # a bright region a quarter of the picture tall
        stats = image_stats(image)
        self.assertAlmostEqual(stats["p50"], 100)
        self.assertEqual(stats["p99"], 255)
        self.assertAlmostEqual(stats["saturated"], 0.25, delta=0.05)

    def test_thin_stripe_lines_do_not_count_as_over_exposed(self):
        # What the camera saw on the stepper: a dark picture crossed by thin bright red lines.
        image = np.zeros((1067, 1600, 3), np.uint8)
        image[:, :, 2] = 12
        for y in range(0, 1067, 28):
            image[y:y + 3, :, 2] = 255
        stats = image_stats(image)
        self.assertEqual(stats["p99_raw"], 255)       # the raw picture looks clipped...
        self.assertEqual(stats["saturated"], 0.0)     # ...but it is not over-exposed
        self.assertLess(stats["p99"], LOW_P99)        # it is too dark

    def test_dark_picture_has_no_stripe_score(self):
        self.assertIsNone(stripe_strength(np.zeros((200, 300, 3), np.uint8)))

    def test_averaging(self):
        avg = FrameAverager(2)
        a, b = np.full((4, 4, 3), 100, np.uint8), np.full((4, 4, 3), 200, np.uint8)
        avg.add(a)
        self.assertEqual(int(avg.add(b)[0, 0, 0]), 150)
        avg.set_count(1)
        self.assertIs(avg.add(a), a)


class ModeTests(unittest.TestCase):
    def test_every_listed_sensor_mode_is_available(self):
        presets = {(m.width, m.height, m.fps) for m in preset_modes()}
        for entries in SENSOR_PRESETS.values():
            for entry in entries:
                self.assertIn(entry, presets)
        self.assertIn((4056, 3040, 10), presets)   # IMX477
        self.assertIn((4656, 3496, 9), presets)    # IMX519
        self.assertIn((5472, 3648, 9), presets)    # IMX283 (B0477)
        self.assertIn((USB2_MODE.width, USB2_MODE.height, USB2_MODE.fps), presets)

    def test_highest_resolution_at_lowest_frame_rate(self):
        modes = [Mode(1920, 1080, 60), Mode(5472, 3648, 15), Mode(5472, 3648, 9), Mode(3840, 2160, 20)]
        self.assertEqual(best_mode(modes), Mode(5472, 3648, 9))
        self.assertEqual(ordered_candidates(modes)[0], Mode(5472, 3648, 9))
        self.assertEqual([m.fps for m in dedupe_resolutions(modes) if m.width == 5472], [9])

    def test_requested_mode_only_when_manual(self):
        self.assertIsNone(requested_mode({"mode": "max", "width": 640}))
        self.assertIsNone(requested_mode({"mode": "auto"}))
        self.assertEqual(requested_mode({"mode": "manual", "width": 1920, "height": 1080, "fps": 60}).key(), "1920x1080@60")
        self.assertIn("20.0 MP", Mode(5472, 3648, 9).label())


class Scene:
    """Simulated camera looking at the projector's red light.

    The DLP cycles its LEDs `hz` times a second; a rolling-shutter exposure that is not a
    whole number of cycles shows stripes. Brightness follows red^2.2 x exposure x gain."""

    def __init__(self, hz, base=300.0):
        self.hz, self.base = hz, base

    def stats(self, red, exposure, gain):
        t = 2.0 ** exposure
        cycles = t * self.hz
        residual = abs(cycles - round(cycles))
        p99 = self.base * red ** 2.2 * (t / 0.0625) * gain / 100.0
        return {"p99": min(255.0, p99), "saturated": 0.05 if p99 >= 255 else 0.0,
                "stripes": 0.01 + 0.3 * residual / max(cycles, 1.0)}


def simulate(scene, frames=400, red=1.0, exposure=-6, gain=100, auto=None):
    auto = auto or AutoCamera(ranges={"exposure": {"min": -11, "max": -3}, "gain": {"min": 100, "max": 2239}})
    auto.start_search(0)
    for index in range(1, frames):
        for action in auto.update(index, scene.stats(red, exposure, gain), red, exposure, gain):
            if action.kind == "red":
                red = action.value
            else:
                exposure = action.value.get("exposure", exposure)
                gain = action.value.get("gain", gain)
    return auto, red, exposure, gain


class AutoCameraTests(unittest.TestCase):
    def test_finds_stripe_free_exposure_for_240_hz(self):
        auto, red, exposure, gain = simulate(Scene(240))
        self.assertEqual(auto.best_exposure, -4)
        self.assertEqual(exposure, -4)
        self.assertEqual(auto.phase, "track")
        p99 = Scene(240).stats(red, exposure, gain)["p99"]
        self.assertTrue(LOW_P99 <= p99 <= HIGH_P99, p99)
        self.assertIn("settled", auto.status)

    def test_finds_stripe_free_exposure_for_120_hz(self):
        auto, red, exposure, gain = simulate(Scene(120))
        self.assertEqual(auto.best_exposure, -3)
        self.assertEqual(exposure, -3)

    def test_dim_scene_raises_light_then_gain(self):
        auto, red, exposure, gain = simulate(Scene(240, base=40.0), frames=600)
        self.assertEqual(red, 1.0)
        self.assertGreater(gain, 100)
        p99 = Scene(240, base=40.0).stats(red, exposure, gain)["p99"]
        self.assertGreaterEqual(p99, LOW_P99)

    def test_too_dark_exposures_are_never_chosen(self):
        self.assertEqual(choose_exposure({-6: None, -5: .20, -4: .05, -3: None}), -4)
        self.assertIsNone(choose_exposure({-6: None, -5: None}))

    def test_search_skips_exposures_too_dark_to_measure(self):
        auto = AutoCamera(ranges={"exposure": {"min": -11, "max": -3}, "gain": {"min": 100, "max": 2239}})
        auto.start_search(0)
        exposure, red, gain = -6, 1.0, 100
        for index in range(1, 400):
            t = 2.0 ** exposure
            p99 = min(255.0, 30.0 * t / 0.0625)              # dim scene: -6 and -5 stay dark
            stats = {"p99": p99, "saturated": 0.0, "stripes": None if p99 < 20 else 0.02}
            for action in auto.update(index, stats, red, exposure, gain):
                if action.kind == "red":
                    red = action.value
                else:
                    exposure = action.value.get("exposure", exposure)
                    gain = action.value.get("gain", gain)
            if auto.phase == "track":
                break
        self.assertIsNone(auto.results[-6])
        self.assertNotIn(auto.best_exposure, (-6, -5))

    def test_brightness_control_never_drives_exposure_to_black(self):
        # A picture that keeps reading "clipped" must not push the exposure below the search range.
        auto = AutoCamera(ranges={"exposure": {"min": -13, "max": -1}, "gain": {"min": 100, "max": 2239}})
        auto.phase, auto.best_exposure = "track", -3
        exposure, red, gain = -3, 1.0, 100
        for index in range(1, 500):
            for action in auto.update(index, {"p99": 255, "saturated": 0.3, "stripes": 0.05}, red, exposure, gain):
                if action.kind == "red":
                    red = action.value
                else:
                    exposure = action.value.get("exposure", exposure)
                    gain = action.value.get("gain", gain)
        self.assertGreaterEqual(exposure, min(CANDIDATES))

    def test_choose_prefers_shorter_only_when_equally_good(self):
        self.assertEqual(choose_exposure({-6: .03, -5: .03, -4: .01, -3: .01}), -4)
        self.assertEqual(choose_exposure({-6: .548, -5: .544, -4: .480, -3: .542}), -4)
        self.assertEqual(choose_exposure({-6: .010, -5: .011, -4: .012, -3: .010}), -6)

    def test_pause_and_resume(self):
        auto = AutoCamera()
        auto.start_search(0)
        auto.pause()
        self.assertEqual(auto.phase, "paused")
        self.assertEqual(auto.update(10, {"p99": 300, "stripes": 0.1}, 1.0, -6, 100), [])
        auto.resume(10)
        self.assertEqual(auto.phase, "search")
        self.assertEqual(auto.update(11, {"p99": 200, "stripes": 0.1}, 1.0, -6, 100), [])  # settling

    def test_keeps_full_red_and_uses_longer_exposure_before_gain(self):
        # A dim scene on a 240 Hz projector with the Arducam's small gain range (4-8).
        ranges = {"exposure": {"min": -10, "max": -2}, "gain": {"min": 4, "max": 8}}
        scene = Scene(240, base=45.0)
        auto = AutoCamera(ranges=ranges)
        auto.start_search(0)
        red, exposure, gain = 1.0, -6, 4
        for index in range(1, 400):
            stats = scene.stats(red, exposure, gain * 25)
            for action in auto.update(index, stats, red, exposure, gain):
                if action.kind == "red":
                    red = action.value
                else:
                    exposure = action.value.get("exposure", exposure)
                    gain = action.value.get("gain", gain)
        self.assertEqual(auto.good, [-4, -3])
        self.assertEqual(red, 1.0)
        self.assertEqual(exposure, -3)          # longest line-free exposure first ...
        self.assertGreater(gain, 4)             # ... then gain
        self.assertTrue(LOW_P99 <= scene.stats(red, exposure, gain * 25)["p99"] <= HIGH_P99)

    def test_exposure_never_leaves_the_line_free_set_and_does_not_hunt(self):
        ranges = {"exposure": {"min": -10, "max": -2}, "gain": {"min": 4, "max": 8}}
        auto = AutoCamera(ranges=ranges)
        auto.phase, auto.good, auto.best_exposure = "track", [-4, -3], -4
        red, exposure, gain, changes = 1.0, -4, 4, 0
        for index in range(1, 600):
            brightness = 160.0 * (1 + 0.2 * np.sin(index / 20.0))     # stage moving over the chip
            p99 = min(255.0, brightness * red ** 2.2 * 2 ** (exposure + 4) * gain / 4)
            actions = auto.update(index, {"p99": p99, "saturated": 0.0, "stripes": 0.002}, red, exposure, gain)
            changes += bool(actions)
            for action in actions:
                if action.kind == "red":
                    red = action.value
                else:
                    exposure = action.value.get("exposure", exposure)
                    gain = action.value.get("gain", gain)
            self.assertIn(exposure, (-4, -3))
        self.assertLess(changes, 25)            # a few gain steps per sweep, no back-and-forth every frame
        self.assertEqual(red, 1.0)

    def test_gain_steps_always_make_progress(self):
        auto = AutoCamera(ranges={"gain": {"min": 4, "max": 8}})
        auto.phase, auto.good, auto.best_exposure = "track", [-3], -3
        _red, _e, gain = auto.plan({"p99": 140.0, "saturated": 0.0}, 1.0, -3, 4, [-3])
        self.assertEqual(gain, 6)   # 4 x 200/140 = 5.7: rounds to a real step, never stays at 4

    def test_good_set_and_refresh_advice(self):
        self.assertEqual(good_exposures({-6: .03, -5: .03, -4: .002, -3: .001}), [-4, -3])
        self.assertEqual(good_exposures({-6: None, -5: None}), [])
        self.assertIn("48 Hz", refresh_hint(60))
        self.assertEqual(refresh_hint(48), "")
        self.assertEqual(refresh_hint(23), "")      # 23.976 Hz: 125 ms is 3 frames
        self.assertEqual(refresh_hint(0), "")

    def test_candidates_cover_common_projector_rates(self):
        self.assertEqual(CANDIDATES, (-6, -5, -4, -3))
        self.assertAlmostEqual(exposure_ms(-4), 62.5)


if __name__ == "__main__":
    unittest.main()
