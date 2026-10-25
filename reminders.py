# -*- coding: utf-8 -*-
"""Таймеры и напоминания: «таймер на 5 минут», «напомни в 18 30 выпить таблетку».

Живут в reminders.json рядом с модулем и переживают перезапуск. Срабатывают
в фоновом потоке: start(notify) — и notify(текст) говорит его вслух.
handle(t, cfg) получает уже нормализованную фразу (как skills._norm).
"""

import datetime
import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path

from info import number_at

log = logging.getLogger("jarvis.reminders")

FILE = Path(__file__).resolve().parent / "reminders.json"
_now = datetime.datetime.now     # тесты подменяют своими часами
_MISSED_AFTER = 60               # при запуске опоздали больше минуты — «пропущенное»
_MAX_SLEEP = 30                  # поток всё равно просыпается: сон, перевод часов

_lock = threading.RLock()
_items = None                    # [{"id", "kind", "text", "at", "created", "dur"}]
_notify = None
_thread = None
_wake = threading.Event()


def _norm(s):
    return re.sub(r"[^a-zа-я0-9 ]", " ", s.lower().replace("ё", "е")).strip()


def _plural(n, one, few, many):
    tail = abs(n) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _cap(s):
    return s[:1].upper() + s[1:]


# --------------------------------------------------------------------------
# Хранение
# --------------------------------------------------------------------------

def _at(item):
    return datetime.datetime.fromisoformat(item["at"])


def _valid(x):
    try:
        return (isinstance(x, dict) and x.get("kind") in ("timer", "reminder")
                and isinstance(x.get("id"), str) and _at(x) is not None)
    except (TypeError, ValueError):
        return False


def _load():
    """Список под замком; файл читаем один раз. Нет файла или битый — пусто."""
    global _items
    if _items is None:
        try:
            data = json.loads(FILE.read_text(encoding="utf-8"))
            _items = [x for x in data if _valid(x)] if isinstance(data, list) else []
        except FileNotFoundError:
            _items = []
        except (OSError, ValueError):
            log.warning("reminders.json не читается — начинаю с пустого списка")
            _items = []
    return _items


def _save():
    tmp = FILE.with_name(FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(_items, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, FILE)
    except OSError:
        log.exception("Не смог сохранить напоминания")


def _add(kind, at, text, dur=None):
    at = (at + datetime.timedelta(microseconds=500000)).replace(microsecond=0)
    item = {"id": uuid.uuid4().hex[:8], "kind": kind, "text": text,
            "at": at.isoformat(timespec="seconds"),
            "created": _now().isoformat(timespec="seconds")}
    if dur is not None:
        item["dur"] = int(dur)
    with _lock:
        _load().append(item)
        _save()
    _wake.set()
    return item


def _remove(items_to_drop):
    ids = {it["id"] for it in items_to_drop}
    with _lock:
        items = _load()
        items[:] = [it for it in items if it["id"] not in ids]
        _save()
    _wake.set()


def pending():
    """Для панели: [{"id", "kind", "text", "at", "left_s"}] по времени."""
    now = _now()
    with _lock:
        items = sorted(_load(), key=_at)
        return [{"id": it["id"], "kind": it["kind"], "text": it["text"], "at": it["at"],
                 "left_s": max(0, int(round((_at(it) - now).total_seconds())))}
                for it in items]


def cancel(rid):
    """Удалить по id. Вернуть True, если было что удалять."""
    with _lock:
        items = _load()
        for k, it in enumerate(items):
            if it["id"] == rid:
                del items[k]
                _save()
                _wake.set()
                return True
    return False


# --------------------------------------------------------------------------
# Срабатывание
# --------------------------------------------------------------------------

def _fire_text(item, missed=False):
    text = item.get("text") or ""
    if item["kind"] == "timer":
        return f"Пропущенный {text}." if missed else f"Сэр, время: {text}."
    head = "Пропущенное напоминание" if missed else "Сэр, напоминаю"
    if not text:
        return f"{head}." if missed else "Сэр, вы просили напомнить."
    # «напомни о встрече» — без двоеточия
    sep = " " if re.match(r"^(?:о|об|про)\b", text) else ": "
    return f"{head}{sep}{text}."


def _take_due(first=False):
    """Забрать сработавшие и вернуть фразы. first — сразу после запуска:
    сильно опоздавшие — «пропущенные»."""
    now = _now()
    with _lock:
        items = _load()
        due = sorted((it for it in items if _at(it) <= now), key=_at)
        if not due:
            return []
        items[:] = [it for it in items if _at(it) > now]
        _save()
    return [_fire_text(it, missed=first and (now - _at(it)).total_seconds() > _MISSED_AFTER)
            for it in due]


def _say(text):
    log.info("Напоминание: %s", text)
    if _notify is None:
        return
    try:
        _notify(text)
    except Exception:
        log.exception("notify упал")


def _sleep_time():
    with _lock:
        items = _load()
        if not items:
            return _MAX_SLEEP
        left = (min(_at(it) for it in items) - _now()).total_seconds()
    return max(0.05, min(_MAX_SLEEP, left))


def _loop():
    first = True
    while True:
        _wake.clear()
        try:
            for text in _take_due(first):
                _say(text)
        except Exception:
            log.exception("Сбой в потоке напоминаний")
        first = False
        _wake.wait(_sleep_time())


def start(notify, cfg=None):
    """Запустить фоновый поток (повторный вызов только меняет notify).
    Звать, когда голос уже готов: пропущенные прозвучат сразу."""
    global _notify, _thread
    with _lock:
        _notify = notify
        _load()
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, daemon=True, name="reminders")
        _thread.start()


