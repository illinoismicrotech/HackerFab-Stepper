"""Chip & map page: the exposure map, the chip record (which pattern went where) and tiling."""
from __future__ import annotations

import os
import tkinter
from pathlib import Path
from tkinter import StringVar, filedialog, messagebox

import cv2
import ttkbootstrap as ttk
from PIL import Image, ImageTk

from dispatcher import Event, EventDispatcher, MovementLock, RedFocusSource
from lib.img import image_to_tk_image
from ui_kit import add_tooltip
from ui_theme import palette, px


def confirm_stage_move(what: str, coords: dict) -> bool:
    where = ", ".join(f"{axis.upper()} {value:,.0f}" for axis, value in coords.items())
    return messagebox.askokcancel("Move the stage", f"Move to {where} µm ({what})?\n\n"
                                  "This moves several axes at once. Keep hands and tools clear of the stage.")


def describe_coords(exposure) -> str:
    x, y, z = exposure.coords
    note = " · stopped early" if exposure.aborted else ""
    return f"X {x:,.1f} · Y {y:,.1f} · Z {z:,.1f} µm · {exposure.duration / 1000:g} s{note}"


class ChipFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent)
        self.model = event_dispatcher
        self.path = StringVar()

        self.image_cache = dict()

        self.frame.columnconfigure(0, weight=1)
        file_row = ttk.Frame(self.frame)
        file_row.grid(row=0, column=0, sticky="ew")
        ttk.Label(file_row, text="Chip file").grid(row=0, column=0, sticky="w", padx=(0, px(8)))
        self.path_text = StringVar(value="not saved to a file yet: click Save as… to keep this record")
        self.path.trace_add("write", lambda *_: self.path_text.set(
            self.path.get() or "not saved to a file yet: click Save as… to keep this record"))
        ttk.Label(file_row, textvariable=self.path_text, style="Muted.TLabel", wraplength=px(620)).grid(
            row=0, column=1, sticky="w")
        self.chip_select_frame = ttk.Frame(self.frame)
        self.chip_select_frame.grid(row=1, column=0, sticky="w", pady=(px(8), 0))

        chip_types = [("Chip records", "*.json"), ("All files", "*.*")]

        def on_open():
            if not self.offer_to_save("Open a chip record"):
                return
            path = filedialog.askopenfilename(title="Open Chip", filetypes=chip_types)
            if not path:
                return
            try:
                self.model.load_chip(path)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                messagebox.showerror("Could not open chip", f"{path}\n\n{exc}")
                return
            self.path.set(path)

        def on_new():
            if not self.offer_to_save("Start a new chip record"):
                return
            path = filedialog.asksaveasfilename(title="Create Chip As", defaultextension=".json", filetypes=chip_types)
            if not path:
                return
            self.path.set(path)
            self.model.new_chip()
            self._save_quietly()

        def on_save():
            self.save_as()

        def on_finish_layer():
            print("Layer finished!")
            self.model.add_chip_layer()

        def on_delete_exposure():
            pair = self._selected_exposure()
            if pair is None:
                return
            yes = messagebox.askyesno(title="Delete Exposure", message="Are you sure you want to delete the selected exposure?")
            if yes:
                self.model.delete_chip_exposure(pair[0], pair[1])

        def on_select(e, cur):
            if cur and len(self.cur_layer_view.selection()) > 0:
                self.prev_layer_view.selection_set([])
                self.delete_exposure_button["state"] = "normal"
            elif not cur and len(self.prev_layer_view.selection()) > 0:
                self.cur_layer_view.selection_set([])
                self.delete_exposure_button["state"] = "normal"
            else:
                self.delete_exposure_button["state"] = "disabled"

        def on_double_click(cur):
            pair = self._selected_exposure()
            if pair is None or self.model.movement_lock != MovementLock.UNLOCKED:
                return  # nothing selected, or an exposure/autofocus is running
            x, y, z = self.model.chip.layers[pair[0]].exposures[pair[1]].coords
            target = {"x": x, "y": y, "z": z}
            if confirm_stage_move("where this exposure was made", target):
                self.model.move_absolute(target)

        def on_chip_changed():
            if len(self.model.chip.layers) < 2:
                self.prev_layer_select.configure(state="disabled")
                self.prev_layer_select.configure(to=0)
            else:
                self.prev_layer_select.configure(state="readonly")
                self.prev_layer_select.configure(to=len(self.model.chip.layers) - 2)
                self.prev_layer_select_var.set(str(len(self.model.chip.layers) - 2))
            self.refresh_prev_layer()
            self.refresh_cur_layer()
            self._save_quietly()
            if len(self.model.chip.layers[-1].exposures) > 0:
                self.finish_layer_button.configure(state="normal")
            else:
                self.finish_layer_button.configure(state="disabled")

        def prev_layer_index_changed(a, b, c):
            self.refresh_prev_layer()

        self.model.add_event_listener(Event.CHIP_CHANGED, on_chip_changed)

        buttons = [("Open…", on_open, "dark"), ("New…", on_new, "dark"), ("Save as…", on_save, "dark"),
                   ("Finish layer", on_finish_layer, "primary-outline"),
                   ("Delete exposure", on_delete_exposure, "danger-outline")]
        made = []
        for column, (text, command, style) in enumerate(buttons):
            button = ttk.Button(self.chip_select_frame, text=text, command=command, bootstyle=style)
            button.grid(row=0, column=column, padx=(0 if column == 0 else px(6), 0) if column != 3 else (px(24), 0))
            made.append(button)
        (self.open_chip_button, self.new_chip_button, self.save_chip_button, self.finish_layer_button,
         self.delete_exposure_button) = made
        self.finish_layer_button.configure(state="disabled")
        self.delete_exposure_button.configure(state="disabled")
        add_tooltip(self.finish_layer_button, "Start the next layer. The finished one appears under Earlier layer, "
                                              "so you can line the next pattern up with it.")

        self.layer_frame = ttk.Frame(self.frame)
        self.layer_frame.grid(row=2, column=0, sticky="ew", pady=(px(12), 0))
        self.layer_frame.columnconfigure(0, weight=1)
        # One table above the other: side by side they made the page wider than a laptop screen.
        self.cur_layer_frame = ttk.Frame(self.layer_frame)
        self.cur_layer_frame.grid(row=0, column=0, sticky="nsew")
        self.prev_layer_frame = ttk.Frame(self.layer_frame)
        self.prev_layer_frame.grid(row=1, column=0, sticky="nsew", pady=(px(12), 0))
        ttk.Label(self.cur_layer_frame, text="THIS LAYER", style="Caption.TLabel").grid(row=0, column=0, sticky="w")
        select_frame = ttk.Frame(self.prev_layer_frame)
        select_frame.grid(row=0, column=0, sticky="w")
        ttk.Label(select_frame, text="EARLIER LAYER", style="Caption.TLabel").grid(row=0, column=0, sticky="w")

        self.tree_view_style = ttk.Style()
        self.tree_view_style.configure("Treeview", rowheight=max(52, px(30)))

        def tree(parent):
            view = ttk.Treeview(parent, selectmode="browse", columns=("XYZ",), height=4)
            view.column("#0", width=px(100), stretch=False)
            view.column("XYZ", width=px(300), stretch=True)
            view.heading("#0", text="Pattern")
            view.heading("XYZ", text="Where and how long")
            view.grid(row=1, column=0, sticky="nsew", pady=(px(4), 0))
            parent.columnconfigure(0, weight=1)
            return view
        self.cur_layer_view = tree(self.cur_layer_frame)
        self.cur_layer_view.bind("<<TreeviewSelect>>", lambda e: on_select(e, cur=True))
        self.cur_layer_view.bind("<Double-1>", lambda e: on_double_click(cur=True))
        self.prev_layer_view = tree(self.prev_layer_frame)
        self.prev_layer_view.bind("<<TreeviewSelect>>", lambda e: on_select(e, cur=False))
        self.prev_layer_view.bind("<Double-1>", lambda e: on_double_click(cur=False))

        self.prev_layer_select_var = StringVar()
        self.prev_layer_select_var.trace_add("write", prev_layer_index_changed)
        self.prev_layer_select = ttk.Spinbox(select_frame, from_=0, to=0, textvariable=self.prev_layer_select_var,
                                             width=4)
        self.prev_layer_select.configure(state="disabled")
        self.prev_layer_select.grid(row=0, column=1, padx=(px(8), 0))
        ttk.Label(select_frame, text="(layer number)", style="Small.TLabel").grid(row=0, column=2, padx=(px(6), 0))
        ttk.Label(self.frame, text="Double-click an exposure to move the stage back to where it was made.",
                  style="Small.TLabel").grid(row=3, column=0, sticky="w", pady=(px(8), 0))

    def _save_quietly(self):
        """Save to the chip file after every change. A failure is reported, never raised: this
        runs right after an exposure and must not disturb anything else."""
        path = self.path.get()
        if not path:
            return
        try:
            self.model.save_chip(path)
        except OSError as exc:
            self.model.notify(f"Chip record NOT saved to {path}: {exc}. Use Save As to pick another place.", "bad")

    def _selected_exposure(self):
        cur_sel = self.cur_layer_view.selection()
        if len(cur_sel) > 0:
            layer_idx, ex_idx = cur_sel[0].split("_")
            return int(layer_idx), int(ex_idx)
        prev_sel = self.prev_layer_view.selection()
        if len(prev_sel) > 0:
            layer_idx, ex_idx = prev_sel[0].split("_")
            return int(layer_idx), int(ex_idx)
        return None

    def unsaved_exposures(self) -> int:
        """Exposures that exist only in memory (no chip file chosen yet)."""
        if self.path.get():
            return 0
        return sum(len(layer.exposures) for layer in self.model.chip.layers)

    def save_as(self) -> bool:
        path = filedialog.asksaveasfilename(title="Save chip record as", defaultextension=".json",
                                            filetypes=[("Chip records", "*.json"), ("All files", "*.*")])
        if not path:
            return False
        try:
            self.model.save_chip(path)
        except OSError as exc:
            messagebox.showerror("Chip record not saved", f"{path}\n\n{exc}")
            return False
        self.path.set(path)
        return True

    def offer_to_save(self, action: str) -> bool:
        """Ask before discarding unsaved exposures. False means the user cancelled."""
        count = self.unsaved_exposures()
        if count == 0:
            return True
        answer = messagebox.askyesnocancel(action, f"The current chip record has {count} exposure(s) that are not "
                                           "saved to a file. Save them first?")
        if answer is None:
            return False
        return self.save_as() if answer else True

    def _get_thumbnail(self, path: str):
        try:
            return self.image_cache[path][1]
        except KeyError:
            try:
                with Image.open(path) as source:
                    img = source.convert("RGB").resize((80, 45))
            except (OSError, ValueError):
                # Pattern file moved, deleted, or from another PC: show a grey placeholder.
                img = Image.new("RGB", (80, 45), "#5a646c")
            photo = image_to_tk_image(img)
            self.image_cache[path] = (img, photo)
            return photo

    def refresh_prev_layer(self):
        chip = self.model.chip

        for item in self.prev_layer_view.get_children():
            self.prev_layer_view.delete(item)

        try:
            idx = int(self.prev_layer_select_var.get())
        except ValueError:
            print(f"Leaving previous layer empty because select var is {self.prev_layer_select_var.get()!r}")
            return

        if len(chip.layers) < 2:
            return

        for i, ex in enumerate(chip.layers[idx].exposures):
            ex_id = f"{idx}_{i}"
            pos = describe_coords(ex)
            self.prev_layer_view.insert("", "end", ex_id, image=self._get_thumbnail(ex.path), values=(pos,))

    def refresh_cur_layer(self):
        chip = self.model.chip

        for item in self.cur_layer_view.get_children():
            self.cur_layer_view.delete(item)

        for i, ex in enumerate(chip.layers[-1].exposures):
            ex_id = f"{len(chip.layers) - 1}_{i}"
            pos = describe_coords(ex)
            self.cur_layer_view.insert("", "end", ex_id, image=self._get_thumbnail(ex.path), values=(pos,))


