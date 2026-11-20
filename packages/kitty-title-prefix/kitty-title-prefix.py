# kitty global watcher: macOS menu-bar title "<prefix>: <active tab title>"
# per OS window. Set the prefix with `wt <name>`, clear it with `wt`
# (user var title_prefix, kept on one window of that OS window).
# No module state: after a config reload kitty may load a fresh copy of
# this file, so the prefix is always read back from the windows.
from typing import Any

from kitty.boss import Boss
from kitty.fast_data_types import set_os_window_title
from kitty.window import Window

KEY = "title_prefix"


def windows_of(boss: Boss, os_window_id: int) -> list[Window]:
    tm = boss.os_window_map.get(os_window_id)
    return [w for tab in tm for w in tab] if tm is not None else []


def refresh(boss: Boss, os_window_id: int, clear: bool = False) -> None:
    tm = boss.os_window_map.get(os_window_id)
    if tm is None:
        return
    prefix = next(
        (w.user_vars[KEY] for w in windows_of(boss, os_window_id) if w.user_vars.get(KEY)),
        "",
    )
    # Marker on the TabManager (survives a module reload): set only when this
    # watcher pinned the title, so kitty's own temporary titles stay untouched.
    applied = getattr(tm, "title_prefix_applied", False)
    if not prefix:
        if clear or applied:
            # Empty title drops the override: kitty goes back to its own titles.
            set_os_window_title(os_window_id, "")
            tm.title_prefix_applied = False
        return
    tm.title_prefix_applied = True
    tab = tm.active_tab
    title = tab.effective_title if tab is not None else ""
    set_os_window_title(os_window_id, f"{prefix}: {title}" if title else prefix)


def on_set_user_var(boss: Boss, window: Window, data: dict[str, Any]) -> None:
    if data.get("key") != KEY:
        return
    try:
        # One prefix per OS window: drop it from the other windows directly
        # (no set_user_var, so no watcher recursion).
        for w in windows_of(boss, window.os_window_id):
            if w is not window:
                w.user_vars.pop(KEY, None)
        refresh(boss, window.os_window_id, clear=not data.get("value"))
    except Exception as e:
        print("title-prefix:", e)


def on_tab_bar_dirty(boss: Boss, window: Window, data: dict[str, Any]) -> None:
    # kitty doesn't catch errors here; one would break tab actions.
    try:
        refresh(boss, data["tab_manager"].os_window_id)
    except Exception as e:
        print("title-prefix:", e)
