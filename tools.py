#!/usr/bin/env python3
"""The MCP tool surface for AutoCAD 2D drafting.

Every tool runs its AutoCAD work on the COM bridge's worker thread and returns
a short string. Successful draws report the entity handle so the model can
refer back to what it made; failures return a sentence explaining what to do
about it rather than raising.

Coordinates are drawing units, 2D (Z is always 0). Angles are degrees.
"""

from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Image

import config
from acad import (
    AcadError,
    bridge,
    deg2rad,
    dispatches,
    doubles,
    entity_summary,
    flatten_points,
    point,
)

# Raster plotter shipped with AutoCAD, used to render the view as a picture.
PNG_PLOTTER = "PublishToWeb PNG.pc3"

# AutoCAD colour indices by name, for a friendlier layer API.
COLORS = {
    "red": 1, "yellow": 2, "green": 3, "cyan": 4, "blue": 5,
    "magenta": 6, "white": 7, "black": 7, "grey": 8, "gray": 8,
    "lightgrey": 9, "lightgray": 9,
}


def _color_index(color: str | int | None) -> int | None:
    if color is None or color == "":
        return None
    if isinstance(color, int):
        return max(1, min(255, color))
    text = str(color).strip().lower()
    if text.isdigit():
        return max(1, min(255, int(text)))
    if text in COLORS:
        return COLORS[text]
    raise AcadError(
        f"Unknown colour '{color}'. Use a name ({', '.join(sorted(COLORS))}) "
        "or an AutoCAD colour index from 1 to 255."
    )


def _safe_output_path(filename: str, extension: str) -> Path:
    """Resolve filename inside the one folder the server may write to."""
    candidate = Path(str(filename).strip().strip('"').strip("'"))
    # anchor, not is_absolute(): on Windows "\dir\x" and "C:x" are not absolute,
    # yet joining either onto SAVE_DIR throws away part of SAVE_DIR.
    if candidate.anchor or ".." in candidate.parts:
        candidate = Path(candidate.name)
    if not candidate.name:
        raise AcadError("A file name is required.")
    if candidate.suffix.lower() != extension:
        candidate = candidate.with_suffix(extension)

    resolved = (config.SAVE_DIR / candidate).resolve()
    # A path comparison, not a string prefix: "...\Out-other" starts with
    # "...\Out" but is a different folder.
    if not resolved.is_relative_to(config.SAVE_DIR):
        raise AcadError(f"Files can only be written inside {config.SAVE_DIR}.")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _points_from(raw: Any, what: str = "points") -> list[list[float]]:
    """Accept [[x,y],...] or a JSON string of the same (models send both)."""
    value = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AcadError(
                f"Could not read {what}: expected [[x, y], [x, y], ...]. ({exc})"
            ) from exc
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise AcadError(f"{what} needs at least 2 points, as [[x, y], [x, y], ...].")
    return [list(p) for p in value]