# --------------------------------------------------------------------------
# Время словами
# --------------------------------------------------------------------------

_UNITS = {
    "секунда": 1, "секунду": 1, "секунды": 1, "секунд": 1, "секундочку": 1, "сек": 1,
    "минута": 60, "минуту": 60, "минуты": 60, "минут": 60, "минутку": 60,
    "минутки": 60, "минуток": 60, "мин": 60,
    "час": 3600, "часа": 3600, "часов": 3600, "часик": 3600, "часика": 3600,
    "день": 86400, "дня": 86400, "дней": 86400, "сутки": 86400, "суток": 86400,
    "неделю": 604800, "недели": 604800, "недель": 604800,
}
# «через час», «на минутку» — без числа
_SINGLE = {"час", "часик", "минуту", "минутку", "секунду", "секундочку", "день",
           "сутки", "неделю"}
_HALF = {"полчаса": 1800, "полчасика": 1800, "полминуты": 30, "полдня": 43200}

_ORD_GEN = {"первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5,
            "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9, "десятого": 10,
            "одиннадцатого": 11, "двенадцатого": 12}
_PERIODS = ("утра", "дня", "вечера", "ночи")
_PARTS = {"утром": (9, 0), "днем": (13, 0), "вечером": (19, 0), "ночью": (23, 0)}
_WEEKDAYS = {"понедельник": 0, "вторник": 1, "среду": 2, "четверг": 3, "пятницу": 4,
             "субботу": 5, "воскресенье": 6}
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]


def _num(w, i):
    if i < len(w) and w[i] in ("пару", "пара", "парочку"):
        return 2, i + 1
    return number_at(w, i)


def _duration(w, i):
    """«5 минут», «1 час 20 минут», «полтора часа», «полчаса», «час двадцать»,
    «два с половиной часа». Вернуть (секунды, где кончилось) или (None, i)."""
    total, last, j, n = 0, None, i, len(w)
    while j < n:
        word = w[j]
        if word in _HALF:
            total, last, j = total + _HALF[word], None, j + 1
            continue
        if word in _SINGLE:
            total, last, j = total + _UNITS[word], _UNITS[word], j + 1
            continue
        if word == "с" and w[j + 1:j + 2] == ["половиной"] and last:
            total, last, j = total + last / 2, None, j + 2
            continue
        if word == "и" and total:
            # «час и 20 минут» — «и» берём, только если дальше снова срок
            sec, k = _duration(w, j + 1)
            if sec:
                total, j = total + sec, k
            break
        num, k = _num(w, j)
        if num is None:
            break
        if k < n and w[k] in _UNITS:
            total, last, j = total + num * _UNITS[w[k]], _UNITS[w[k]], k + 1
            continue
        if w[k:k + 2] == ["с", "половиной"] and k + 2 < n and w[k + 2] in _UNITS:
            total, last, j = total + (num + 0.5) * _UNITS[w[k + 2]], None, k + 3
            continue
        # «час двадцать»: число без единицы после часов — минуты
        if last == 3600 and isinstance(num, int) and 0 < num < 60:
            total, last, j = total + num * 60, None, k
            continue
        break
    if total <= 0:
        return None, i
    return int(round(total)), j


