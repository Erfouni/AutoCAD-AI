#!/usr/bin/env python3
"""COM bridge to AutoCAD.

AutoCAD's automation interface is apartment-threaded: every call has to come
from the same thread that initialised COM, and the application rejects calls
outright while it is busy (a modal dialog, a running command, a redraw). An
async HTTP server serves requests from a pool of threads, so all AutoCAD work
is funnelled onto one dedicated worker thread through a queue.

Public surface:
    bridge.run(fn)          -- run fn(acad) on the COM thread, return its result
    acad.app / .doc / .ms   -- live COM handles, only valid inside fn
    point(x, y) / doubles() -- VARIANT helpers COM requires for coordinates
"""

from __future__ import annotations

import math
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

import pythoncom
import win32com.client
from win32com.client import VARIANT

# COM errors that mean "busy, try again" rather than "broken".
_RETRYABLE_HRESULTS = {
    -2147418111,  # 0x80010001 RPC_E_CALL_REJECTED
    -2147417846,  # 0x8001010A RPC_E_SERVERCALL_RETRYLATER
    -2147417847,  # 0x80010109 RPC_E_SERVERCALL_REJECTED
}

# Errors that mean the AutoCAD we were talking to is gone.
_DISCONNECTED_HRESULTS = {
    -2147023174,  # 0x800706BA RPC_S_SERVER_UNAVAILABLE
    -2147023170,  # 0x800706BE RPC_S_CALL_FAILED
    -2146959355,  # 0x80080005 CO_E_SERVER_EXEC_FAILURE
}


class AcadError(RuntimeError):
    """A readable failure the language model can act on."""


# --------------------------------------------------------------------------
# VARIANT helpers -- AutoCAD refuses plain Python lists for coordinates
# --------------------------------------------------------------------------

def point(x: float, y: float, z: float = 0.0) -> VARIANT:
    """A 3D point as the array-of-doubles VARIANT AutoCAD expects."""
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(x), float(y), float(z)])


def doubles(values) -> VARIANT:
    """A flat array-of-doubles VARIANT (polyline vertices, etc.)."""
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])


def dispatches(objects) -> VARIANT:
    """An array-of-objects VARIANT (hatch boundary loops, selection sets)."""
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, list(objects))


def flatten_points(points) -> list[float]:
    """[[x, y], [x, y], ...] -> [x, y, x, y, ...] for lightweight polylines."""
    flat: list[float] = []
    for i, p in enumerate(points):
        if len(p) < 2:
            raise AcadError(f"Point {i} needs at least an x and a y, got {p!r}.")
        flat.extend((float(p[0]), float(p[1])))
    return flat


# --------------------------------------------------------------------------
# The COM worker
# --------------------------------------------------------------------------

