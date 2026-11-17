# -*- coding: utf-8 -*-
"""«Что на экране?», «переведи выделенное», «объясни это».

Источник — выделенный текст (копируем его из активного окна и возвращаем
прежний буфер обмена) или снимок активного окна. Дальше — подключённый ИИ
(brain.task): бесплатный Gemini понимает и текст, и картинки.

Снимок и выделенное уходят тому ИИ, который отвечает, — в журнал Джарвиса не
пишутся ни они, ни ответ (там бывают пароли и данные клиентов).
"""

import ctypes
import io
import logging
import re
import time
from ctypes import wintypes

import win32gui

import ai
import brain
import sendkeys
import windows

log = logging.getLogger("jarvis.look")

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
_dwm = ctypes.WinDLL("dwmapi")
_dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_dwm.DwmGetWindowAttribute.restype = ctypes.c_long

MAX_SIDE = 1600          # px: крупнее ИИ всё равно уменьшит, а грузится дольше
MAX_TEXT = 6000          # символов выделенного — больше ИИ не отправляем
SPEAK_LIMIT = 700        # длиннее — говорим начало, целиком кладём в буфер

# в терминалах Ctrl+C без выделения — это «прервать программу», поэтому туда
# только Ctrl+Insert (копирует, а без выделения ничего не делает)
_TERMINALS = {"code", "windowsterminal", "conhost", "cmd", "powershell", "pwsh", "mintty",
              "putty", "wezterm-gui", "alacritty", "conemu64", "conemu", "kitty", "tabby",
              "cursor", "windsurf"}

# --------------------------------------------------------------- разбор фразы

_VERB = {
    "translate": r"(?:переведи|перевод|переведи\s+мне)",
    "read": r"(?:прочитай|прочти|зачитай|прочитай\s+мне)",
    "explain": r"(?:объясни|поясни|растолкуй|что\s+(?:это\s+)?(?:значит|означает)|что\s+это(?:\s+такое)?)",
    "summary": r"(?:перескажи|кратко\s+перескажи|о\s+чем\s+(?:это|тут|здесь))",
}
_ALONE = {"explain": r"(?:объясни|поясни|растолкуй|что\s+это(?:\s+такое)?)"}
_SEL = r"(?:выделенное|выделенный\s+текст|выделенный|выделение|что\s+выделено|что\s+я\s+выделил\w*|выделенное\s+мной)"
_CLIP = r"(?:из\s+буфера|буфер(?:\s+обмена)?|что\s+в\s+буфере|скопированное|что\s+скопировал\w*)"
_SCREEN = r"(?:(?:что\s+)?(?:у\s+меня\s+)?на\s+экране|экран|с\s+экрана|(?:что\s+)?в\s+окне|окно)"
_LANG = r"(?:\s+на\s+(?P<lang>\w+(?:ий|ой|ый))(?:\s+язык)?)?"
_THIS = r"(?:\s+(?:это|вот\s+это|мне|пожалуйста))*"

# вопрос про экран — только с вопросительным словом в начале: «выключи экран»
# или «яркость экрана» сюда попадать не должны
_SCREEN_ASK = re.compile(
    r"^(?:а\s+)?(?:что|кто|какой|какая|какое|какие|сколько|где|почему|зачем|как|о\s+чем|"
    r"посмотри|глянь|скажи)\b.*\b(?:на\s+экране|в\s+(?:этом\s+)?окне|на\s+(?:этой\s+)?картинке)\b"
    r"|^(?:посмотри|глянь)\s+на\s+экран\b"
    r"|^что\s+(?:тут|здесь)\s+(?:написано|происходит|не\s+так)\b"
    r"|^(?:что\s+за|какая)\s+(?:тут\s+|здесь\s+|это\s+)?ошибк\w*$"
    r"|^почему\s+(?:тут\s+|здесь\s+)?ошибк\w*$")