def _apply_period(h, period, bare_hour=False):
    if period == "утра":
        return 0 if h == 12 else h
    if period == "дня":
        return h + 12 if 1 <= h <= 6 else h
    if period == "вечера":
        return h + 12 if 1 <= h < 12 else h
    if period == "ночи":
        return 0 if h == 12 else (h + 12 if 9 <= h <= 11 else h)
    return 13 if bare_hour else h    # «в час» без уточнения — днём


def _clock(w, i):
    """«18 30», «восемнадцать тридцать», «шесть вечера», «9 утра», «полдень»,
    «час дня», «половине восьмого». Вернуть ((час, минуты), где кончилось) или None."""
    n = len(w)
    if i >= n:
        return None
    word, bare = w[i], False
    if word == "полдень":
        return (12, 0), i + 1
    if word == "полночь":
        return (0, 0), i + 1
    if word == "половине" and i + 1 < n and w[i + 1] in _ORD_GEN:
        h, m, j = _ORD_GEN[w[i + 1]] - 1, 30, i + 2
    elif word == "час":
        h, m, j, bare = 1, 0, i + 1, True
    else:
        h, j = number_at(w, i)
        if not isinstance(h, int):
            return None
        m = 0
        if h >= 100 and word.isdigit() and len(word) in (3, 4):   # «1830»
            h, m = divmod(h, 100)
        elif h > 24:
            return None
        else:
            if j < n and w[j] in ("час", "часа", "часов"):
                j += 1
            mm, k = number_at(w, j)
            if isinstance(mm, int) and mm < 60:
                m, j = mm, k
                if mm == 0 and j < n:          # «шесть ноль пять», «девятнадцать ноль ноль»
                    z, k = number_at(w, j)
                    if isinstance(z, int) and z < 10:
                        m, j = z, k
                if j < n and w[j] in ("минут", "минуты", "минута", "минуту"):
                    j += 1
    period = w[j] if j < n and w[j] in _PERIODS else None
    if period:
        j += 1
    h = _apply_period(h, period, bare and not period)
    if h == 24:
        h = 0
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return (h, m), j


def _day(w, i, now):
    """«сегодня», «завтра», «послезавтра», «в пятницу», «25 октября».
    Вернуть (дата, на сколько дней сдвинуть, если время уже прошло, где кончилось)."""
    n = len(w)
    if i >= n:
        return None
    today = now.date()
    if w[i] == "на" and i + 1 < n and w[i + 1] in ("сегодня", "завтра", "послезавтра"):
        i += 1
    shift = {"сегодня": 0, "завтра": 1, "послезавтра": 2}.get(w[i])
    if shift is not None:
        return today + datetime.timedelta(days=shift), 1, i + 1
    k = i + 1 if w[i] in ("в", "во", "на") else i
    if k < n and w[k] in _WEEKDAYS:
        delta = (_WEEKDAYS[w[k]] - today.weekday()) % 7
        return today + datetime.timedelta(days=delta), 7, k + 1
    num, k2 = number_at(w, k)
    if isinstance(num, int) and 1 <= num <= 31 and k2 < n and w[k2] in _MONTHS:
        try:
            d = datetime.date(today.year, _MONTHS.index(w[k2]) + 1, num)
            if d < today:
                d = d.replace(year=today.year + 1)
        except ValueError:
            return None
        return d, 1, k2 + 1
    return None


def _when(w, i, now):
    """«через 20 минут», «в 18 30», «завтра в 10», «в шесть вечера», «в пятницу»,
    «25 октября в 9 утра». Вернуть (когда, секунды для «через» или None, где кончилось)."""
    n = len(w)
    if i < n and w[i] == "через":
        sec, j = _duration(w, i + 1)
        if not sec:
            return None
        return now + datetime.timedelta(seconds=sec), sec, j
    j, hm = i, None
    day = _day(w, j, now)
    if day:
        j = day[2]
    if j < n and w[j] in ("в", "во", "на", "к"):
        got = _clock(w, j + 1)
        if got:
            hm, j = got
    if hm is None and j < n and w[j] in _PARTS:
        hm, j = _PARTS[w[j]], j + 1
    if hm is not None and day is None:          # «в 10 завтра»
        day = _day(w, j, now)
        if day:
            j = day[2]
    if hm is None and day is None:
        return None
    if hm is None:
        hm = (9, 0)                              # «напомни завтра …» — утром
    date, roll = (day[0], day[1]) if day else (now.date(), 1)
    at = datetime.datetime.combine(date, datetime.time(*hm))
    if at <= now:                                # уже прошло — значит, на завтра
        at += datetime.timedelta(days=roll)
    return at, None, j


