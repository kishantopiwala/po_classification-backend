"""Click the Time Champ popup once, minimize it if still open, then exit.

Setup: python3 -m pip install 'pynput>=1.8.1'
Run from backend: .venv/bin/python testing.py
                  .venv/bin/python testing.py --window-title 'TimeChamp'
Idle mode:        .venv/bin/python testing.py --mode idle --idle-seconds 180 --interval 60

Popup mode matches a title prefix AND exact application class, ignoring spaces
and case, excluding the app's Pause or Resume status window.
It does not read countdown text inside the window. Use --list-windows
to inspect titles and classes without sending any keys or minimizing windows.
It also reacts if the matching window is already visible at startup.
It clicks the popup center once. Use --popup-action ctrl for the earlier key action.
Logs the detection time in IST and elapsed minutes since script startup.
Idle mode starts monitoring when this script starts. The first press happens after 180
seconds without physical keyboard activity; further presses occur every 60
seconds. Physical input resets the timer. Mouse activity does not reset it.
Press Escape anywhere, or Ctrl+C in the terminal, to stop.
Requires a supported graphical desktop (Linux: X11, not Wayland).
"""

import argparse
import math
import os
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo


def visible_windows(display):
    """Yield visible X11 windows on the current desktop."""
    from Xlib import X, error

    root = display.screen().root
    atom = display.intern_atom

    def property_value(window, name):
        result = window.get_full_property(atom(name), X.AnyPropertyType)
        return result.value if result is not None else None

    clients = property_value(root, "_NET_CLIENT_LIST")
    if clients is None:
        windows = root.query_tree().children
    else:
        windows = [
            display.create_resource_object("window", int(wid)) for wid in clients
        ]
    current_desktop = property_value(root, "_NET_CURRENT_DESKTOP")
    hidden = atom("_NET_WM_STATE_HIDDEN")
    for window in windows:
        try:
            if window.get_attributes().map_state != X.IsViewable:
                continue
            states = property_value(window, "_NET_WM_STATE")
            if states is not None and hidden in states:
                continue
            desktop = property_value(window, "_NET_WM_DESKTOP")
            if (
                desktop is not None
                and current_desktop is not None
                and int(desktop[0]) not in (int(current_desktop[0]), 0xFFFFFFFF)
            ):
                continue
            yield window
        except error.BadWindow:
            # A popup can close between listing it and reading its properties.
            continue


def window_details(display, window):
    from Xlib import X

    prop = window.get_full_property(
        display.intern_atom("_NET_WM_NAME"), X.AnyPropertyType
    )
    title = prop.value if prop is not None else window.get_wm_name() or ""
    if isinstance(title, bytes):
        title = title.decode("utf-8", errors="replace")
    return str(title), tuple(window.get_wm_class() or ())


def window_matches(title, classes, expected_title, expected_class):
    def normalize(value):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return "".join(value.casefold().split())

    normalized_title = normalize(title)
    if "pauseorresume" in normalized_title:
        return False
    return normalized_title.startswith(normalize(expected_title)) and any(
        normalize(item) == normalize(expected_class) for item in classes
    )


def find_visible_window(display, title, window_class):
    from Xlib import error

    for window in visible_windows(display):
        try:
            name, classes = window_details(display, window)
            if window_matches(name, classes, title, window_class):
                return window
        except error.BadWindow:
            continue
    return None


