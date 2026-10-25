# -*- coding: utf-8 -*-
"""Браузер: открыть сайт, поиск или вкладку — всегда в браузере по умолчанию.

Ничего не запускает принудительно: webbrowser.open отдаёт ссылку системному
браузеру по умолчанию (тому самому, что открывается по ссылке из письма).

Сайты можно называть словами («открой в браузере телеграм») — список в SITES,
туда же дописывайте свои. Не из списка — пробуем как адрес («гугл точка ком»),
а совсем непонятное уходит в поиск, чтобы команда не пропадала впустую.
"""

import logging
import re
import time
import webbrowser
from urllib.parse import quote

log = logging.getLogger("jarvis.browser")

SEARCH_URL = "https://www.google.com/search?q="

# процессы браузеров — чтобы открыть новую вкладку в уже запущенном окне
_BROWSER_EXES = ("chrome", "msedge", "firefox", "opera", "brave", "vivaldi",
                 "yandex", "browser")

# Сайты по имени: как слышит распознавание (нормализовано) -> адрес.
# Дописывайте свои — ключ в нижнем регистре, «ё» как «е», без знаков.
SITES = {
    "телеграм": "https://web.telegram.org",
    "телеграмм": "https://web.telegram.org",
    "телега": "https://web.telegram.org",
    "телегу": "https://web.telegram.org",
    "ютуб": "https://www.youtube.com",
    "ютюб": "https://www.youtube.com",
    "гугл": "https://www.google.com",
    "гугл почта": "https://mail.google.com",
    "гмайл": "https://mail.google.com",
    "джимейл": "https://mail.google.com",
    "почта": "https://mail.google.com",
    "гугл диск": "https://drive.google.com",
    "диск": "https://drive.google.com",
    "гугл карты": "https://maps.google.com",
    "карты": "https://yandex.ru/maps",
    "яндекс карты": "https://yandex.ru/maps",
    "переводчик": "https://translate.google.com",
    "гугл переводчик": "https://translate.google.com",
    "яндекс": "https://ya.ru",
    "яндекс почта": "https://mail.yandex.ru",
    "маил": "https://mail.ru",
    "майл": "https://mail.ru",
    "вконтакте": "https://vk.com",
    "вк": "https://vk.com",
    "одноклассники": "https://ok.ru",
    "ватсап": "https://web.whatsapp.com",
    "вацап": "https://web.whatsapp.com",
    "инстаграм": "https://www.instagram.com",
    "фейсбук": "https://www.facebook.com",
    "твиттер": "https://twitter.com",
    "икс": "https://x.com",
    "гитхаб": "https://github.com",
    "википедия": "https://ru.wikipedia.org",
    "вики": "https://ru.wikipedia.org",
    "чат гпт": "https://chatgpt.com",
    "чатгпт": "https://chatgpt.com",
    "клод": "https://claude.ai",
    "дискорд": "https://discord.com/app",
    "твич": "https://www.twitch.tv",
    "нетфликс": "https://www.netflix.com",
    "озон": "https://www.ozon.ru",
    "вайлдберриз": "https://www.wildberries.ru",
    "авито": "https://www.avito.ru",
    "кинопоиск": "https://www.kinopoisk.ru",
    "авиасейлс": "https://www.aviasales.ru",
    "два гис": "https://2gis.ru",
    "дубльгис": "https://2gis.ru",
}

# «гугл точка ком», «пример точ ру», «site dot com»
_DOMAIN = re.compile(r"^([a-zа-я0-9][a-zа-я0-9-]*)\s+(?:точка|точ|dot)\s+"
                     r"(ru|com|org|net|kz|io|рф|ру|ком|орг|нет|кз)$")
_TLD = {"ру": "ru", "ком": "com", "орг": "org", "нет": "net", "кз": "kz", "рф": "рф"}
# уже похоже на адрес: «youtube.com», «vk.com/feed» (из набранного текста)
_LOOKS_LIKE_URL = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Zрф]{2,}(/\S*)?$")


def _norm(s):
    """Как в skills: нижний регистр, «ё»→«е», только буквы-цифры и пробелы."""
    return re.sub(r"[^a-zа-я0-9 ]", " ", (s or "").lower().replace("ё", "е")).strip()


def _norm_spaces(s):
    return re.sub(r"\s+", " ", _norm(s)).strip()


SITES = {_norm_spaces(k): v for k, v in SITES.items()}


def to_url(target):
    """Строка (имя сайта / домен / готовый URL) -> URL, или None, если непонятно."""
    raw = (target or "").strip()
    if not raw:
        return None
    if "://" in raw:
        return raw
    s = _norm_spaces(raw)
    if s in SITES:
        return SITES[s]
    m = _DOMAIN.match(s)
    if m:
        label = m.group(1)
        if label in SITES:                 # «ютуб точка ком» -> известный сайт
            return SITES[label]
        if label.isascii():                # «example точка ком» -> example.com
            return "https://" + label + "." + _TLD.get(m.group(2), m.group(2))
        return None                        # кириллица без совпадения — не угадать
    if _LOOKS_LIKE_URL.match(raw):
        return "https://" + raw
    return None


def open_site(spoken):
    """«открой в браузере телеграм». Не узнали сайт — ищем его в браузере."""
    url = to_url(spoken)
    if url:
        webbrowser.open(url)
        log.info("Открываю в браузере: %s", url)
        return f"Открываю {spoken} в браузере, сэр"
    log.info("Сайт «%s» не распознан — открываю поиск", spoken)
    webbrowser.open(SEARCH_URL + quote(spoken))
    return f"Не знаю такого сайта, ищу «{spoken}» в браузере"


def search(query):
    """«загугли …», «найди в браузере …» — поиск в браузере по умолчанию."""
    webbrowser.open(SEARCH_URL + quote(query))
    log.info("Поиск в браузере: %s", query)
    return f"Ищу в браузере: {query}"


def open_tab(target=""):
    """«открой вкладку» — пустую; «открой вкладку <сайт>» — сразу с сайтом."""
    target = (target or "").strip()
    if not target:
        _new_blank_tab()
        return "Открыл новую вкладку, сэр"
    url = to_url(target) or (SEARCH_URL + quote(target))
    webbrowser.open_new_tab(url)
    log.info("Новая вкладка: %s", url)
    return f"Открыл вкладку: {target}"


def open_url(target):
    """Для шага режима «Открыть сайт»: имя сайта, домен, URL или поиск.
    True — что-то открыли, False — пустая строка."""
    url = to_url(target)
    if url:
        webbrowser.open(url)
        return True
    if (target or "").strip():
        webbrowser.open(SEARCH_URL + quote(target.strip()))
        return True
    return False


def _ctrl_t():
    import ctypes

    user32 = ctypes.windll.user32
    user32.keybd_event(0x11, 0, 0, 0)        # Ctrl вниз
    user32.keybd_event(0x54, 0, 0, 0)        # T вниз
    user32.keybd_event(0x54, 0, 2, 0)        # T вверх
    user32.keybd_event(0x11, 0, 2, 0)        # Ctrl вверх


def _new_blank_tab():
    """Пустая вкладка в уже открытом браузере (Ctrl+T), иначе — запустить браузер."""
    try:
        import windows

        wins = [w for w in windows.list_windows()
                if any(b in w.exe for b in _BROWSER_EXES)]
        if wins:
            windows.focus(wins[0])
            time.sleep(0.15)
            _ctrl_t()
            return
    except Exception:
        log.exception("Не смог открыть вкладку в активном браузере")
    webbrowser.open_new("about:blank")