# --------------------------------------------------------------------------
# Как сказать
# --------------------------------------------------------------------------

def _dur_text(sec):
    """Винительный: «на 5 минут», «через 21 минуту», «на полчаса», «через час»."""
    sec = int(round(sec))
    special = {90: "полторы минуты", 1800: "полчаса", 5400: "полтора часа"}
    if sec in special:
        return special[sec]
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = [f"{x} {_plural(x, *forms)}" for x, forms in (
        (d, ("день", "дня", "дней")), (h, ("час", "часа", "часов")),
        (m, ("минуту", "минуты", "минут")), (s, ("секунду", "секунды", "секунд"))) if x]
    if len(parts) == 1 and parts[0].startswith("1 "):
        return parts[0][2:]
    return " ".join(parts)


def _left_text(sec):
    """Именительный, округлённо: «3 минуты 12 секунд», «1 час 5 минут»."""
    sec = max(1, int(round(sec)))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    D, H, M, S = (("день", "дня", "дней"), ("час", "часа", "часов"),
                  ("минута", "минуты", "минут"), ("секунда", "секунды", "секунд"))
    if d:
        parts = [(d, D), (h, H)]
    elif h:
        parts = [(h, H), (m, M)]
    elif m >= 10:
        parts = [(m, M)]
    else:
        parts = [(m, M), (s, S)]
    return " ".join(f"{x} {_plural(x, *f)}" for x, f in parts if x)


def _clock_text(at, now):
    hm = f"{at.hour}:{at.minute:02d}"
    days = (at.date() - now.date()).days
    if days == 0:
        return f"в {hm}"
    if days == 1:
        return f"завтра в {hm}"
    if days == 2:
        return f"послезавтра в {hm}"
    return f"{at.day} {_MONTHS[at.month - 1]} в {hm}"


def _with_text(head, text):
    """«Напомню в 18:30: выпить таблетку», «Напомню через час о встрече»."""
    if not text:
        return f"{head}."
    sep = " " if re.match(r"^(?:о|об|про)\b", text) else ": "
    return f"{head}{sep}{text}."


# --------------------------------------------------------------------------
# Разбор фразы
# --------------------------------------------------------------------------

_CANCEL = re.compile(r"^(?:отмени|отменить|отмена|удали|удалить|убери|сбрось|сотри|"
                     r"очисти|выключи|останови|отключи|стоп)\s+(.+)$")
_REMIND = re.compile(
    r"^(?:напомни|напомните|напомнить|напоминай)(?:\s+мне)?(?:\s+пожалуйста)?\s+(.+)$|"
    r"^(?:поставь|создай|сделай|добавь|заведи|установи)\s+(?:мне\s+)?напоминание\s+(.+)$|"
    r"^напоминание\s+(.+)$")
_TIMER = re.compile(
    r"^(?:(?:поставь|заведи|запусти|включи|установи|сделай|создай|начни)\s+)?(?:мне\s+)?"
    r"таймер(?:\s+(.+))?$|^засеки(?:\s+мне)?\s+(.+)$")

_LIST_WORDS = {"а", "какие", "какой", "покажи", "перечисли", "список", "мои", "мой",
               "есть", "ли", "у", "меня", "что", "за", "стоят", "стоит", "поставлены",
               "поставлен", "остались", "активные", "сейчас", "текущие", "все", "скажи",
               "назови", "запущены", "запущен", "идут", "идет"}
_KEY_WORDS = {"напоминания": "reminder", "напоминаний": "reminder",
              "напоминание": "reminder", "таймеры": "timer", "таймеров": "timer",
              "таймер": "timer"}
_LEFT_WORDS = {"а", "сколько", "еще", "времени", "осталось", "остается", "на", "до",
               "у", "по", "конца", "мне", "ждать", "таймере", "таймера", "таймеру",
               "таймер", "напоминания", "напоминание", "напоминанию"}
