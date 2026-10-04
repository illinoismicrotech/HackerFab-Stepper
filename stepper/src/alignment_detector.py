"""Alignment-marker detection with the YOLO model, without PyTorch.

The model (ckpts/best.onnx, exported from the team's ckpts/best.pt) runs on OpenCV's DNN
module, which the app already ships, so detection works in the standalone download. If
only a .pt file is available and ultralytics is installed, that is used instead.

Pre-processing matches the original ultralytics code path exactly: the whole camera frame
is stretched to 640 x 640 and fed as RGB, so existing calibrations stay valid.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

INPUT_SIZE = 640
CONFIDENCE = 0.25   # ultralytics' default
IOU = 0.7           # ultralytics' default for overlapping boxes

Box = tuple[tuple[int, int], tuple[int, int]]


class OnnxMarkerDetector:
    """YOLO (v8/v11 detect head) on OpenCV DNN. Input: an RGB camera frame."""

    def __init__(self, path):
        self.path = str(path)
        self.net = cv2.dnn.readNetFromONNX(self.path)

    def detect(self, rgb: np.ndarray, confidence: float = CONFIDENCE) -> list[tuple[Box, float]]:
        height, width = rgb.shape[:2]
        blob = cv2.dnn.blobFromImage(rgb, 1 / 255.0, (INPUT_SIZE, INPUT_SIZE), swapRB=False, crop=False)
        self.net.setInput(blob)
        output = self.net.forward()            # (1, 4 + classes, candidates)
        predictions = output[0].T              # (candidates, 4 + classes)
        scores = predictions[:, 4:].max(axis=1)
        keep = scores > confidence
        predictions, scores = predictions[keep], scores[keep]
        if not len(scores):
            return []
        cx, cy, w, h = predictions[:, 0], predictions[:, 1], predictions[:, 2], predictions[:, 3]
        boxes = np.stack([cx - w / 2, cy - h / 2, w, h], axis=1)
        chosen = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), confidence, IOU)
        sx, sy = width / INPUT_SIZE, height / INPUT_SIZE
        found = []
        for i in np.array(chosen).reshape(-1)[:300]:
            x, y, bw, bh = boxes[i]
            box = ((int(x * sx), int(y * sy)), (int((x + bw) * sx), int((y + bh) * sy)))
            found.append((box, float(scores[i])))
        return found


class UltralyticsMarkerDetector:
    """The original PyTorch path, for a .pt model when ultralytics is installed."""

    def __init__(self, path):
        from ultralytics import YOLO
        self.path = str(path)
        self.model = YOLO(self.path)

    def detect(self, rgb: np.ndarray, confidence: float = CONFIDENCE) -> list[tuple[Box, float]]:
        height, width = rgb.shape[:2]
        # ultralytics treats NumPy images as BGR.
        resized = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), (INPUT_SIZE, INPUT_SIZE))
        result = self.model(resized, conf=confidence, iou=IOU, verbose=False)[0]
        sx, sy = width / INPUT_SIZE, height / INPUT_SIZE
        found = []
        for (x1, y1, x2, y2), score in zip(result.boxes.xyxy.tolist(), result.boxes.conf.tolist()):
            found.append((((int(x1 * sx), int(y1 * sy)), (int(x2 * sx), int(y2 * sy))), float(score)))
        return found


def load_marker_detector(model_path) -> Optional[object]:
    """Load the detector named in the config. A .pt path also finds a .onnx file beside it,
    which is preferred because it needs no PyTorch. Returns None (with a printed reason)
    when no usable model is found."""
    path = Path(model_path)
    names = [path.with_suffix(".onnx"), path] if path.suffix == ".pt" else [path]
    candidates = list(names)
    if not path.is_absolute():
        # The copy inside the standalone app, or the stepper/ folder when run from source.
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
        candidates += [base / name for name in names]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            if candidate.suffix == ".onnx":
                return OnnxMarkerDetector(candidate)
            return UltralyticsMarkerDetector(candidate)
        except ImportError:
            print(f"Cannot use {candidate}: it needs the 'ultralytics' package. Use the .onnx model instead.")
        except Exception as exc:
            print(f"Could not load the alignment model {candidate}: {exc}")
    if not any(c.exists() for c in candidates):
        print(f"Alignment model not found: {model_path}")
    return None


def marker_centers(boxes: list[Box], width: int, height: int) -> list[tuple[float, float]]:
    """Box centres as fractions of the frame (0..1)."""
    return [((x0 + x1) / 2 / width, (y0 + y1) / 2 / height) for (x0, y0), (x1, y1) in boxes]