def register(mcp) -> None:
    """Attach every tool to the given FastMCP server."""

    # ------------------------------------------------------------------
    # Drawing primitives
    # ------------------------------------------------------------------

    @mcp.tool()
    def draw_hatch(
        boundary_handle: str, pattern: str = "ANSI31", scale: float = 1.0
    ) -> str:
        """Fill a closed shape with a hatch pattern.

        boundary_handle must be a *closed* polyline, circle or ellipse -- use the
        handle a draw tool returned. Common patterns: ANSI31 (diagonal), SOLID,
        EARTH, GRAVEL, BRICK, HONEY, DOTS, NET.
        """
        def work(acad):
            boundary = acad.entity_by_handle(boundary_handle)
            hatch = acad.ms.AddHatch(0, str(pattern).upper(), True)
            hatch.AppendOuterLoop(dispatches([boundary]))
            if scale and scale != 1.0:
                hatch.PatternScale = float(scale)
            hatch.Evaluate()
            return hatch.Handle
        try:
            handle = bridge.run(work, config.COM_TIMEOUT)
        except AcadError as exc:
            raise AcadError(
                f"{exc} Hatching needs a closed boundary -- if you drew the shape "
                "with draw_polyline, pass closed=True."
            ) from exc
        return f"Hatched {boundary_handle} with {pattern}. handle={handle}"

    # ------------------------------------------------------------------
    # Dimensions
    # ------------------------------------------------------------------

    @mcp.tool()
    def dim_linear(
        x1: float, y1: float, x2: float, y2: float,
        text_x: float, text_y: float, vertical: bool = False,
    ) -> str:
        """Dimension the horizontal (or vertical) distance between two points.

        (text_x, text_y) is where the dimension line sits -- offset it clear of
        the geometry. Set vertical=True to measure height instead of width.
        """
        def work(acad):
            import math
            rotation = math.pi / 2 if vertical else 0.0
            dim = acad.ms.AddDimRotated(
                point(x1, y1), point(x2, y2), point(text_x, text_y), rotation
            )
            return dim.Handle
        handle = bridge.run(work, config.COM_TIMEOUT)
        axis = "vertical" if vertical else "horizontal"
        return (
            f"{axis.capitalize()} dimension from ({x1}, {y1}) to ({x2}, {y2}). "
            f"handle={handle}"
        )

    @mcp.tool()
    def dim_aligned(
        x1: float, y1: float, x2: float, y2: float, text_x: float, text_y: float
    ) -> str:
        """Dimension the true point-to-point distance, parallel to the measured line."""
        def work(acad):
            dim = acad.ms.AddDimAligned(
                point(x1, y1), point(x2, y2), point(text_x, text_y)
            )
            return dim.Handle
        handle = bridge.run(work, config.COM_TIMEOUT)
        return f"Aligned dimension from ({x1}, {y1}) to ({x2}, {y2}). handle={handle}"

    # ------------------------------------------------------------------
    # Layers
    # ------------------------------------------------------------------

    @mcp.tool()
    def create_layer(name: str, color: str = "white", make_current: bool = True) -> str:
        """Create a layer (or update its colour if it already exists).

        Colour may be a name -- red, yellow, green, cyan, blue, magenta, white,
        grey -- or an AutoCAD colour index from 1 to 255.
        """
        index = _color_index(color)

        def work(acad):
            layer = acad.doc.Layers.Add(str(name))
            if index is not None:
                layer.Color = index
            if make_current:
                acad.doc.ActiveLayer = layer
            return layer.Name
        created = bridge.run(work, config.COM_TIMEOUT)
        suffix = " and made current" if make_current else ""
        return f"Layer '{created}' ready in {color}{suffix}."

    @mcp.tool()
    def set_current_layer(name: str) -> str:
        """Make an existing layer current, so new geometry lands on it."""
        def work(acad):
            for layer in acad.doc.Layers:
                if layer.Name.lower() == str(name).strip().lower():
                    acad.doc.ActiveLayer = layer
                    return layer.Name
            names = ", ".join(sorted(l.Name for l in acad.doc.Layers))
            raise AcadError(
                f"No layer called '{name}'. This drawing has: {names}. "
                "Use create_layer to make a new one."
            )
        return f"Layer '{bridge.run(work, config.COM_TIMEOUT)}' is now current."

    # ------------------------------------------------------------------
    # Query and edit
    # ------------------------------------------------------------------

    @mcp.tool()
    def list_entities(layer: str = "", limit: int = 100) -> str:
        """List what is in the drawing: handle, type, layer and extents.

        Pass a layer name to list only that layer. Use the handles it returns
        with get_entity_info, erase_entity or draw_hatch.
        """
        wanted = str(layer).strip().lower()

        def work(acad):
            model = acad.ms
            total = model.Count
            rows, shown = [], 0
            for i in range(total):
                if shown >= max(1, min(int(limit), 500)):
                    break
                item = model.Item(i)
                try:
                    item_layer = item.Layer
                except Exception:  # noqa: BLE001
                    item_layer = "?"
                if wanted and item_layer.lower() != wanted:
                    continue
                info = entity_summary(item)
                bounds = info.get("bounds")
                extent = (
                    f" [{bounds['min']} to {bounds['max']}]" if bounds else ""
                )
                rows.append(
                    f"  {info.get('handle', '?')}  "
                    f"{info.get('type', '?').replace('AcDb', '')}  "
                    f"on {item_layer}{extent}"
                )
                shown += 1
            return total, rows
        total, rows = bridge.run(work, config.COM_TIMEOUT)
        if not rows:
            scope = f" on layer '{layer}'" if layer else ""
            return f"The drawing has {total} entities, none{scope}."
        header = f"{len(rows)} of {total} entities"
        header += f" on layer '{layer}'" if layer else ""
        return header + ":\n" + "\n".join(rows)

    @mcp.tool()
    def get_entity_info(handle: str) -> str:
        """Look up one entity by handle: type, layer, extents, area, length, radius."""
        def work(acad):
            return entity_summary(acad.entity_by_handle(handle))
        info = bridge.run(work, config.COM_TIMEOUT)
        return json.dumps(info, indent=2)

    @mcp.tool()
    def erase_entity(handle: str) -> str:
        """Delete one entity from the drawing. This cannot be undone from here."""
        def work(acad):
            entity = acad.entity_by_handle(handle)
            kind = entity.ObjectName.replace("AcDb", "")
            entity.Delete()
            return kind
        kind = bridge.run(work, config.COM_TIMEOUT)
        return f"Deleted {kind} {handle}."

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    @mcp.tool()
    def get_drawing_info() -> str:
        """Report the open drawing: name, path, units, layers, and entity count."""
        def work(acad):
            doc = acad.doc
            try:
                insunits = int(doc.GetVariable("INSUNITS"))
            except Exception:  # noqa: BLE001
                insunits = -1
            unit_names = {
                0: "unitless", 1: "inches", 2: "feet", 4: "millimetres",
                5: "centimetres", 6: "metres", 9: "microns", 14: "decimetres",
            }
            current = doc.ActiveLayer.Name
            layers = []
            for layer in doc.Layers:
                entry = f"{layer.Name} (colour {layer.Color}"
                if not layer.LayerOn:
                    entry += ", off"
                if layer.Name == current:
                    entry += ", current"
                layers.append(entry + ")")

            return {
                "drawing": doc.Name,
                "path": doc.FullName or "(never saved to disk)",
                "units": unit_names.get(insunits, f"code {insunits}"),
                "entities_in_model_space": doc.ModelSpace.Count,
                "current_layer": current,
                "layers": layers,
                "unsaved_changes": not doc.Saved,
            }
        return json.dumps(bridge.run(work, config.COM_TIMEOUT), indent=2)

    @mcp.tool()
    def new_drawing() -> str:
        """Start a fresh empty drawing. Unsaved changes in the current one stay open."""
        def work(acad):
            # Documents.Add() returns a handle pywin32 cannot always resolve
            # members on; the new drawing becomes active, so read it from there.
            acad.app.Documents.Add()
            return acad.app.ActiveDocument.Name
        name = bridge.run(work, max(config.COM_TIMEOUT, 180))
        return f"Created and switched to a new drawing: {name}"

    @mcp.tool()
    def save_drawing(filename: str) -> str:
        """Save the drawing as a DWG in the server's output folder."""
        target = _safe_output_path(filename, ".dwg")

        def work(acad):
            acad.doc.SaveAs(str(target))
            return str(target)
        saved = bridge.run(work, max(config.COM_TIMEOUT, 180))
        return f"Saved to {saved}"

    @mcp.tool()
    def export_pdf(filename: str, paper_size: str = "") -> str:
        """Plot the current drawing to a PDF in the server's output folder.

        Plots the drawing extents, scaled to fit. Leave paper_size empty for the
        layout's default; the error message lists valid names if one is wrong.
        """
        target = _safe_output_path(filename, ".pdf")

        def work(acad):
            doc = acad.doc
            try:
                doc.SetVariable("BACKGROUNDPLOT", 0)  # plot synchronously
            except Exception:  # noqa: BLE001 - not fatal, plot may just be async
                pass

            layout = doc.ActiveLayout
            layout.ConfigName = "DWG To PDF.pc3"
            if paper_size:
                try:
                    layout.CanonicalMediaName = str(paper_size)
                except Exception as exc:  # noqa: BLE001
                    try:
                        available = ", ".join(layout.GetCanonicalMediaNames()[:40])
                    except Exception:  # noqa: BLE001
                        available = "(could not read the list)"
                    raise AcadError(
                        f"Paper size '{paper_size}' is not available. "
                        f"Valid names: {available}"
                    ) from exc

            layout.PlotType = 1          # acExtents
            layout.UseStandardScale = True
            layout.StandardScale = 0     # acScaleToFit
            layout.CenterPlot = True
            layout.PlotWithLineweights = True

            if not doc.Plot.PlotToFile(str(target), "DWG To PDF.pc3"):
                raise AcadError(
                    "AutoCAD reported the plot failed. Check that the "
                    "'DWG To PDF.pc3' plotter is installed and no plot is queued."
                )
            return str(target)

        saved = bridge.run(work, max(config.COM_TIMEOUT, 300))
        exists = Path(saved).exists()
        if not exists:
            return (
                f"AutoCAD accepted the plot but {saved} is not on disk yet. "
                "Background plotting may still be running -- check the folder shortly."
            )
        size_kb = Path(saved).stat().st_size / 1024
        return f"Exported {saved} ({size_kb:.0f} KB)"

    @mcp.tool()
    def autocad_status() -> str:
        """Check whether AutoCAD is running and reachable, without changing anything.

        Call this first if a drawing tool failed -- it says whether AutoCAD is
        closed, busy, or fine, and starting it is slow so it is worth knowing.
        """
        def work(acad):
            app = acad.connect(launch_if_missing=False)
            # Iterate the collection itself; Item() indexing is inconsistent
            # across AutoCAD versions and silently repeats entries.
            names = [d.Name for d in app.Documents]
            return {
                "running": True,
                "version": f"{app.Name} {app.Version}",
                "open_drawings": names or ["(none)"],
            }
        try:
            info = bridge.run(work, 30)
        except AcadError as exc:
            return (
                f"AutoCAD is not available: {exc} "
                "Use start_autocad to launch it, then try again."
            )
        return json.dumps(info, indent=2)

    @mcp.tool()
    def start_autocad(wait_seconds: int = 90) -> str:
        """Launch AutoCAD if it is not already running, and open a blank drawing.

        Starting AutoCAD takes 30-60 seconds. If it is already running this
        returns immediately without disturbing the open drawing.
        """
        def work(acad):
            app = acad.connect(launch_if_missing=True)
            app.Visible = True
            if app.Documents.Count == 0:
                app.Documents.Add()
            return f"{app.Name} {app.Version}, drawing '{acad.doc.Name}'"
        started = bridge.run(work, max(30, min(int(wait_seconds), 300)))
        return f"AutoCAD is ready: {started}"

    @mcp.tool()
    def open_drawing(filename: str) -> str:
        """Open an existing DWG from the server's output folder."""
        target = _safe_output_path(filename, ".dwg")
        if not target.exists():
            existing = sorted(p.name for p in config.SAVE_DIR.glob("*.dwg"))
            raise AcadError(
                f"There is no '{target.name}' in {config.SAVE_DIR}. "
                + (f"Available: {', '.join(existing)}" if existing else "That folder has no drawings yet.")
            )

        def work(acad):
            app = acad.connect(launch_if_missing=True)
            app.Visible = True

            # Opening a file AutoCAD already has open gives you a second, usually
            # read-only copy -- edits then land in whichever one happens to be
            # active. Switch to the existing one instead.
            wanted = str(target).lower()
            for doc in app.Documents:
                try:
                    if doc.FullName and doc.FullName.lower() == wanted:
                        doc.Activate()
                        return doc.Name, True
                except Exception:  # noqa: BLE001 - unsaved docs have no FullName
                    continue

            return app.Documents.Open(str(target)).Name, False

        name, already_open = bridge.run(work, max(config.COM_TIMEOUT, 180))
        if already_open:
            return f"{name} was already open; switched to it."
        return f"Opened {name} from {target}"

    @mcp.tool()
    def close_autocad(save_first: str = "", discard_changes: bool = False) -> str:
        """Close AutoCAD. Refuses to discard unsaved work unless you say so.

        Pass save_first with a filename to save into the output folder before
        closing. Set discard_changes=True only when losing the drawing is fine.
        """
        saved_to = None
        if save_first:
            saved_to = _safe_output_path(save_first, ".dwg")

        def work(acad):
            app = acad.connect(launch_if_missing=False)
            unsaved = []
            for i in range(app.Documents.Count):
                doc = app.Documents.Item(i)
                if not doc.Saved:
                    unsaved.append(doc.Name)

            if saved_to is not None:
                acad.doc.SaveAs(str(saved_to))
                unsaved = [n for n in unsaved if n != acad.doc.Name]

            if unsaved and not discard_changes:
                raise AcadError(
                    "Not closing: these drawings have unsaved changes -- "
                    f"{', '.join(unsaved)}. Either pass save_first with a "
                    "filename, or set discard_changes=True to throw the work away."
                )

            # Close each document explicitly with an answer to the save question,
            # so Quit never stops on a modal 'Save changes?' dialog. That dialog
            # would block COM permanently and wedge every later call.
            for i in range(app.Documents.Count - 1, -1, -1):
                app.Documents.Item(i).Close(False)
            app.Quit()
            return True

        bridge.run(work, max(config.COM_TIMEOUT, 180))
        bridge.reset()
        note = f" after saving to {saved_to}" if saved_to else ""
        return f"AutoCAD closed{note}."

    @mcp.tool()
    def capture_view(max_pixels: int = 1100, zoom_to_fit: bool = True) -> list:
        """Render the drawing to a picture and return it, so you can SEE your work.

        Use this after drawing to check the result before continuing -- it is the
        only way to catch geometry that is the wrong size, in the wrong place, or
        overlapping. Returns a summary plus the image itself.

        max_pixels caps the longest edge; keep it modest, a picture costs far more
        to look at than a line of text.
        """
        target = config.SAVE_DIR / "_view.png"

        def work(acad):
            doc = acad.doc
            if zoom_to_fit:
                acad.app.ZoomExtents()

            try:
                doc.SetVariable("BACKGROUNDPLOT", 0)  # plot on this thread
            except Exception:  # noqa: BLE001 - not fatal
                pass

            layout = doc.ActiveLayout
            previous_config = None
            try:
                previous_config = layout.ConfigName
            except Exception:  # noqa: BLE001
                pass

            try:
                layout.ConfigName = PNG_PLOTTER
            except Exception as exc:  # noqa: BLE001
                raise AcadError(
                    f"AutoCAD has no plotter called '{PNG_PLOTTER}', so the view "
                    "cannot be rendered. Check Plotter Manager for a raster plotter."
                ) from exc

            # Media names for a raster plotter are pixel sizes; pick the largest
            # that stays within the requested cap.
            try:
                chosen, best = None, -1
                for name in layout.GetCanonicalMediaNames():
                    width = _media_width(name)
                    if width and best < width <= max(max_pixels, 400):
                        chosen, best = name, width
                if chosen:
                    layout.CanonicalMediaName = chosen
            except Exception:  # noqa: BLE001 - fall back to the plotter default
                pass

            layout.PlotType = 1          # acExtents
            layout.UseStandardScale = True
            layout.StandardScale = 0     # acScaleToFit
            layout.CenterPlot = True
            try:
                # Without this AutoCAD rotates the view to fit the paper, which
                # would tell the model the drawing is sideways.
                layout.PlotRotation = 0  # ac0degrees
            except Exception:  # noqa: BLE001
                pass

            try:
                if not doc.Plot.PlotToFile(str(target), PNG_PLOTTER):
                    raise AcadError("AutoCAD reported the render failed.")
            finally:
                # Leave the layout as we found it so export_pdf still works.
                if previous_config:
                    try:
                        layout.ConfigName = previous_config
                    except Exception:  # noqa: BLE001
                        pass

            return doc.ModelSpace.Count

        count = bridge.run(work, max(config.COM_TIMEOUT, 300))

        if not target.exists():
            raise AcadError(
                "AutoCAD accepted the render but produced no file. Background "
                "plotting may still be running; try again in a moment."
            )

        data, dimensions = _shrink_png(target, max_pixels)
        return [
            f"Current view of {count} objects ({dimensions}). "
            "Check the geometry before drawing more.",
            Image(data=data, format="png"),
        ]

    # ------------------------------------------------------------------
    # Batch -- one round trip instead of many
    # ------------------------------------------------------------------

    @mcp.tool()
    def draw_batch(operations: list) -> str:
        """Run many drawing operations in a single call.

        Far faster than calling the individual tools one at a time, because each
        call has to cross the network. Pass a list of objects, each with a "type"
        and that shape's parameters:

          {"type": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0}
          {"type": "polyline", "points": [[0,0],[10,0],[10,5]], "closed": true}
          {"type": "rectangle", "x": 0, "y": 0, "width": 100, "height": 60}
          {"type": "circle", "x": 50, "y": 30, "radius": 20}
          {"type": "arc", "x": 0, "y": 0, "radius": 10, "start_angle": 0, "end_angle": 90}
          {"type": "point", "x": 5, "y": 5}
          {"type": "text", "text": "PLAN", "x": 0, "y": 70, "height": 5, "rotation": 0}
          {"type": "layer", "name": "WALLS", "color": "red"}

        Every operation runs on the layer current at that moment, so put a
        "layer" operation before the geometry that belongs on it.
        """
        ops = operations
        if isinstance(ops, str):
            try:
                ops = json.loads(ops)
            except json.JSONDecodeError as exc:
                raise AcadError(f"Could not read the operations list: {exc}") from exc
        if not isinstance(ops, (list, tuple)) or not ops:
            raise AcadError("operations must be a non-empty list of objects.")

        def work(acad):
            handles: list[str] = []
            for i, raw in enumerate(ops):
                if not isinstance(raw, dict):
                    raise AcadError(
                        f"Operation {i + 1} is not an object: {raw!r}"
                    )
                kind = str(raw.get("type", "")).strip().lower()
                try:
                    handles.append(_run_batch_op(acad, kind, raw))
                except AcadError:
                    raise
                except KeyError as exc:
                    raise AcadError(
                        f"Operation {i + 1} ({kind}) is missing {exc}."
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    raise AcadError(
                        f"Operation {i + 1} ({kind}) failed: {exc}. "
                        f"{len(handles)} earlier operations were already drawn."
                    ) from exc
            return handles

        handles = bridge.run(work, max(config.COM_TIMEOUT, 300))
        drawn = [h for h in handles if h]
        return (
            f"Drew {len(drawn)} objects in one batch. "
            f"handles={', '.join(drawn) if drawn else '(none)'}"
        )

    # ------------------------------------------------------------------
    # Gated: raw command line
    # ------------------------------------------------------------------

    if config.ENABLE_SEND_COMMAND:
        @mcp.tool()
        def run_command(command: str) -> str:
            """Run any AutoCAD command or AutoLISP expression, and report the result.

            This reaches everything the drawing tools do not: OFFSET, TRIM,
            EXTEND, FILLET, CHAMFER, ARRAY, MIRROR, ROTATE, SCALE, BLOCK, XREF.

            Always supply every argument at once in the parenthesised form:
                (command "_CIRCLE" "2,2" "1.5")
                (command "_OFFSET" "0.25" "L" "" "")
            A bare command name leaves AutoCAD waiting at a prompt and the next
            thing you send is swallowed as the answer to it.

            Reports whether AutoCAD ended up idle, the expression's value or
            error, and how many objects were created -- so you can tell a
            command that worked from one that silently did nothing.
            """
            expression = str(command).strip()
            if not expression:
                raise AcadError("No command given.")

            result_file = config.LOG_DIR / "_lisp_result.txt"
            lisp_path = str(result_file).replace("\\", "/")

            def work(acad):
                doc = acad.doc
                before = doc.ModelSpace.Count

                # Clear any prompt left behind by an earlier call, otherwise
                # this command's text is eaten as the answer to that prompt.
                recovered = False
                if _command_active(doc):
                    doc.SendCommand("\x1b\x1b")
                    time.sleep(0.3)
                    recovered = not _command_active(doc)

                result_file.unlink(missing_ok=True)

                if expression.startswith("("):
                    # Evaluate it and capture the value or the error message.
                    payload = (
                        '(vl-load-com)'
                        '(setq #mcpr (vl-catch-all-apply (function (lambda () '
                        + expression +
                        '))))'
                        f'(setq #mcpf (open "{lisp_path}" "w"))'
                        '(if (vl-catch-all-error-p #mcpr)'
                        ' (write-line (strcat "ERR " (vl-catch-all-error-message #mcpr)) #mcpf)'
                        ' (write-line (strcat "OK " (vl-prin1-to-string #mcpr)) #mcpf))'
                        '(close #mcpf)(princ)\n'
                    )
                else:
                    payload = expression + "\n"

                doc.SendCommand(payload)

                # SendCommand returns before AutoCAD has necessarily finished.
                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    if not _command_active(doc) and (
                        result_file.exists() or not expression.startswith("(")
                    ):
                        break
                    time.sleep(0.2)

                after = doc.ModelSpace.Count
                stuck = _command_active(doc)
                try:
                    prompt = str(doc.GetVariable("LASTPROMPT") or "").strip()
                except Exception:  # noqa: BLE001
                    prompt = ""

                # Hand back the handles of whatever was just created, so a
                # command-line draw is as usable as a typed draw tool -- these
                # are what draw_hatch and erase_entity need.
                handles = []
                model = doc.ModelSpace
                for i in range(before, min(after, before + 50)):
                    try:
                        handles.append(model.Item(i).Handle)
                    except Exception:  # noqa: BLE001
                        break

                return before, after, stuck, recovered, prompt, handles

            before, after, stuck, recovered, prompt, handles = bridge.run(
                work, max(config.COM_TIMEOUT, 120)
            )

            outcome = "(no value reported)"
            if result_file.exists():
                text = result_file.read_text(encoding="utf-8", errors="replace").strip()
                if text.startswith("ERR "):
                    outcome = f"AutoLISP error: {text[4:]}"
                elif text.startswith("OK "):
                    outcome = f"returned {text[3:]}"

            lines = [f"Ran: {expression}", outcome]
            created = after - before
            if created:
                line = f"{created} new object(s) ({before} -> {after})."
                if handles:
                    line += f" handles={', '.join(handles)}"
                lines.append(line)
            else:
                lines.append(
                    f"No new objects ({after} in model space). If you expected "
                    "geometry, the command did not do what you intended."
                )
            if recovered:
                lines.append("Cleared a prompt left over from an earlier command.")
            if stuck:
                lines.append(
                    "WARNING: AutoCAD is still waiting at a prompt"
                    + (f" ('{prompt}')" if prompt else "")
                    + ". Send the missing input, or run autocad_status to clear it."
                )
            return "\n".join(lines)


def _command_active(doc) -> bool:
    """True when AutoCAD is mid-command, i.e. waiting for input."""
    try:
        return int(doc.GetVariable("CMDACTIVE")) != 0
    except Exception:  # noqa: BLE001 - treat unknown as idle rather than block
        return False


def _media_width(media_name: str) -> int | None:
    """Pixel width from a raster plotter media name, e.g. UXGA_(1600.00_x_1200.00_Pixels)."""
    match = re.search(r"(\d+)(?:\.\d+)?[_ ]*x", str(media_name), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _shrink_png(path: Path, max_pixels: int) -> tuple[bytes, str]:
    """Downscale the render so it is cheap for a model to look at."""
    raw = path.read_bytes()
    try:
        from PIL import Image as PilImage
    except ImportError:
        return raw, "original size"

    with PilImage.open(io.BytesIO(raw)) as img:
        img = img.convert("RGB")
        original = f"{img.width}x{img.height}"
        if max(img.size) > max_pixels:
            ratio = max_pixels / max(img.size)
            img = img.resize(
                (max(1, round(img.width * ratio)), max(1, round(img.height * ratio))),
                PilImage.LANCZOS,
            )
        buffer = io.BytesIO()
        img.save(buffer, format="PNG", optimize=True)
        shrunk = f"{img.width}x{img.height}"

    return buffer.getvalue(), (
        shrunk if shrunk == original else f"{shrunk}, rendered at {original}"
    )


def _run_batch_op(acad, kind: str, spec: dict) -> str:
    """Execute one operation from draw_batch on the COM thread."""
    def num(key, default=None):
        if key not in spec:
            if default is None:
                raise KeyError(key)
            return float(default)
        return float(spec[key])

    if kind == "line":
        obj = acad.ms.AddLine(
            point(num("x1"), num("y1")), point(num("x2"), num("y2"))
        )
    elif kind in ("polyline", "pline"):
        pts = _points_from(spec.get("points", []), "polyline points")
        obj = acad.ms.AddLightWeightPolyline(doubles(flatten_points(pts)))
        if spec.get("closed"):
            obj.Closed = True
    elif kind in ("rectangle", "rect"):
        x, y = num("x"), num("y")
        w, h = num("width"), num("height")
        corners = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
        obj = acad.ms.AddLightWeightPolyline(doubles(flatten_points(corners)))
        obj.Closed = True
    elif kind == "circle":
        obj = acad.ms.AddCircle(point(num("x"), num("y")), num("radius"))
    elif kind == "arc":
        obj = acad.ms.AddArc(
            point(num("x"), num("y")), num("radius"),
            deg2rad(num("start_angle")), deg2rad(num("end_angle")),
        )
    elif kind == "point":
        obj = acad.ms.AddPoint(point(num("x"), num("y")))
    elif kind == "text":
        obj = acad.ms.AddText(
            str(spec.get("text", "")), point(num("x"), num("y")), num("height", 2.5)
        )
        if spec.get("rotation"):
            obj.Rotation = deg2rad(spec["rotation"])
    elif kind == "mtext":
        obj = acad.ms.AddMText(
            point(num("x"), num("y")), num("width", 100), str(spec.get("text", ""))
        )
        obj.Height = num("height", 2.5)
    elif kind == "layer":
        layer = acad.doc.Layers.Add(str(spec.get("name", "0")))
        index = _color_index(spec.get("color"))
        if index is not None:
            layer.Color = index
        acad.doc.ActiveLayer = layer
        return ""  # layers have handles but they are not drawn objects
    else:
        raise AcadError(
            f"Unknown operation type '{kind}'. Supported: line, polyline, "
            "rectangle, circle, arc, point, text, mtext, layer."
        )

    return obj.Handle
