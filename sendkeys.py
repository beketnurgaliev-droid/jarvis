# -*- coding: utf-8 -*-
"""Нажатия клавиш в активное окно — как будто печатает человек.

Через SendInput: сочетания (Ctrl+Insert) и набор любого текста по буквам
(KEYEVENTF_UNICODE — раскладка клавиатуры не важна, русский и английский
печатаются одинаково).

Структуры описаны полностью и у SendInput заданы argtypes: без этого на
64-битном Python ctypes молча передаёт мусор (см. грабли в perf.py).
"""

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12          # Alt
VK_RETURN = 0x0D
VK_INSERT = 0x2D
VK_LWIN = 0x5B
VK_RWIN = 0x5C


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    # мышь — самая большая часть объединения; без неё размер INPUT неверный,
    # и SendInput отказывается работать
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


def _key(vk=0, scan=0, flags=0):
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)))


def _send(events):
    if not events:
        return 0
    arr = (INPUT * len(events))(*events)
    return user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def _modifiers_held():
    """Человек держит Ctrl/Alt/Shift/Win — наши буквы стали бы сочетаниями."""
    return any(user32.GetAsyncKeyState(vk) & 0x8000
               for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN))


def combo(*vks):
    """Сочетание: combo(VK_CONTROL, VK_INSERT) — нажать по порядку, отпустить
    в обратном. Вернуть, сколько событий Windows приняла."""
    down = [_key(vk) for vk in vks]
    up = [_key(vk, flags=KEYEVENTF_KEYUP) for vk in reversed(vks)]
    return _send(down + up)


def type_text(text, chunk=40, pause=0.01):
    """Напечатать текст в активное окно. Перевод строки — Shift+Enter: в
    мессенджерах простой Enter отправил бы сообщение недописанным.
    Вернуть (сколько событий Windows приняла, сколько отправляли) — если
    меньше, ввод заблокирован (например, окно запущено от администратора)."""
    for _ in range(20):                 # подождать, пока отпустят клавиши (до 1 с)
        if not _modifiers_held():
            break
        time.sleep(0.05)
    events = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            events += [_key(VK_SHIFT), _key(VK_RETURN),
                       _key(VK_RETURN, flags=KEYEVENTF_KEYUP), _key(VK_SHIFT, flags=KEYEVENTF_KEYUP)]
            continue
        # символы за пределами BMP (эмодзи) — двумя половинками UTF-16
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            events += [_key(scan=unit, flags=KEYEVENTF_UNICODE),
                       _key(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    sent = 0
    # порциями: иначе некоторые программы (1С, старые Qt) теряют буквы
    step = chunk * 2
    for i in range(0, len(events), step):
        sent += _send(events[i:i + step])
        time.sleep(pause)
    return sent, len(events)