class _Acad:
    """Live COM handles. Only touch these from inside the worker thread."""

    def __init__(self) -> None:
        self._app = None
        # How long a cold AutoCAD start may take before we give up.
        self.launch_timeout = 180.0

    # -- connection ------------------------------------------------------
    def connect(self, launch_if_missing: bool = True):
        """Attach to a running AutoCAD, or start one."""
        if self._app is not None:
            # A cached handle survives AutoCAD being closed but every call on it
            # fails, so prove it still answers before handing it back.
            try:
                _ = self._app.Documents.Count
                return self._app
            except (pythoncom.com_error, AttributeError):
                # A stale dynamic-dispatch proxy raises AttributeError rather
                # than a COM error, which would otherwise escape unformatted.
                self._app = None

        try:
            self._app = win32com.client.GetActiveObject("AutoCAD.Application")
        except pythoncom.com_error:
            if not launch_if_missing:
                raise AcadError(
                    "AutoCAD is not running. Start AutoCAD, open a drawing, and try again."
                )
            try:
                self._app = win32com.client.Dispatch("AutoCAD.Application")
            except pythoncom.com_error as exc:
                raise AcadError(
                    "Could not start AutoCAD through COM. Check that AutoCAD 2027 "
                    f"(full version, not LT) is installed. COM said: {exc}"
                ) from exc

            # A cold start takes the better part of a minute, and until it
            # finishes AutoCAD rejects every call with "call was rejected by
            # callee". Wait for it to answer before touching anything.
            deadline = time.monotonic() + self.launch_timeout
            last_error: pythoncom.com_error | None = None
            while time.monotonic() < deadline:
                try:
                    _ = self._app.Documents.Count
                    break
                except pythoncom.com_error as exc:
                    last_error = exc
                    time.sleep(1.0)
            else:
                self._app = None
                raise AcadError(
                    f"AutoCAD did not finish starting within {self.launch_timeout:.0f}s. "
                    "It may be showing a startup dialog or a licence prompt -- check "
                    f"the AutoCAD window. ({last_error})"
                )

            try:
                self._app.Visible = True
            except pythoncom.com_error:
                pass  # cosmetic; it is running and answering, which is what matters

        return self._app

    def drop(self) -> None:
        """Forget the current handles so the next call re-attaches."""
        self._app = None

    # -- handles ---------------------------------------------------------
    @property
    def app(self):
        return self.connect()

    @property
    def doc(self):
        app = self.connect()
        try:
            if app.Documents.Count == 0:
                return app.Documents.Add()
            return app.ActiveDocument
        except pythoncom.com_error as exc:
            hresult = getattr(exc, "hresult", exc.args[0])
            if hresult in _DISCONNECTED_HRESULTS:
                # AutoCAD went away mid-call. Let the retry layer see the raw COM
                # error so it re-attaches instead of reporting a dead handle.
                self.drop()
                raise
            raise AcadError(f"No drawing is open in AutoCAD ({exc}).") from exc

    @property
    def ms(self):
        """Model space of the active drawing."""
        return self.doc.ModelSpace

    # -- convenience -----------------------------------------------------
    def entity_by_handle(self, handle: str):
        """Look up an entity by the handle a draw tool returned."""
        try:
            return self.doc.HandleToObject(str(handle).strip())
        except pythoncom.com_error as exc:
            raise AcadError(
                f"No entity with handle '{handle}' in this drawing. "
                "Handles change when the drawing is closed and reopened."
            ) from exc

    def set_var(self, name: str, value: Any) -> None:
        self.doc.SetVariable(name, value)

    def get_var(self, name: str) -> Any:
        return self.doc.GetVariable(name)