class OffsetAmountFrame:
    def __init__(self, parent, label, default_offset):
        self.frame = ttk.Labelframe(parent, text=label)

        offset_label = ttk.Label(self.frame, text="Offset (µm)")
        offset_label.grid(row=0, column=0)
        self.offset_var = StringVar(value=str(default_offset))
        self.offset_entry = ttk.Entry(self.frame, textvariable=self.offset_var, width=5)
        self.offset_entry.grid(row=0, column=1)
        amount_label = ttk.Label(self.frame, text="Amount")
        amount_label.grid(row=0, column=2)
        self.amount_var = StringVar(value="1")
        self.amount_spinbox = ttk.Spinbox(self.frame, from_=-20, to=20, textvariable=self.amount_var, width=3)
        self.amount_spinbox.grid(row=0, column=3)

class TilingFrame:
    def __init__(self, parent, model: EventDispatcher):
        self.frame = ttk.Frame(parent)
        self.model = model

        self.red_to_uv_offset = -40

        self.overall_pattern_size_w = 0
        self.overall_pattern_size_h = 0

        #Defaults set based on DLP471TP and a 10x objective
        #5.4 um Pixel Pitch
        #Width 10.368 mm
        #Height 5.832 mm
        # Move in X = 10.368mm / 10 = 1037um
        # Move in Y = 5.832 mm / 10 = 538.2 um ~ 539 um
        #Subtraction amounts are there to tune the offset for the alignment markers
        self.x_settings = OffsetAmountFrame(self.frame, "X", 1037-54) #Move amount between exposures in X
        self.y_settings = OffsetAmountFrame(self.frame, "Y", 539-27)  #Move amount between exposures in y

        #Tiling verisons of alignment
        def detect_alignment_markers_tiling(yolo_model, image, draw_rectangle=False, edge=None, edge_fraction=0.25):
            #Detects alignment markers and optionally filters detections by image edge(s).
            #yolo_model: YOLO model
            #image: image to detect on 
            #draw_rectangle: If True, draw rectangles
            #edge: 'left', 'right', 'top', or a list like ['left', 'right'] where markers are expected
                                                    #none means that markers are expect on all edges
            #edge_fraction: Fraction of width/height considered as edge region

            detections = []
            display_image = image.copy()
            try:
                image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                original_height, original_width = image_rgb.shape[:2]
                resized = cv2.resize(image_rgb, (640, 640))
                results = yolo_model(resized)
                boxes = results[0].boxes

                if isinstance(edge, str):
                    edge = [edge]  # allow single string or list

                for box in boxes:
                    x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                    x1 = int(x1 * original_width / 640)
                    x2 = int(x2 * original_width / 640)
                    y1 = int(y1 * original_height / 640)
                    y2 = int(y2 * original_height / 640)
                    x_center = (x1 + x2) / 2
                    y_center = (y1 + y2) / 2

                    # If edge filtering is enabled
                    if edge is not None:
                        if 'left' in edge and x_center > original_width * edge_fraction:
                            continue
                        if 'right' in edge and x_center < original_width * (1 - edge_fraction):
                            continue
                        if 'top' in edge and y_center > original_height * edge_fraction:
                            continue

                    detections.append(((x1, y1), (x2, y2)))
                    if draw_rectangle:
                        cv2.rectangle(display_image, (x1, y1), (x2, y2), (0, 255, 0), 3)

                print(f"Detected {len(detections)} marker(s)")
            except Exception as e:
                print(f"Detection failed: {e}")

            return detections, display_image

        def do_align_tiling(edge):
            if model.camera_image is None or model.model is None:
                messagebox.showinfo("Alignment unavailable", "Connect a live camera and load an alignment model first.")
                return
            #edge = ['left', 'right', 'top']
            h, w, _ = model.camera_image.shape

            # Detect markers on the left, right, and top edges
            markers, _ = detect_alignment_markers_tiling(model.model, model.camera_image, edge=edge)
            if len(markers) == 0:
                print("No markers detected.")
                return

            alignment = model.config.alignment
            dx, dy = 0.0, 0.0
            count_x, count_y = 0, 0

            for m in markers:
                xy0, xy1 = m
                x0, y0 = xy0
                x1, y1 = xy1
                x = (x0 + x1) / 2 / w
                y = (y0 + y1) / 2 / h

                # Horizontal alignment (left/right markers)
                if x < 0.5:
                    dx += alignment.x_scale_factor * (alignment.left_marker_x / w - x)
                    count_x += 1
                elif x > 0.5:
                    dx += alignment.x_scale_factor * (alignment.right_marker_x / w - x)
                    count_x += 1

                # Vertical alignment (top markers only)
                if y < 0.3:  # top region
                    dy += alignment.y_scale_factor * (alignment.top_marker_y / h - y)
                    count_y += 1

            # Average corrections based on detected edges
            if count_x > 0:
                dx /= count_x
            if count_y > 0:
                dy /= count_y

            # Move accordingly (if no top markers, dy=0)
            #If a small amount of alignment is needed move the image otherwise move the stage since we have far more percision in moving the image than the stage
            #The con of this is that large movements of the image result in cropping of the image
            #TODO calibrate the stage move threshold
            if abs(dx) < 10 and abs(dy) < 10:
                #move the image instead of the stage
                model.set_image_position(dx, dy, t=0)
            else:
              model.move_relative({'x': dx, 'y': dy})
              print(f"Alignment correction: dx={dx:.5f}, dy={dy:.5f} using {len(markers)} markers.")

        #function that takes in an arbitrary sized image composed of 3840x2160 tiles
        #with shared alignment marks that are 200 pixels from the edge
        def split_image_with_overlap(image_path,
                                          tile_width=3840,
                                          tile_height=2160,
                                          overlap_x=200,
                                          overlap_y=200,
                                          output_dir="tiles"):
            img = Image.open(image_path)
            # No tiles left over from another pattern. Only our own tile files are deleted.
            for old in Path(output_dir).glob("tile_*.png"):
                try:
                    old.unlink()
                except OSError:
                    pass
            img_w, img_h = img.size
            self.overall_pattern_size_w = img_w
            self.overall_pattern_size_h = img_h
            os.makedirs(output_dir, exist_ok=True)

            stride_x = tile_width - overlap_x
            stride_y = tile_height - overlap_y

            # Compute all top-left coordinates
            x_positions = []
            y_positions = []

            # Horizontal positions
            x = 0
            while True:
                if x + tile_width >= img_w:
                    x = max(0, img_w - tile_width)
                    x_positions.append(x)
                    break
                x_positions.append(x)
                x += stride_x

            # Vertical positions
            y = 0
            while True:
                if y + tile_height >= img_h:
                    y = max(0, img_h - tile_height)
                    y_positions.append(y)
                    break
                y_positions.append(y)
                y += stride_y

            tile_count = 0
            # Number of tiles, used when exposing
            self.tile_counts = (len(x_positions), len(y_positions))
            self.x_settings.amount_var.set(str(len(x_positions)))
            self.y_settings.amount_var.set(str(len(y_positions)))
            #Crop and Save the tile images
            for tile_id_y, top in enumerate(y_positions):
                for tile_id_x, left in enumerate(x_positions):
                    right = left + tile_width
                    bottom = top + tile_height

                    box = (left, top, right, bottom)
                    tile = img.crop(box)
                    tile.save(os.path.join(output_dir, f"tile_{tile_id_y},{tile_id_x}.png"))
                    tile_count += 1

            print(f"Saved {tile_count} tiles ({self.tile_counts[0]} x {self.tile_counts[1]}) to {output_dir}")
            return tile_count

        #function that patterns a single tile
        def pattern_for_tile(self, model, x_start, x_dir, x_idx, x_offset, y_start, y_dir, y_idx, y_offset, y_idx_max, x_idx_max, tile_dir="tiles"):
            if model.sequence_abort:
                return False
            #change image
            image_path = os.path.join(tile_dir, f"tile_{y_idx},{x_idx}.png")
            with Image.open(image_path) as source:
                current_tile = source.convert("RGB")
            model.set_pattern_image(current_tile, image_path)
            #move to the next position if not the first tile
            #the first tile is exposed where the operator(user of the stepper) places it
            if not (x_idx == 0 and y_idx == 0):
                self.model.move_absolute(
                    {
                        "x": x_start + x_dir * x_idx * x_offset,
                        "y": y_start + y_dir * y_idx * y_offset,
                    }
                )
            if model.sequence_abort:
                return False
            #Red autofocus
            self.model.autofocus(blue_only=False)
            if model.sequence_abort:
                return False

            #align to previous alignment marks if not first tile
            if not (x_idx == 0 and y_idx == 0):
                if x_idx != 0 and x_idx != x_idx_max:
                    if(y_idx % 2 == 0):
                        do_align_tiling('left')
                    else:
                        do_align_tiling('right')
                else:
                    do_align_tiling('top')



            #Do automatic offset for UV then autofocus
            self.model.move_relative({"z": self.red_to_uv_offset})
            self.model.non_blocking_delay(0.5)
            self.model.enter_uv_mode(mode_switch_autofocus=False)
            self.model.autofocus(blue_only=True)

            #expose the image
            if model.sequence_abort:
                self.model.move_relative({"z": -1 * self.red_to_uv_offset})
                return False
            exposed = self.model.begin_patterning()

            #TODO Add second exposure of the alignment markers
            # I tried doing this with a non blocking delay but didnt have success
            # I think that loading a pattern of the alignment marks that is hardcoded into the software might be the best bet

            #Offset back to red mode (not after STOP: STOP means projector off)
            if not model.sequence_abort:
                self.model.enter_red_mode(mode_switch_autofocus=False)
            self.model.move_relative({"z": -1 * self.red_to_uv_offset})
            if not exposed:
                self.stop_reason = "the exposure did not start"
                return False
            return not model.sequence_abort



        self.tile_counts = None
        self.tile_dir = None
        self.stop_reason = ""

        def segment():
            if not model.pattern_image_path:
                messagebox.showinfo("Segment images", "Choose the full pattern first (Operate → 1 Pattern).")
                return
            if model.busy_text():
                return
            tile_dir = str(Path(model.snapshot_directory) / "stepper_tiles")
            try:
                count = split_image_with_overlap(model.pattern_image_path, output_dir=tile_dir)
            except (OSError, ValueError) as exc:
                messagebox.showerror("Segment images", f"The pattern could not be split into tiles:\n\n{exc}")
                return
            self.tile_dir = tile_dir
            #load the first tile for operator placement
            model.set_red_focus_source(RedFocusSource.PATTERN)
            image_path = os.path.join(tile_dir, "tile_0,0.png")
            with Image.open(image_path) as source:
                current_tile = source.convert("RGB")
            model.set_pattern_image(current_tile, image_path)
            model.notify(f"Pattern split into {count} tiles ({self.tile_counts[0]} × {self.tile_counts[1]}). "
                         "Place the first tile, then Begin Tiling.", "ok")

        def number(var, what):
            try:
                return int(round(float(var.get())))
            except (TypeError, ValueError):
                raise ValueError(f"{what} must be a number") from None

        def on_begin():
            if self.tile_counts is None or self.tile_dir is None:
                messagebox.showinfo("Begin tiling", "Click Segment Images first: it splits the pattern into "
                                    "projector-sized tiles.")
                return
            if model.busy_text():
                messagebox.showinfo("Begin tiling", f"Wait for the {model.busy_text()} to finish.")
                return
            try:
                x_offset = number(self.x_settings.offset_var, "X offset")
                y_offset = number(self.y_settings.offset_var, "Y offset")
                x_sign = number(self.x_settings.amount_var, "X amount")
                y_sign = number(self.y_settings.amount_var, "Y amount")
            except ValueError as exc:
                messagebox.showerror("Begin tiling", str(exc))
                return
            x_amount, y_amount = self.tile_counts
            x_dir = -1 if x_sign < 0 else 1   # a negative Amount reverses the direction
            y_dir = -1 if y_sign < 0 else 1
            total = x_amount * y_amount
            seconds = (model.exposure_time or 0) / 1000
            if not messagebox.askokcancel("Begin tiling", f"Expose {total} tiles ({x_amount} × {y_amount}), "
                                          f"{seconds:g} s each?\n\nThe stage moves, focuses and exposes on its own. "
                                          "Keep hands clear. STOP (Esc) ends the run after the current step."):
                return

            x_start, y_start = self.model.stage_setpoint[0], self.model.stage_setpoint[1]
            self.stop_reason = ""
            model.set_sequence_busy(True)
            self.begin_tiling_button.configure(state="disabled")
            self.segment_images_button.configure(state="disabled")
            self.abort_tiling_button.configure(state="normal")
            done = 0
            try:
                #Move in Snake pattern with left to right on even rows and right to left on odd rows
                for y_idx in range(y_amount):
                    columns = range(x_amount) if y_idx % 2 == 0 else range(x_amount - 1, -1, -1)
                    for x_idx in columns:
                        if not pattern_for_tile(self, model, x_start, -x_dir, x_idx, x_offset, y_start, -y_dir, y_idx,
                                                y_offset, y_idx_max=y_amount, x_idx_max=x_amount, tile_dir=self.tile_dir):
                            raise StopIteration
                        done += 1
                        print(f"Patterned x_idx: {x_idx} y_idx: {y_idx}")
            except StopIteration:
                pass
            except Exception as exc:
                messagebox.showerror("Tiling stopped", f"Tiling stopped after {done} of {total} tiles:\n\n{exc}")
            finally:
                aborted = model.sequence_abort
                model.set_sequence_busy(False)
                self.begin_tiling_button.configure(state="normal")
                self.segment_images_button.configure(state="normal")
                self.abort_tiling_button.configure(state="disabled")
            if aborted:
                model.notify(f"Tiling stopped after {done} of {total} tiles.", "warn")
            elif done == total:
                model.notify(f"Tiling finished: {total} tiles exposed.", "ok")
            elif self.stop_reason:
                model.notify(f"Tiling stopped after {done} of {total} tiles: {self.stop_reason}.", "warn")

        def on_abort():
            model.sequence_abort = True
            model.stop_all()

        # Step 1 (split) must come before step 2 (begin): on_begin checks it.
        steps = ttk.Frame(self.frame)
        steps.grid(row=0, column=0, sticky="nw", padx=(0, px(24)))
        ttk.Label(steps, text="For patterns larger than one projector field: the pattern is split into "
                              "projector-sized tiles that are exposed one after another, moving the stage "
                              "between them.", style="Small.TLabel", wraplength=px(300), justify="left").grid(
            row=0, column=0, sticky="w", pady=(0, px(8)))
        _tbtn = dict(sticky="ew", pady=px(3))
        self.segment_images_button = ttk.Button(steps, text="1  Split the pattern into tiles",
                                                command=segment, bootstyle="dark")
        self.segment_images_button.grid(row=1, column=0, **_tbtn)
        self.begin_tiling_button = ttk.Button(steps, text="2  ▶  Begin tiling",
                                              command=on_begin, bootstyle="success")
        self.begin_tiling_button.grid(row=2, column=0, **_tbtn)
        self.abort_tiling_button = ttk.Button(steps, text="■  Stop tiling",
                                              command=on_abort, bootstyle="danger", state="disabled")
        self.abort_tiling_button.grid(row=3, column=0, **_tbtn)
        spacing = ttk.Frame(self.frame)
        spacing.grid(row=0, column=1, sticky="nw", padx=(0, px(24)))
        ttk.Label(spacing, text="STAGE MOVE BETWEEN TILES", style="Caption.TLabel").grid(row=0, column=0, sticky="w")
        self.x_settings.frame.grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        self.y_settings.frame.grid(row=2, column=0, sticky="w", pady=(px(6), 0))
        ttk.Label(spacing, text="Amount: number of tiles (set by step 1).\nA negative amount reverses the direction.",
                  style="Small.TLabel", justify="left").grid(row=3, column=0, sticky="w", pady=(px(6), 0))
        self.tiling_check_button = TilingCheckFrame(self.frame, model)
        self.tiling_check_button.frame.grid(row=0, column=2, sticky="nw")


class TilingCheckFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent)
        self.event_dispatcher = event_dispatcher

        ttk.Label(self.frame, text="CHECK THE CHIP", style="Caption.TLabel").grid(row=0, column=0, sticky="w")
        self.capture_button = ttk.Button(
            self.frame,
            text="Capture & Stitch Chip Images",
            command=self.capture_and_stitch,
            bootstyle="dark",
        )
        self.capture_button.grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        add_tooltip(self.capture_button, "Moves the stage over the area of the pattern, takes a camera picture at "
                                         "each spot and stitches them into one overview, with the pattern drawn "
                                         "on top.")

        self.preview_label = ttk.Label(self.frame)
        self.preview_label.grid(row=2, column=0, pady=(px(6), 0), sticky="nw")

        self.frame.rowconfigure(2, weight=1)
        self.frame.columnconfigure(0, weight=1)

    def capture_and_stitch(self):
        d = self.event_dispatcher
        if not d.pattern_image_path:
            messagebox.showinfo("Capture & stitch", "Choose the pattern first (Operate → 1 Pattern).")
            return
        if d.camera_image is None:
            messagebox.showinfo("Capture & stitch", "This needs a live camera picture.")
            return
        if d.busy_text():
            messagebox.showinfo("Capture & stitch", f"Wait for the {d.busy_text()} to finish.")
            return
        self.img = d.pattern_image
        self.img_w, self.img_h = self.img.size # 3840*2, 2160*2 # in pixels

        self.tile_width, self.tile_height = 3840, 2160 # in pixels, defined in TilingFrame

        # the distance that the stage will move in um
        stride_x, stride_y = 798, 448 # 1037 / 1.3 and 583 / 1.3
        # crop image pixels
        # each snapshot captured is 1920 x 1080 pixels
        crop_x, crop_y = 1477, 831 # 1920 / 1.3 and 1080 / 1.3
        # Disable button during capture
        self.capture_button.config(state='disabled', text="Capturing… (STOP cancels)")
        d.set_sequence_busy(True)
        try:
            stitched_image = self.takeAndStitchMapImages(stride_x, stride_y, crop_x, crop_y)
            if stitched_image:
                self.display_image(stitched_image, crop_x, crop_y)
                d.notify("Chip images captured and stitched.", "ok")
        except Exception as exc:
            messagebox.showerror("Capture & stitch", f"Stopped: {exc}")
        finally:
            d.set_sequence_busy(False)
            self.capture_button.config(state='normal', text="Capture & Stitch Chip Images")

    def display_image(self, pil_image, crop_x, crop_y):
        display_img = pil_image.copy()
        display_img = display_img.resize((crop_x//4, crop_y//4), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(display_img)
        self.preview_label.config(image=photo)
        self.preview_label.image = photo

    def capture_current_image(self):
        # Get the camera view from the event dispatcher
        if hasattr(self.event_dispatcher, 'camera_image') and self.event_dispatcher.camera_image is not None:
            camera_image = self.event_dispatcher.camera_image
            pil_image = Image.fromarray(camera_image)
            return pil_image
        else:
            print("No camera image available")
            return None

    def takeAndStitchMapImages(self, stride_x, stride_y, crop_x, crop_y):
        """
        Take snapshots num_cols * num_rows times, move in snake pattern
        Move in stride_x and y um in distance
        Crop all snapshots to crop_x, crop_y pixels in the center
        Paste them to the blank canvas
        Overlay the pattern image in the center
        """
        # calculate how many times the stage will move: num cols & rows
        total_x_um = self.img_w * 1037 / 3840
        total_y_um = self.img_h * 583 / 2160
        num_cols = int(total_x_um // stride_x)
        num_rows = int(total_y_um // stride_y)
        if num_cols * stride_x < total_x_um:
            num_cols += 1
        if num_rows * stride_y < total_y_um:
            num_rows += 1
        print("num_cols, num_rows: ", num_cols, num_rows)

        # large blank canvas
        stitched_width = num_cols * crop_x
        stitched_height = num_rows * crop_y
        stitched_image = Image.new('RGB', (stitched_width, stitched_height), color='black')

        # get starting position: center the chip
        orig_x, orig_y, orig_z = self.event_dispatcher.stage_setpoint
        try:
            return self._capture_grid(num_rows, num_cols, stride_x, stride_y, crop_x, crop_y, total_x_um, total_y_um,
                                      stitched_image, stitched_width, stitched_height)
        finally:
            # Always return to where it started, also after STOP or an error.
            try:
                self.event_dispatcher.move_absolute({"x": orig_x, "y": orig_y, "z": orig_z})
            except Exception as exc:
                print(f"Could not return to the start position: {exc}")

    def _capture_grid(self, num_rows, num_cols, stride_x, stride_y, crop_x, crop_y, total_x_um, total_y_um,
                      stitched_image, stitched_width, stitched_height):
        orig_x, orig_y, orig_z = self.event_dispatcher.stage_setpoint
        delta_x_um = (num_cols * stride_x - total_x_um) / 2
        delta_y_um = (num_rows * stride_y - total_y_um) / 2
        start_x = orig_x - delta_x_um
        start_y = orig_y - delta_y_um

        # move in snake pattern with left to right on even rows
        # and right to left on odd rows
        for row in range(num_rows):
            current_y = start_y + row * stride_y

            if row % 2 == 0:
                col_range = range(num_cols)
                first_x = start_x
            else:
                col_range = range(num_cols - 1, -1, -1)
                first_x = start_x + (num_cols - 1) * stride_x

            if self.event_dispatcher.sequence_abort:
                raise RuntimeError("stopped with STOP")
            # move to the next row
            self.event_dispatcher.move_absolute({
                "x": first_x,
                "y": current_y,
                "z": orig_z
            })
            self.event_dispatcher.non_blocking_delay(2)

            # columns in this row
            for idx, col in enumerate(col_range):
                current_x = start_x + col * stride_x
                if idx > 0: # idx = 0 first one don't need to move
                    self.event_dispatcher.move_absolute({
                        "x": current_x,
                        "y": current_y,
                        "z": orig_z
                    })
                    self.event_dispatcher.non_blocking_delay(2.5)

                if self.event_dispatcher.sequence_abort:
                    raise RuntimeError("stopped with STOP")
                captured_image = self.capture_current_image()
                if captured_image is None:
                    raise RuntimeError("the camera picture was lost")
                # crop
                orig_width, orig_height = captured_image.size
                # each snapshot captured is 1920 x 1080 pixels
                left = (orig_width - crop_x) // 2
                top = (orig_height - crop_y) // 2
                right = left + crop_x
                bottom = top + crop_y
                cropped_img = captured_image.crop((left, top, right, bottom))
                # paste
                x_pos = col * crop_x
                y_pos = (num_rows - 1 - row) * crop_y
                stitched_image.paste(cropped_img, (x_pos, y_pos))

                self.event_dispatcher.non_blocking_delay(0.5)
                self.frame.update()

        # Overlay the pattern image at center with 50% transparency
        pattern_img = self.img.copy().convert('RGBA') # pattern image
        # resize pattern image to match the snapshot's pixel
        # each snapshot has half of each tile's w and h
        pattern_img = pattern_img.resize((self.img_w//2, self.img_h//2), Image.Resampling.LANCZOS)
        pattern_img.putalpha(int(255 * 0.5))
        center_x = (stitched_width - self.img_w//2) // 2
        center_y = (stitched_height - self.img_h//2) // 2
        stitched_image = stitched_image.convert('RGBA')
        stitched_image.paste(pattern_img, (center_x, center_y), pattern_img)
        stitched_image = stitched_image.convert('RGB')

        return stitched_image


class MapFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent)
        self.event_dispatcher = event_dispatcher

        # Map dimensions in micrometers
        self.map_size_um = 10000.0  # 1 cm * 1 cm

        # Canvas size in pixels
        self.canvas_size = px(350)

        # Pattern dimensions: from DLP projector datasheet
        self.pattern_w = 1037
        self.pattern_h = 583

        self.center = (0.0, 0.0)   # µm at the middle of the map; follows the stage and the exposures

        self.canvas = tkinter.Canvas(
            self.frame,
            width=self.canvas_size,
            height=self.canvas_size,
            bg='#182632',
            highlightthickness=0,
        )
        self.canvas.grid(row=0, column=0)
        c = palette()
        legend = ttk.Frame(self.frame)
        legend.grid(row=1, column=0, sticky="w", pady=(px(6), 0))
        for color, text, filled in (('#E86E7F', 'projector field now', False), ('#7BB7B7', 'exposed', True)):
            swatch = tkinter.Canvas(legend, width=px(14), height=px(10), highlightthickness=0, background=c["bg"])
            swatch.create_rectangle(1, 1, px(13), px(9), outline=color, fill=color if filled else '', width=2)
            swatch.pack(side="left")
            ttk.Label(legend, text=text, style="Small.TLabel").pack(side="left", padx=(px(4), px(14)))
        self.scale_label = ttk.Label(legend, text="", style="Small.TLabel")
        self.scale_label.pack(side="left")

        # coordinates of exposed patterns (list of tuples: (x, y))
        self.pattern_markers = []

        event_dispatcher.add_event_listener(Event.STAGE_POSITION_CHANGED, self._on_position_changed)
        event_dispatcher.add_event_listener(Event.PATTERNING_FINISHED, self._on_pattern_exposed)
        event_dispatcher.add_event_listener(Event.CHIP_CHANGED, self._on_chip_changed)

        self._redraw_all()

    def _um_to_pixels(self, um_x, um_y):
        """
        Convert micrometer coordinates to canvas pixel coordinates.
        (0, 0) in micrometers is at the center of the canvas.
        """
        scale = self.canvas_size / self.map_size_um

        # Add half map size to shift the map centre to the middle of the canvas
        pixel_x = (um_x - self.center[0] + self.map_size_um / 2) * scale
        pixel_y = (um_y - self.center[1] + self.map_size_um / 2) * scale

        return pixel_x, pixel_y

    def _um_size_to_pixels(self, um_width, um_height):
        """Convert micrometer dimensions to pixel dimensions"""
        scale = self.canvas_size / self.map_size_um
        return um_width * scale, um_height * scale

    def _draw_pattern_marker(self, x_um, y_um):
        """ Draw a blue rectangle given x and y """
        x1_px, y1_px = self._um_to_pixels(x_um, y_um)
        w_px, h_px = self._um_size_to_pixels(self.pattern_w, self.pattern_h)

        x2_px = x1_px + w_px
        y2_px = y1_px + h_px

        # shift from bottom down to bottom up
        y1_px = self.canvas_size - y1_px
        y2_px = self.canvas_size - y2_px

        marker = self.canvas.create_rectangle(
            x1_px, y1_px, x2_px, y2_px,
            fill='#7BB7B7',
            width=0
        )

        return marker

    def _draw_current_position(self):
        """ Draw the red rectangle for current position. """
        x_um, y_um, z_um = self.event_dispatcher.stage_setpoint

        # Convert top-left corner to pixel coordinates
        x1_px, y1_px = self._um_to_pixels(x_um, y_um)

        # Get pattern dimensions in pixels
        w_px, h_px = self._um_size_to_pixels(self.pattern_w, self.pattern_h)

        # Calculate bottom-right corner
        x2_px = x1_px + w_px
        y2_px = y1_px + h_px

        y1_px = self.canvas_size - y1_px
        y2_px = self.canvas_size - y2_px        

        marker = self.canvas.create_rectangle(
                x1_px, y1_px, x2_px, y2_px,
                fill='',  # No fill
                outline='#E86E7F',  # Red outline
                width=2
            )

        return marker # marker ID

    def _load_patterns_from_chip(self):
        """ Load all exposed pattern coordinates from the chip
        into the self.pattern_markers list """

        self.pattern_markers.clear()

        chip = self.event_dispatcher.chip
        for layer in chip.layers:
            for exposure in layer.exposures:
                if not exposure.aborted:  # Only include successful exposures
                    x, y, z = exposure.coords
                    self.pattern_markers.append((x, y))

    def _redraw_all(self):
        """Redraw all exposed patterns from the chip"""
        # Clear existing pattern markers
        self.canvas.delete("all")

        # Fit the map around the exposures and the current position (1 cm, or larger if needed).
        x_now, y_now, _z = self.event_dispatcher.stage_setpoint
        xs = [x for x, _ in self.pattern_markers] + [x_now]
        ys = [y for _, y in self.pattern_markers] + [y_now]
        span = max(max(xs) - min(xs) + self.pattern_w, max(ys) - min(ys) + self.pattern_h)
        self.map_size_um = 10000.0 * max(1, int(span // 9000) + 1)
        self.center = ((max(xs) + min(xs) + self.pattern_w) / 2, (max(ys) + min(ys) + self.pattern_h) / 2)
        grid_step = 1000.0 * (self.map_size_um // 10000.0)
        x0 = self.center[0] - self.map_size_um / 2
        y0 = self.center[1] - self.map_size_um / 2
        first = (x0 // grid_step + 1) * grid_step
        for i in range(int(self.map_size_um // grid_step) + 1):
            gx, _ = self._um_to_pixels(first + i * grid_step, 0)
            self.canvas.create_line(gx, 0, gx, self.canvas_size, fill='#22374a')
        first = (y0 // grid_step + 1) * grid_step
        for i in range(int(self.map_size_um // grid_step) + 1):
            _, gy = self._um_to_pixels(0, first + i * grid_step)
            gy = self.canvas_size - gy
            self.canvas.create_line(0, gy, self.canvas_size, gy, fill='#22374a')
        self.scale_label.configure(text=f"grid: {grid_step / 1000:g} mm · map: {self.map_size_um / 10000:g} cm")

        # Draw all exposures from all layers
        for x_um, y_um in self.pattern_markers:
            self._draw_pattern_marker(x_um, y_um)

        self._draw_current_position()

    def _on_position_changed(self):
        self._redraw_all() # TODO: only update current_position?

    def _on_pattern_exposed(self):
        """ Get the most recent exposure from current layer """
        chip = self.event_dispatcher.chip
        if chip.layers and chip.layers[-1].exposures:
            latest_exposure = chip.layers[-1].exposures[-1]
            if not latest_exposure.aborted:
                x, y, z = latest_exposure.coords
                self.pattern_markers.append((x, y))
                self._redraw_all()

    def _on_chip_changed(self):
        """Handle chip changes (load, new chip, etc.) - reload and redraw"""
        self._load_patterns_from_chip()
        self._redraw_all()