_QUERY_STOP = {"про", "о", "об", "насчет", "на", "в", "во", "с", "что", "чтобы", "все",
               "мое", "мои", "мой", "мне", "это", "этот", "эти", "пожалуйста", "и", "у",
               "меня", "тот", "который", "последний", "последнее", "последние",
               "текущий"}


def _clean_text(words):
    while words and words[0] in ("что", "чтобы", "мне"):
        words = words[1:]
    if words[:2] == ["о", "том"]:
        words = words[2:]
        while words and words[0] in ("что", "чтобы"):
            words = words[1:]
    while words and words[-1] == "пожалуйста":
        words = words[:-1]
    return " ".join(words)


def _list_reply(kinds):
    now = _now()
    with _lock:
        items = sorted((it for it in _load() if it["kind"] in kinds), key=_at)
    if not items:
        return {frozenset({"timer"}): "Таймеров нет, сэр.",
                frozenset({"reminder"}): "Напоминаний нет, сэр."}.get(
                    frozenset(kinds), "Ни таймеров, ни напоминаний нет, сэр.")
    parts = []
    for it in items[:5]:
        if it["kind"] == "timer":
            label = it["text"].replace("таймер ", "", 1)
            parts.append(f"таймер {label}, ещё {_left_text((_at(it) - now).total_seconds())}")
        else:
            parts.append(f"{_clock_text(_at(it), now)} — {it['text'] or 'без текста'}")
    n_t = sum(it["kind"] == "timer" for it in items)
    n_r = len(items) - n_t
    head = []
    if n_t:
        head.append(f"{n_t} {_plural(n_t, 'таймер', 'таймера', 'таймеров')}")
    if n_r:
        head.append(f"{n_r} {_plural(n_r, 'напоминание', 'напоминания', 'напоминаний')}")
    more = f" и ещё {len(items) - 5}" if len(items) > 5 else ""
    return f"{' и '.join(head)}: " + "; ".join(parts) + more + "."


def _left_reply(words):
    now = _now()
    want_r = any(x.startswith("напоминани") for x in words)
    with _lock:
        items = sorted(_load(), key=_at)
    timers = [it for it in items if it["kind"] == "timer"]
    rems = [it for it in items if it["kind"] == "reminder"]
    if not want_r and timers:
        it = timers[0]
        return f"{_cap(it['text'])}: ещё {_left_text((_at(it) - now).total_seconds())}."
    if rems and (want_r or not timers):
        it = rems[0]
        return _with_text(f"До напоминания {_left_text((_at(it) - now).total_seconds())}",
                          it["text"])
    return "Напоминаний нет, сэр." if want_r else "Таймеров нет, сэр."


def _match(item, query, now):
    """Насколько напоминание похоже на «про маму», «на 5 минут», «на 18 30»."""
    if not query:
        return 0
    if item["kind"] == "timer":
        q = query[1:] if query[0] == "на" else query
        sec, j = _duration(q, 0)
        return 5 if sec and j == len(q) and item.get("dur") == sec else 0
    if query[0] in ("на", "в") and len(query) > 1:
        got = _clock(query, 1)
        if got and got[1] == len(query):
            at = _at(item)
            return 5 if (at.hour, at.minute) == got[0] else 0
    words = (item.get("text") or "").split()
    score = 0
    for q in query:
        if q in _QUERY_STOP or len(q) < 3:
            continue
        stem = q[:max(3, len(q) - 2)]
        if any(x.startswith(stem) or q.startswith(x[:max(3, len(x) - 2)])
               for x in words if len(x) >= 3):
            score += 1
    return score