class AcadBridge:
    """Serialises all AutoCAD work onto one COM-initialised thread."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._acad = _Acad()
        self._thread = threading.Thread(
            target=self._worker, name="autocad-com", daemon=True
        )
        self._thread.start()

    # -- public ----------------------------------------------------------
    def run(self, fn: Callable[[_Acad], Any], timeout: float = 120.0) -> Any:
        """Run fn on the COM thread and return its result.

        Raises AcadError with a message worth showing to a model, never a bare
        COM error.
        """
        future: Future = Future()
        self._queue.put((fn, future))
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            # A call still waiting in the queue is withdrawn, so it cannot fire
            # once AutoCAD is free and duplicate whatever the model sends next.
            # One already running on the COM thread cannot be stopped.
            if future.cancel():
                fate = "The request was not sent to AutoCAD."
            elif future.done():
                return future.result()
            else:
                fate = (
                    "The request was already running and may still complete once "
                    "AutoCAD is free, so check with list_entities before sending "
                    "it again."
                )
            raise AcadError(
                f"AutoCAD did not respond within {timeout:.0f}s. It is most likely "
                "showing a dialog box or waiting for input at the command line. "
                "Switch to AutoCAD, press Escape, close any dialog, then retry. "
                f"{fate}"
            ) from None

    def reset(self) -> None:
        """Forget the cached COM handles, e.g. after deliberately quitting AutoCAD."""
        self._acad.drop()

    # -- internals -------------------------------------------------------
    def _worker(self) -> None:
        pythoncom.CoInitialize()
        try:
            while True:
                fn, future = self._queue.get()
                if fn is None:
                    return
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(self._call_with_retry(fn))
                except Exception as exc:  # noqa: BLE001 - reported to the caller
                    future.set_exception(exc)
        finally:
            pythoncom.CoUninitialize()

    def _call_with_retry(self, fn: Callable[[_Acad], Any]) -> Any:
        """Call fn, backing off while AutoCAD says it is busy."""
        delays = [0.2, 0.4, 0.8, 1.2, 1.6, 2.0, 2.0, 2.0]  # ~10s total
        last: Exception | None = None

        for attempt, delay in enumerate([0.0, *delays]):
            if delay:
                time.sleep(delay)
            try:
                return fn(self._acad)
            except AcadError:
                raise
            except AttributeError as exc:
                # A COM proxy left over from a closed AutoCAD raises
                # AttributeError ("<unknown>.Count"), not a COM error, and would
                # otherwise reach the caller as gibberish. Treat it as a lost
                # connection: drop the handles and let the loop re-attach.
                last = exc  # type: ignore[assignment]
                self._acad.drop()
                continue
            except pythoncom.com_error as exc:
                last = exc
                hresult = exc.hresult if hasattr(exc, "hresult") else exc.args[0]

                if hresult in _DISCONNECTED_HRESULTS:
                    # AutoCAD went away; re-attach and let the loop retry.
                    self._acad.drop()
                    continue
                if hresult in _RETRYABLE_HRESULTS:
                    continue
                raise AcadError(_describe_com_error(exc)) from exc

        self._acad.drop()
        raise AcadError(
            "AutoCAD stayed busy for 10 seconds and refused the request. "
            "It is usually a modal dialog or an unfinished command: switch to "
            f"AutoCAD, press Escape, close any dialog, then retry. ({last})"
        )


def _describe_com_error(exc: pythoncom.com_error) -> str:
    """Turn a COM error tuple into something a model can act on."""
    detail = ""
    try:
        args = exc.args
        if len(args) > 2 and args[2]:
            # args[2] = (source, description, helpfile, helpcontext, scode)
            detail = str(args[2][2] or args[2][1] or "").strip()
        if not detail and len(args) > 1:
            detail = str(args[1]).strip()
    except Exception:  # noqa: BLE001 - never fail while formatting an error
        detail = str(exc)

    hint = ""
    lowered = detail.lower()
    if "invalid" in lowered and "argument" in lowered:
        hint = (
            " AutoCAD rejected an argument: check that coordinates are numbers, "
            "radii are greater than zero, and angles are in degrees."
        )
    elif "not applicable" in lowered or "no document" in lowered:
        hint = " Make sure a drawing is open in AutoCAD."

    return f"AutoCAD refused the operation: {detail or exc}.{hint}"


# --------------------------------------------------------------------------
# Small shared helpers used by the tool layer
# --------------------------------------------------------------------------

def deg2rad(degrees: float) -> float:
    return math.radians(float(degrees))


def entity_summary(entity) -> dict:
    """A compact, JSON-safe description of an AutoCAD entity."""
    info: dict[str, Any] = {}
    for attr, key in (
        ("Handle", "handle"),
        ("ObjectName", "type"),
        ("Layer", "layer"),
    ):
        try:
            info[key] = getattr(entity, attr)
        except Exception:  # noqa: BLE001 - entities vary in what they expose
            pass

    try:
        lo, hi = entity.GetBoundingBox()
        info["bounds"] = {
            "min": [round(lo[0], 4), round(lo[1], 4)],
            "max": [round(hi[0], 4), round(hi[1], 4)],
        }
    except Exception:  # noqa: BLE001 - not every entity has a bounding box
        pass

    for attr, key in (("Area", "area"), ("Length", "length"), ("Radius", "radius")):
        try:
            info[key] = round(float(getattr(entity, attr)), 4)
        except Exception:  # noqa: BLE001
            pass

    return info


# One bridge per process.
bridge = AcadBridge()
