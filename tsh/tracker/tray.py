"""System tray icon for the tsh tracker.

Two layers:

- ``IconActions`` — the testable callback object. Holds references to the
  TrackerState plus three injected callbacks: ``on_switch_request`` (opens
  the picker popup, supplied by Phase 7's GUI), ``on_show_main_window``
  (also Phase 7), and ``on_quit`` (the runner's shutdown function — see
  Task 19). Provides ``build_menu()`` which returns a pystray Menu object
  reflecting current state, and ``handle_pause_resume()`` etc. that
  drive ``state``.

- ``build_icon(actions)`` — constructs the pystray.Icon. Runs its own
  thread when ``icon.run()`` is called; not started here.

Tests target IconActions directly. The actual icon rendering is manual.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Callable

from PIL import Image, ImageDraw

from tsh.tracker.server import TrackerState

try:
    import pystray  # noqa: F401  — imported lazily by build_icon
except Exception:  # pragma: no cover
    pystray = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ----- Icon image -----------------------------------------------------------


def _create_icon_image(active: bool) -> Image.Image:
    """Generate a 64x64 icon. Solid green dot when active, hollow grey ring when idle."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (34, 139, 34, 255) if active else (120, 120, 120, 255)
    draw.ellipse((8, 8, 56, 56), outline=color, width=4)
    if active:
        draw.ellipse((20, 20, 44, 44), fill=color)
    return img


# ----- Status text ---------------------------------------------------------


def _format_elapsed(seconds: int) -> str:
    hours, rem = divmod(int(seconds), 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _status_text(state: TrackerState) -> str:
    """Tooltip / first menu entry text. 'SFXS-1073 — 1h 15m' or 'Idle'."""
    state.refresh()
    active = state.get_active()
    if not active or active.start_at is None:
        return "tsh — Idle"
    elapsed = (datetime.now(timezone.utc) - active.start_at).total_seconds()
    return f"tsh — {active.ticket_key} — {_format_elapsed(int(elapsed))}"


# ----- Action object --------------------------------------------------------


class IconActions:
    """Testable callbacks behind every tray menu item.

    The pystray Menu items invoke these methods. The methods are pure with
    respect to the icon (they don't touch pystray); they only mutate the
    TrackerState or call out to injected callbacks.
    """

    def __init__(
        self,
        state: TrackerState,
        *,
        on_switch_request: Callable[[], None],
        on_show_main_window: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self.state = state
        self._on_switch_request = on_switch_request
        self._on_show_main_window = on_show_main_window
        self._on_quit = on_quit

    # --- direct state actions ---

    def handle_pause(self) -> None:
        """'Pause' menu — stops the active timer."""
        try:
            self.state.stop()
        except Exception:  # pragma: no cover - defensive
            logger.exception("pause failed")

    def handle_switch_request(self) -> None:
        """'Switch task...' — defers to the injected GUI callback."""
        self._on_switch_request()

    def handle_show_main_window(self) -> None:
        """'Show main window' — defers to the injected GUI callback."""
        self._on_show_main_window()

    def handle_quit(self) -> None:
        """'Quit' — defers to the injected runner shutdown."""
        self._on_quit()

    # --- predicates the menu uses to enable/disable items ---

    def is_active(self) -> bool:
        self.state.refresh()
        return self.state.get_active() is not None

    def status_text(self) -> str:
        return _status_text(self.state)


# ----- Icon construction ----------------------------------------------------


def build_icon(actions: IconActions, *, name: str = "tsh"):
    """Construct (but do not start) a pystray.Icon wired to ``actions``.

    Caller invokes ``icon.run()`` from a thread when ready (the runner does
    this in Task 19). The menu re-evaluates predicates and labels on each
    open via ``pystray.Menu``'s lazy callbacks.
    """
    import pystray  # imported here so the module is importable without pystray installed

    def _menu_items():
        yield pystray.MenuItem(actions.status_text(), None, enabled=False)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(
            "Switch task...",
            lambda icon, item: actions.handle_switch_request(),
        )
        yield pystray.MenuItem(
            "Pause",
            lambda icon, item: actions.handle_pause(),
            enabled=actions.is_active(),
        )
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(
            "Show main window",
            lambda icon, item: actions.handle_show_main_window(),
        )
        yield pystray.MenuItem(
            "Quit",
            lambda icon, item: actions.handle_quit(),
        )

    icon = pystray.Icon(
        name=name,
        title=actions.status_text(),
        icon=_create_icon_image(actions.is_active()),
        menu=pystray.Menu(lambda: tuple(_menu_items())),
    )
    return icon


# ----- Periodic refresh helper ---------------------------------------------


def refresh_icon(icon, actions: IconActions) -> None:
    """Update the icon image and tooltip to reflect current state.

    The runner (Task 19) calls this periodically (e.g., every second) so the
    elapsed time in the tooltip stays accurate.
    """
    icon.icon = _create_icon_image(actions.is_active())
    icon.title = actions.status_text()
