# Alignment-marker detection and Auto-align

Using the app (exposures, focus, tiling, settings) is covered by the Help page (F1) and the
illustrated [how-to guide](HOW-TO-GUIDE.html). This page explains how alignment-marker detection
works and how to calibrate it for a station.

## The model

The app finds alignment marks with a small YOLO model (a fine-tuned 2.6M-parameter YOLOv11n).
The repository has it twice:

- `ckpts/best.onnx`: used by default. It runs on OpenCV's DNN module
  (`src/alignment_detector.py`), so neither the standalone app nor `run.bat` / `run.sh` needs
  PyTorch.
- `ckpts/best.pt`: the original training checkpoint. It needs the `ultralytics` package. A `.pt`
  path in the settings also picks up a `.onnx` file with the same name next to it.

To train a model on your own chips, see [model.md](model.md); it ends with exporting to ONNX.

## Using it

On the **Alignment** page:

- **Show detected markers on the camera picture** draws a green box around every mark the model
  finds (refreshed twice a second). `[alignment] enabled = true` turns this on at startup.
- **Auto-align stage** moves the stage so the marks land on their calibrated positions, and
  reports the move in the status line. Click it again to refine.

During **tiling** (Chip & map page), every tile after the first is corrected with the marks on
the edge it shares with the previous tile: left or right within a row (rows run in snake order),
the top edge for the first tile of a new row. Corrections are always stage moves.

## How the correction is computed

1. The model finds the marks in the camera picture. Each mark's centre is taken as a fraction of
   the frame (0 to 1), so the result does not depend on the camera resolution.
2. A mark in the right half of the frame is compared with `right_marker_x`, one in the left half
   with `left_marker_x`; likewise bottom/top with `bottom_marker_y` / `top_marker_y`. Those
   positions are pixels at `reference-width` × `reference-height` and are scaled to fractions too.
3. The difference times `x_scale_factor` / `y_scale_factor` is the stage move in µm. With several
   marks, the moves are averaged.

```toml
[alignment]
enabled = false
model_path = "ckpts/best.onnx"
reference-width = 1920     # camera resolution the positions below were measured at
reference-height = 1080
right_marker_x = 1820.0    # where marks on the right should be, in pixels
left_marker_x = 280.0
top_marker_y = 269.0
bottom_marker_y = 1075.0
x_scale_factor = -1100     # µm of stage movement for a mark moving one full frame width
y_scale_factor = 800       # µm for one full frame height
```

## Calibrating

The values above were measured on one station's camera and optics. Check them on yours before
relying on Auto-align or tiling.

1. **Marker positions.** Put a chip on the stage with a clean area in view and focus in red.
   Choose a pattern with alignment marks where you want them and click **Show it in red**
   (1 Pattern). Turn on **Show detected markers** to check they are found, then click
   **Snapshot**. Open the snapshot in an image viewer and read the pixel position of the centre
   of each mark. Enter them as `left_marker_x`, `right_marker_x`, `top_marker_y` and
   `bottom_marker_y`, and the snapshot's width and height as `reference-width` and
   `reference-height`.
2. **Scale factors.** Note a mark's position, move the stage by a known distance (for example
   100 µm in X with the stage pad) and note the new position. Then
   `x_scale_factor = stage move in µm ÷ (mark movement in pixels ÷ reference-width)`, and the same
   for Y with `reference-height`. Flip the sign if Auto-align moves the wrong way.
3. Save, restart, and click **Auto-align stage** a few times on a patterned chip: each click
   should make a smaller move.
