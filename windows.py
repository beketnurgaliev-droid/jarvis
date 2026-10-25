# -*- coding: utf-8 -*-
"""Окна: найти, переключиться, свернуть, закрыть.

Закрытие — вежливое (WM_CLOSE, как крестик): программа сама спросит, сохранить
ли несохранённое. Принудительно ничего не убиваем.
"""

import ctypes
import ctypes.wintypes
import logging
import time
from pathlib import Path

import psutil
import win32con
import win32gui
import win32process

log = logging.getLogger("jarvis.windows")

# служебные окна, которые пользователь окнами не считает
_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                 "Windows.UI.Core.CoreWindow", "ApplicationFrameWindow"}
_SKIP_EXE = {"textinputhost", "shellexperiencehost", "searchhost", "startmenuexperiencehost",
             "systemsettings", "lockapp", "applicationframehost"}


class Win:
    def __init__(self, hwnd, title, exe):
        self.hwnd = hwnd
        self.title = title
        self.exe = exe          # имя процесса без .exe, в нижнем регистре

    def __repr__(self):
        return f"<{self.exe}: {self.title}>"


def _is_cloaked(hwnd):
    """Скрытые окна UWP «видимы», но на экране их нет."""
    cloaked = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked),
                                               ctypes.sizeof(cloaked))
    return cloaked.value != 0


def list_windows():
    """Обычные окна верхнего уровня — в порядке сверху вниз (последнее
    активное первым)."""
    found = []
    own_pid = psutil.Process().pid

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        if not title or win32gui.GetClassName(hwnd) in _SKIP_CLASSES:
            return True
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TOOLWINDOW or _is_cloaked(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == own_pid:
            return True
        try:
            exe = Path(psutil.Process(pid).name()).stem.lower()
        except (psutil.Error, OSError):
            return True
        if exe not in _SKIP_EXE:
            found.append(Win(hwnd, title, exe))
        return True

    win32gui.EnumWindows(visit, None)
    return found


def foreground():
    """Активное окно — напрямую, а не из списка: диалоги («Сохранить?») в
    список не попадают, но закрыть просят как раз их."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd or win32gui.GetClassName(hwnd) in _SKIP_CLASSES:
        return None
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    if pid == psutil.Process().pid:
        return None
    try:
        exe = Path(psutil.Process(pid).name()).stem.lower()
    except (psutil.Error, OSError):
        return None
    if exe in _SKIP_EXE:
        return None
    return Win(hwnd, win32gui.GetWindowText(hwnd).strip(), exe)


def fullscreen_foreground():
    """True — активное окно занимает весь монитор без рамок (полноэкранная
    игра или видео). Развёрнутое (maximized) обычное окно сюда НЕ попадает:
    при нём доступна панель задач, и это не игра на весь экран."""
    import win32api

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd or win32gui.GetClassName(hwnd) in _SKIP_CLASSES:
        return False
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == psutil.Process().pid:
            return False
        # развёрнутое окно вылезает за край монитора на ширину рамки и прошло бы
        # проверку ниже — поэтому maximized отсекаем сразу
        if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED:
            return False
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        mon = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        ml, mt, mr, mb = win32api.GetMonitorInfo(mon)["Monitor"]
    except Exception:
        return False
    return left <= ml and top <= mt and right >= mr and bottom >= mb


def focus(win):
    """Вывести окно вперёд. Windows не даёт фоновому процессу перехватывать
    фокус — обходим стандартно: короткое нажатие Alt «разрешает» смену."""
    if win32gui.IsIconic(win.hwnd):
        win32gui.ShowWindow(win.hwnd, win32con.SW_RESTORE)
    user32 = ctypes.windll.user32
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    try:
        win32gui.SetForegroundWindow(win.hwnd)
    except Exception:
        win32gui.BringWindowToTop(win.hwnd)


def minimize(win):
    win32gui.ShowWindow(win.hwnd, win32con.SW_MINIMIZE)


def close(win):
    win32gui.PostMessage(win.hwnd, win32con.WM_CLOSE, 0, 0)


def minimize_all():
    import win32com.client
    win32com.client.Dispatch("Shell.Application").MinimizeAll()


def restore_all():
    import win32com.client
    win32com.client.Dispatch("Shell.Application").UndoMinimizeALL()


def _grid(n):
    """Сколько строк и колонок: до трёх — колонками рядом, дальше — сеткой
    пошире (экран широкий)."""
    rows = 1 if n <= 3 else 2 if n <= 8 else 3
    return rows, -(-n // rows)


def _frame_margins(hwnd):
    """Невидимая рамка окна (тень Windows 10/11): SetWindowPos ставит окно
    вместе с ней, и между окнами остаются щели. Вернуть (лево, верх, право, низ)."""
    rect = ctypes.wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect),
                                                  ctypes.sizeof(rect)):
        return 0, 0, 0, 0
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    return rect.left - l, rect.top - t, r - rect.right, b - rect.bottom


def tile(wins):
    """Разложить окна по экрану, на котором первое из них: одно — на весь
    экран, два-три — колонками, больше — сеткой. Порядок — слева направо,
    как стояли, чтобы окна не перепрыгивали при повторной раскладке."""
    import win32api

    if not wins:
        return 0
    user32 = ctypes.windll.user32
    # Джарвис не DPI-aware: без этого при масштабе 125% координаты «плывут»
    old_ctx = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        if len(wins) == 1:
            win32gui.ShowWindow(wins[0].hwnd, win32con.SW_MAXIMIZE)
            return 1
        for w in wins:
            if win32gui.IsIconic(w.hwnd) or user32.IsZoomed(w.hwnd):
                win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
        wins = sorted(wins, key=lambda w: win32gui.GetWindowRect(w.hwnd)[:2])
        mon = win32api.MonitorFromWindow(wins[0].hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        x0, y0, x1, y1 = win32api.GetMonitorInfo(mon)["Work"]
        rows, cols = _grid(len(wins))
        height = (y1 - y0) // rows
        for i, w in enumerate(wins):
            row = i // cols
            # в неполной последней строке окна пошире — без пустой дыры
            in_row = min(cols, len(wins) - row * cols)
            col = i - row * cols
            width = (x1 - x0) // in_row
            left, top = x0 + col * width, y0 + row * height
            right = x1 if col == in_row - 1 else left + width
            bottom = y1 if row == rows - 1 else top + height
            ml, mt, mr, mb = _frame_margins(w.hwnd)
            win32gui.SetWindowPos(w.hwnd, 0, left - ml, top - mt,
                                  right - left + ml + mr, bottom - top + mt + mb,
                                  win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        return len(wins)
    finally:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old_ctx))


def running(process_names):
    """Какие из процессов (имена без .exe) сейчас запущены."""
    wanted = {p.lower() for p in process_names}
    alive = set()
    for proc in psutil.process_iter(["name"]):
        stem = Path(proc.info["name"] or "").stem.lower()
        if stem in wanted:
            alive.add(stem)
    return alive


def close_processes(process_names, wait=5.0):
    """Вежливо закрыть все окна этих программ. Вернуть, что закрыли."""
    wanted = {p.lower() for p in process_names}
    targets = [w for w in list_windows() if w.exe in wanted]
    for w in targets:
        close(w)
    deadline = time.time() + wait
    while time.time() < deadline and running(wanted) & {w.exe for w in targets}:
        time.sleep(0.3)
    return sorted({w.exe for w in targets})