def _cancel_reply(rest):
    words = rest.split()
    kinds = {_KEY_WORDS[x] for x in words if x in _KEY_WORDS}
    kinds |= {"timer" for x in words if x.startswith("таймер")}
    kinds |= {"reminder" for x in words if x.startswith("напоминани")}
    if not kinds:
        return None
    every = "все" in words or any(x in ("таймеры", "таймеров", "напоминания", "напоминаний")
                                  for x in words)
    query = [x for x in words if not x.startswith(("таймер", "напоминани"))
             and x not in ("все", "мои", "мое")]
    now = _now()
    with _lock:
        items = [it for it in _load() if it["kind"] in kinds]
        if not items:
            return {frozenset({"timer"}): "Таймеров нет, сэр.",
                    frozenset({"reminder"}): "Напоминаний нет, сэр."}.get(
                        frozenset(kinds), "Отменять нечего, сэр.")
        meaningful = [q for q in query if q not in _QUERY_STOP]
        if every and not meaningful:
            _remove(items)
            if len(items) == 1:
                return _cancelled(items[0])
            n_t = sum(it["kind"] == "timer" for it in items)
            n_r = len(items) - n_t
            parts = []
            if n_t:
                parts.append(f"{n_t} {_plural(n_t, 'таймер', 'таймера', 'таймеров')}")
            if n_r:
                parts.append(f"{n_r} {_plural(n_r, 'напоминание', 'напоминания', 'напоминаний')}")
            return f"Отменил {' и '.join(parts)}."
        if meaningful:
            scored = sorted(((_match(it, query, now), it) for it in items),
                            key=lambda p: (-p[0], _at(p[1])))
            if scored[0][0] <= 0:
                return ("Не нашёл такого таймера, сэр." if kinds == {"timer"}
                        else "Не нашёл такого напоминания, сэр.")
            target = scored[0][1]
        else:
            # «отмени таймер» — последний поставленный
            target = max(items, key=lambda it: (it.get("created", ""), it["at"]))
        _remove([target])
    return _cancelled(target)


def _cancelled(item):
    if item["kind"] == "timer":
        return f"Отменил {item['text']}."
    return _with_text("Отменил напоминание", item.get("text"))


def _timer_reply(rest, strict):
    """rest — слова после «таймер»/«засеки». strict — «засеки …»: не поняли — не наше."""
    if rest[:1] == ["на"]:
        rest = rest[1:]
    if not rest:
        return None if strict else "На сколько поставить таймер, сэр?"
    sec, j = _duration(rest, 0)
    if not sec or j != len(rest):
        num, k = number_at(rest, 0)
        if num and k == len(rest) and not strict:       # «таймер на 5» — минуты
            sec = int(round(num * 60))
        else:
            return None if strict else "Не понял, на сколько поставить таймер, сэр."
    text = f"таймер на {_dur_text(sec)}"
    _add("timer", _now() + datetime.timedelta(seconds=sec), text, dur=sec)
    return f"{_cap(text)}."


# с этих слов может начинаться время в конце фразы: «позвонить маме через час»
_WHEN_START = {"через", "в", "во", "на", "завтра", "послезавтра", "сегодня"}


def _remind_reply(body):
    now = _now()
    got = _when(body, 0, now)
    if got:
        at, rel, j = got
        text_words = body[j:]
    else:
        for k in range(1, len(body)):
            if body[k] in _WHEN_START:
                got = _when(body, k, now)
                if got and got[2] == len(body):
                    at, rel, _ = got
                    text_words = body[:k]
                    break
        else:
            return None          # «напомни, как зовут …» — это вопрос, не напоминание
    text = _clean_text(text_words)
    _add("reminder", at, text)
    when = f"через {_dur_text(rel)}" if rel is not None else _clock_text(at, now)
    return _with_text(f"Напомню {when}", text)


def handle(t, cfg=None):
    """Таймеры и напоминания. Вернуть фразу вслух или None — не наше."""
    t = _norm(t or "")
    if not t:
        return None
    words = t.split()

    m = _CANCEL.match(t)
    if m:
        reply = _cancel_reply(m.group(1))
        if reply:
            return reply

    keys = {_KEY_WORDS[x] for x in words if x in _KEY_WORDS}
    if keys and all(x in _LIST_WORDS or x in _KEY_WORDS for x in words) \
            and (len(words) > 1 or words[0] in ("напоминания", "таймеры")):
        return _list_reply(keys)

    if "сколько" in words and all(x in _LEFT_WORDS for x in words) \
            and ({"осталось", "остается", "ждать"} & set(words)
                 or any(x.startswith("таймер") for x in words)):
        return _left_reply(words)

    m = _TIMER.match(t)
    if m:
        if m.group(1) is not None or m.group(2) is None:
            return _timer_reply((m.group(1) or "").split(), strict=False)
        return _timer_reply(m.group(2).split(), strict=True)

    m = _REMIND.match(t)
    if m:
        return _remind_reply((m.group(1) or m.group(2) or m.group(3)).split())
    return None