def parse(t):
    """(откуда, что сделать, язык) или None. Откуда: selection — выделенное
    (нет — экран), clipboard — буфер, screen — экран."""
    for task, verb in _VERB.items():
        for source, obj in (("selection", _SEL), ("clipboard", _CLIP), ("screen", _SCREEN)):
            # язык — до или после: «переведи на английский выделенное» / «переведи выделенное на английский»
            m = re.match(rf"^{verb}{_THIS}{_LANG}\s+{obj}{_LANG.replace('lang', 'lang2')}$", t)
            if m:
                lang = m.groupdict().get("lang") or m.groupdict().get("lang2")
                return source, task, lang
        # без объекта: «переведи», «переведи это на английский», «что это такое» —
        # выделенное, а не выделено ничего — экран. Только коротко: «объясни,
        # что такое квантовая физика» — это вопрос ИИ, а не про экран. И без
        # «что это значит»: так обычно переспрашивают ответ ИИ
        alone = _ALONE.get(task, verb)
        m = re.match(rf"^{alone}{_THIS}{_LANG}$", t)
        if m:
            return "selection", task, m.group("lang")
    if _SCREEN_ASK.search(t):
        return "screen", "ask", None
    return None


# ------------------------------------------------------------ буфер обмена

def _open_clipboard():
    import win32clipboard

    for _ in range(10):               # буфер бывает занят другой программой
        try:
            win32clipboard.OpenClipboard()
            return True
        except Exception:
            time.sleep(0.05)
    return False


def _save_clipboard():
    """Всё, что лежит в буфере и читается как данные: текст, HTML, картинка
    (DIB, PNG), RTF. Файлы (Проводник) и ручки GDI не сохраняются."""
    import win32clipboard

    if not _open_clipboard():
        return None
    saved = []
    try:
        fmt = win32clipboard.EnumClipboardFormats(0)
        while fmt:
            if fmt not in (2, 3, 9, 14, 15):     # BITMAP, METAFILEPICT, PALETTE, ENHMETAFILE, HDROP
                try:
                    data = win32clipboard.GetClipboardData(fmt)
                    if isinstance(data, (str, bytes)):
                        saved.append((fmt, data))
                except Exception:
                    pass
            fmt = win32clipboard.EnumClipboardFormats(fmt)
    finally:
        win32clipboard.CloseClipboard()
    return saved


def _restore_clipboard(saved):
    import win32clipboard

    if saved is None or not _open_clipboard():
        return
    try:
        win32clipboard.EmptyClipboard()
        for fmt, data in saved:
            try:
                win32clipboard.SetClipboardData(fmt, data)
            except Exception:
                pass
    finally:
        win32clipboard.CloseClipboard()


def clipboard_text():
    import win32clipboard

    if not _open_clipboard():
        return ""
    try:
        if win32clipboard.IsClipboardFormatAvailable(13):        # CF_UNICODETEXT
            return win32clipboard.GetClipboardData(13) or ""
        return ""
    except Exception:
        return ""
    finally:
        win32clipboard.CloseClipboard()


def set_clipboard_text(text):
    import win32clipboard

    if not _open_clipboard():
        return False
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(13, text)
        return True
    finally:
        win32clipboard.CloseClipboard()


def _wait_change(before, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if user32.GetClipboardSequenceNumber() != before:
            time.sleep(0.08)          # программа кладёт форматы по очереди
            return True
        time.sleep(0.03)
    return False


def copy_selection():
    """Скопировать выделенное в активном окне и вернуть прежний буфер.
    Вернуть текст или "" — ничего не выделено или окно копировать не даёт."""
    win = windows.foreground()
    exe = win.exe if win else ""
    saved = _save_clipboard()
    before = user32.GetClipboardSequenceNumber()
    try:
        sendkeys.combo(sendkeys.VK_CONTROL, sendkeys.VK_INSERT)
        changed = _wait_change(before, 0.5)
        if not changed and exe and exe not in _TERMINALS:
            # Ctrl+Insert понимают не все программы
            sendkeys.combo(sendkeys.VK_CONTROL, ord("C"))
            changed = _wait_change(before, 0.6)
        text = clipboard_text() if changed else ""
    finally:
        if user32.GetClipboardSequenceNumber() != before:
            _restore_clipboard(saved)
    return text.strip()


# ------------------------------------------------------------------ снимок

class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def grab_window():
    """Снимок активного окна (JPEG). Окна нет (рабочий стол) — монитор, где
    курсор. Вернуть (байты, подпись окна)."""
    from PIL import ImageGrab
    import win32api
    import win32con

    old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))   # реальные пиксели
    try:
        win = windows.foreground()
        box = None
        if win is not None and not win32gui.IsIconic(win.hwnd):
            r = _RECT()
            # рамка без невидимых краёв DWM — иначе в снимок попадает фон
            if _dwm.DwmGetWindowAttribute(win.hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) == 0:
                box = (r.left, r.top, r.right, r.bottom)
        if box is None or box[2] - box[0] < 50 or box[3] - box[1] < 50:
            mon = win32api.MonitorFromPoint(win32api.GetCursorPos(), win32con.MONITOR_DEFAULTTONEAREST)
            box = tuple(win32api.GetMonitorInfo(mon)["Monitor"])
            win = None
        img = ImageGrab.grab(bbox=box, all_screens=True)
    finally:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue(), (win.title if win else "")


