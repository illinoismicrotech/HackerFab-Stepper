# Stepper Software Guide

This is a guide to the photolithography stepper software contained in this repository.

## Overview

TODO

## Quick Start

TODO

## Configuration

TODO

## Tiling

TODO

## Autofocus

TODO

## Detection

The GUI detects alignment markers with a YOLO model (a fine-tuned 2.6M-parameter YOLOv11n).
The repository includes it twice:

- `ckpts/best.onnx`: used by default. It runs on OpenCV's DNN module (`src/alignment_detector.py`),
  so neither the standalone app nor `run.bat` / `run.sh` needs PyTorch.
- `ckpts/best.pt`: the original training checkpoint. It needs the `ultralytics` package. A `.pt`
  path in the config also picks up a `.onnx` file with the same name next to it.

To train a model on your own data, see [this guide](model.md); it ends with exporting the result to ONNX.

```toml
[alignment]
# Start with real-time detection of alignment markers on
enabled = false
# The marker model
model_path = "ckpts/best.onnx"
```

The **Detect alignment markers in real time** checkbox (Image alignment page) draws the detections
on the camera preview, refreshed twice a second.

## Alignment

The alignment system allows for automatic positioning of chips on the stage for layered patterning. The system uses the YOLO model to detect alignment markers and calculates the necessary stage movements to align the patterns precisely.

#### How Alignment Works

1. The system detects alignment markers in the camera image using the model specified in the configuration.
2. For each detected marker, the system calculates its position relative to predefined reference coordinates.
3. Based on these differences, the system computes the necessary stage movements to align the chip correctly.

#### Alignment Parameters

```toml
[alignment]
# ...
# Alignment marker reference coordinates (in pixels)
right_marker_x = 1820.0  # x-coordinate for markers on the right side
top_marker_y = 269.0     # y-coordinate for markers on the top
bottom_marker_y = 1075.0 # y-coordinate for markers on the bottom
left_marker_x = 280.0    # x-coordinate for markers on the left side
# Scaling factors for converting normalized differences to stage movements (in µm)
x_scale_factor = -1100   # Scaling factor for x-axis movements
y_scale_factor = 800     # Scaling factor for y-axis movements
```

The marker coordinates are pixels at the camera resolution given by `reference-width` and
`reference-height` (default 1920 × 1080). They are scaled automatically when the camera runs at a
different resolution, so record the resolution you calibrate at.

**Auto-align** moves the stage once and reports the move; click it again to refine. During **tiling**,
each tile after the first is corrected using only the marks on the edge shared with the previous
tile (left, right or top, following the snake order).

#### Calibrating Alignment Parameters

The coordinates represent the "ideal" positions where markers should be in the camera frame when the chip is perfectly aligned. To calibrate the alignment parameters, we can use our model to detect projected alignment markers on a clean area of a chip:

1. **Reference Coordinates**: Place a chip on the stage. Move the stage so that an unpatterned area of the chip is on view and ensure the stepper is in red focus mode. Enable real-time detection. Then select and project a pattern that has the alignment markers in the desired position. The GUI should output detection positions in the terminal. Note the pixel coordinates and update the reference coordinate values in the config accordingly.

2. **Scaling Factors**: These values depend on your camera's field of view and the mechanical properties of your stage. To calibrate:
   - Move the stage by a known distance (e.g., 100 µm)
   - Measure how many pixels the markers moved in the camera image
   - Calculate the scaling factor as: `(stage movement in µm) / (marker movement in normalized coordinates)`
   - Adjust the sign (positive or negative) based on the relative directions of stage movement and camera coordinates
