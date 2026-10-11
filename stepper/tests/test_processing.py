"""Image processing (focus score, stripe filter, statistics) and the automatic camera settings."""
import unittest

import support  # noqa: F401

import cv2
import numpy as np

from camera.auto_exposure import (CANDIDATES, HIGH_P99, LOW_P99, AutoCamera, choose_exposure, exposure_ms)
from camera.modes import (SENSOR_PRESETS, USB2_MODE, Mode, best_mode, dedupe_resolutions, ordered_candidates,
                          preset_modes, requested_mode)
from camera.processing import (FrameAverager, center_crop, compute_focus_score, destripe, downscale, image_stats,
                               stripe_strength)


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

    def test_candidates_cover_common_projector_rates(self):
        self.assertEqual(CANDIDATES, (-6, -5, -4, -3))
        self.assertAlmostEqual(exposure_ms(-4), 62.5)


if __name__ == "__main__":
    unittest.main()