def click_popup(display, window):
    """Raise the identified popup and click only if it is under the pointer."""
    from Xlib import X
    from Xlib.ext import xtest
    from Xlib.protocol.event import ClientMessage

    root = display.screen().root
    root.send_event(
        ClientMessage(
            window=window.id,
            client_type=display.intern_atom("_NET_ACTIVE_WINDOW"),
            data=(32, [2, X.CurrentTime, 0, 0, 0]),
        ),
        event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
    )
    display.flush()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        geometry = window.get_geometry()
        window.warp_pointer(geometry.width // 2, geometry.height // 2)
        display.sync()
        current = root
        for _ in range(32):
            child = current.query_pointer().child
            if not child:
                break
            if child.id == window.id:
                xtest.fake_input(display, X.ButtonPress, 1)
                try:
                    display.sync()
                finally:
                    xtest.fake_input(display, X.ButtonRelease, 1)
                    display.sync()
                print("Clicked once inside the Time Champ popup.", flush=True)
                return
            current = child
        time.sleep(0.05)
    raise RuntimeError("Popup is covered by another window; no click was sent")


def minimize_window(display, window):
    """Ask the X11 window manager to minimize only the detected window."""
    from Xlib import X, Xutil, error
    from Xlib.protocol.event import ClientMessage

    try:
        if window.get_attributes().map_state != X.IsViewable:
            print("Popup was dismissed by the click.", flush=True)
            return
    except error.BadWindow:
        print("Popup closed after the click.", flush=True)
        return
    root = display.screen().root
    event = ClientMessage(
        window=window.id,
        client_type=display.intern_atom("WM_CHANGE_STATE"),
        data=(32, [Xutil.IconicState, 0, 0, 0, 0]),
    )
    root.send_event(
        event,
        event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
    )
    display.flush()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            if window.get_attributes().map_state != X.IsViewable:
                print("Time Champ window is no longer visible.", flush=True)
                return
            state = window.get_wm_state()
            if state is not None and state.state == Xutil.IconicState:
                print("Minimized Time Champ window.", flush=True)
                return
        except error.BadWindow:
            print("Time Champ window has already closed.", flush=True)
            return
        time.sleep(0.05)
    raise RuntimeError("The window manager did not minimize the Time Champ window")


class IdleTimer:
    """Track physical activity independently of generated key events."""

    def __init__(self, idle_seconds, interval, clock=time.monotonic):
        self.clock = clock
        self.idle_seconds = idle_seconds
        self.interval = interval
        self.next_press = clock() + idle_seconds
        self.held = set()
        self.lock = threading.RLock()

    def activity(self, key, pressed, injected):
        if injected:
            return
        with self.lock:
            if pressed:
                self.held.add(key)
            else:
                self.held.discard(key)
            self.next_press = self.clock() + self.idle_seconds

    def tick(self, emit):
        with self.lock:
            if self.held or self.clock() < self.next_press:
                return False
            emit()
            self.next_press = self.clock() + self.interval
            return True


def positive_seconds(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return result


def main():
    started_at = time.monotonic()
    started_time = datetime.now(ZoneInfo("Asia/Kolkata"))
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "key",
        nargs="?",
        default="ctrl",
        help="single character or key name (default: ctrl)",
    )
    parser.add_argument("--mode", choices=("popup", "idle"), default="popup")
    parser.add_argument("--popup-action", choices=("click", "ctrl"), default="click")
    parser.add_argument(
        "--window-title",
        default="Time Champ",
        help="window title prefix, ignoring case and spaces",
    )
    parser.add_argument(
        "--window-class",
        default="TimeChamp",
        help="exact application WM_CLASS, ignoring case and spaces",
    )
    parser.add_argument(
        "--list-windows",
        action="store_true",
        help="print visible titles and application classes, then exit without actions",
    )
    parser.add_argument("--idle-seconds", type=positive_seconds, default=180)
    parser.add_argument("--interval", type=positive_seconds, default=60)
    args = parser.parse_args()
    if not args.window_title.strip():
        parser.error("--window-title must not be empty")
    if not args.window_class.strip():
        parser.error("--window-class must not be empty")
    if args.mode == "popup" and not sys.platform.startswith("linux"):
        parser.error("popup detection currently requires Linux X11")

    if sys.platform.startswith("linux") and (
        os.environ.get("XDG_SESSION_TYPE") == "wayland"
        or os.environ.get("WAYLAND_DISPLAY")
    ):
        parser.exit(
            1, "This script requires an X11 desktop on Linux; Wayland is unsupported.\n"
        )
    if args.list_windows:
        from Xlib import error
        from Xlib.display import Display

        display = Display()
        try:
            for window in visible_windows(display):
                try:
                    title, classes = window_details(display, window)
                    print(f"0x{window.id:x}: title={title!r}, class={classes!r}")
                except error.BadWindow:
                    continue
        finally:
            display.close()
        return 0
    if os.environ.get(
        "PYNPUT_BACKEND_KEYBOARD", os.environ.get("PYNPUT_BACKEND", "")
    ) in {"uinput", "dummy"}:
        parser.exit(
            1, "Use a desktop pynput backend that identifies injected events.\n"
        )
    try:
        from importlib.metadata import version

        from pynput import keyboard

        if tuple(int(n) for n in version("pynput").split(".")[:2]) < (1, 8):
            parser.exit(
                1, "Upgrade pynput: python3 -m pip install --upgrade 'pynput>=1.8.1'\n"
            )
    except ImportError as exc:
        parser.exit(
            1,
            f"Keyboard access unavailable: {exc}\n"
            "Install: python3 -m pip install 'pynput>=1.8.1'\n",
        )

    name = args.key.lower()
    key = getattr(keyboard.Key, name, None)
    if key is None:
        if len(args.key) != 1:
            parser.error(f"unknown key: {args.key}")
        key = keyboard.KeyCode.from_char(args.key)
    if key == keyboard.Key.esc:
        parser.error("Escape is reserved for stopping the script")

    controller = keyboard.Controller()
    stopped = threading.Event()
    timer = IdleTimer(args.idle_seconds, args.interval)

    def on_press(event_key, injected):
        if not injected and event_key == keyboard.Key.esc:
            stopped.set()
            return False
        timer.activity(event_key, True, injected)

    def on_release(event_key, injected):
        timer.activity(event_key, False, injected)

    def emit():
        try:
            controller.press(key)
        finally:
            controller.release(key)
        print(f"Pressed and released {args.key}.", flush=True)

    try:
        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            if args.mode == "popup":
                from Xlib.display import Display

                display = Display()
                try:
                    print(
                        f"Started at {started_time:%Y-%m-%d %H:%M:%S %Z}.", flush=True
                    )
                    print(
                        f"Started at {started_time:%Y-%m-%d %H:%M:%S %Z}. "
                        f"Waiting for title={args.window_title!r}, class={args.window_class!r}; "
                        f"will {args.popup_action} once, minimize it, and exit. Escape stops.",
                        flush=True,
                    )
                    detected = False
                    while not stopped.is_set() and listener.is_alive():
                        window = find_visible_window(
                            display, args.window_title, args.window_class
                        )
                        if window is not None:
                            if not detected:
                                elapsed = time.monotonic() - started_at
                                minutes, seconds = divmod(int(elapsed), 60)
                                detected_time = datetime.now(ZoneInfo("Asia/Kolkata"))
                                print(
                                    f"Popup detected at {detected_time:%Y-%m-%d %H:%M:%S %Z}; "
                                    f"{minutes} minutes {seconds} seconds after script start "
                                    f"({elapsed / 60:.2f} minutes).",
                                    flush=True,
                                )
                                detected = True
                            # Avoid combining Ctrl with a key the user is holding.
                            with timer.lock:
                                if not timer.held and not stopped.is_set():
                                    title, classes = window_details(display, window)
                                    print(
                                        f"Matched 0x{window.id:x}: title={title!r}, class={classes!r}",
                                        flush=True,
                                    )
                                    if args.popup_action == "click":
                                        click_popup(display, window)
                                        time.sleep(0.2)
                                    else:
                                        emit()
                                    minimize_window(display, window)
                                    break
                        stopped.wait(0.25)
                finally:
                    display.close()
            else:
                timer.activity(None, False, False)
                print(
                    f"Waiting for {args.idle_seconds:g}s of keyboard inactivity; "
                    f"then pressing {args.key} every {args.interval:g}s. Escape stops.",
                    flush=True,
                )
                while not stopped.wait(0.1):
                    if not listener.is_alive():
                        break
                    timer.tick(emit)
            listener.stop()
            listener.join()  # Propagate errors raised inside callbacks.
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception as exc:
        print(f"Keyboard automation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