# ------------------------------------------------------------------ задания

_LANG_FIX = {"английский": "английский", "англиский": "английский", "русский": "русский",
             "казахский": "казахский", "казакский": "казахский"}


def _prompt(task, lang, what, question=""):
    """what — «Текст» или «На картинке — снимок окна»."""
    target = _LANG_FIX.get(lang or "", lang)
    if task == "translate":
        to = (f"на {target} язык" if target else
              "на русский язык (если он уже на русском — на английский)")
        return (f"{what}. Переведи {'основной текст с неё' if 'картинк' in what else 'его'} {to}. "
                "Ответ — только перевод.")
    if task == "read":
        return (f"{what}. Прочитай дословно основной текст (без меню, кнопок и рекламы). "
                "Если текста много — первые абзацы.")
    if task == "explain":
        return f"{what}. Объясни простыми словами, что это и что это значит — два-четыре предложения."
    if task == "summary":
        return f"{what}. Перескажи главное в двух-трёх предложениях."
    return (f"{what}. Человек спрашивает голосом: «{question}». Ответь по картинке коротко, "
            "одно-три предложения. Если на ней ошибка — скажи, в чём она и что сделать.")


def _speak_long(raw):
    """Длинный ответ: начало вслух, весь — в буфер обмена (как есть, с абзацами)."""
    text = brain.clean(raw)
    if len(text) <= SPEAK_LIMIT:
        return text
    set_clipboard_text(raw.strip())
    cut = text[:SPEAK_LIMIT]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    cut = cut[:end + 1] if end > SPEAK_LIMIT // 3 else cut.rsplit(" ", 1)[0] + "…"
    return cut + " Дальше длинно — целиком положил в буфер обмена."


def run(source, task, lang, phrase, cfg):
    """Выполнить и вернуть ответ вслух. Вызывается в рабочем потоке Джарвиса."""
    text = ""
    if source == "selection":
        text = copy_selection()
        if not text:
            source = "screen"              # ничего не выделено — смотрим на экран
    elif source == "clipboard":
        text = clipboard_text().strip()
        if not text:
            return "В буфере обмена нет текста, сэр"

    if text:
        log.info("%s: выделено %d символов", task, len(text))
        if task == "read":
            return _speak_long(text)       # прочитать можно и без ИИ
        if not brain.available(cfg):
            return "Для этого нужен подключённый ИИ, сэр. Подключите его в панели"
        cut = text[:MAX_TEXT]
        what = f"Текст:\n\"\"\"\n{cut}\n\"\"\"" + ("\n(текст обрезан)" if len(text) > MAX_TEXT else "")
        answer = brain.task(_prompt(task, lang, what, phrase), cfg)
        return _speak_long(answer) if answer else "Не получилось, сэр: ИИ не ответил"

    if not any(ai.sees_images(p) for p in brain.ready(cfg)):
        return "Смотреть на экран я могу только с ИИ, который видит картинки, — например, Gemini"
    shot, title = grab_window()
    log.info("%s: снимок окна, %d КБ", task, len(shot) // 1024)
    what = "На картинке — снимок окна" + (f" «{title}»" if title else "") + " с экрана пользователя"
    answer = brain.task(_prompt(task, lang, what, phrase), cfg, image=ai.image(shot), timeout=20)
    return _speak_long(answer) if answer else "Не получилось, сэр: ИИ не ответил"
