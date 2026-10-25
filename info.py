# -*- coding: utf-8 -*-
"""Справки вслух: погода и курсы валют.

Погода — Open-Meteo, курсы — Национальный банк Казахстана; ключи не нужны.
handle(t, cfg) получает уже нормализованную фразу (как skills._norm) и
возвращает, что сказать, или None, если фраза не про погоду и не про курс.
"""

import datetime
import json
import logging
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("jarvis.info")

_HEADERS = {"User-Agent": "Jarvis-voice-assistant/1.0"}
_TIMEOUT = 6


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


def _fetch(url):
    """GET. Open-Meteo иногда отвечает 502/503 «перегружен» — один повтор."""
    for attempt in (0, 1):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt:
                raise
            time.sleep(0.7)


# --------------------------------------------------------------------------
# Числа словами: «две тысячи пятьсот», «полторы тысячи», «восемнадцать
# тридцать» (это два числа). Общая часть — ей пользуется и reminders.py
# --------------------------------------------------------------------------

_N_ONES = {
    "ноль": 0, "нуль": 0, "один": 1, "одна": 1, "одну": 1, "одно": 1, "одного": 1,
    "одной": 1, "два": 2, "две": 2, "двух": 2, "три": 3, "трех": 3, "четыре": 4,
    "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6, "шести": 6, "семь": 7,
    "семи": 7, "восемь": 8, "восьми": 8, "девять": 9, "девяти": 9,
}
_N_TEENS = {
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19, "десяти": 10, "одиннадцати": 11,
    "двенадцати": 12, "тринадцати": 13, "четырнадцати": 14, "пятнадцати": 15,
    "шестнадцати": 16, "семнадцати": 17, "восемнадцати": 18, "девятнадцати": 19,
}
_N_TENS = {
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
    "двадцати": 20, "тридцати": 30, "сорока": 40, "пятидесяти": 50,
    "шестидесяти": 60, "семидесяти": 70, "восьмидесяти": 80, "девяноста": 90,
}
_N_HUNDREDS = {
    "сто": 100, "ста": 100, "двести": 200, "триста": 300, "четыреста": 400,
    "пятьсот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800,
    "девятьсот": 900,
}
_N_MULT = {
    "тысяча": 1000, "тысячи": 1000, "тысяч": 1000, "тысячу": 1000, "тыща": 1000,
    "тыщи": 1000, "тыщ": 1000, "тыщу": 1000, "миллион": 10 ** 6,
    "миллиона": 10 ** 6, "миллионов": 10 ** 6,
}
_N_HALF = {"полтора": 1.5, "полторы": 1.5}


def number_at(words, i):
    """Одно число, которое начинается со слова i. Вернуть (число, где кончилось)
    или (None, i). Соседние «восемнадцать тридцать» — это два числа, 18 и 30."""
    total, cur, last, j = 0, 0, None, i
    while j < len(words):
        w = words[j]
        if w.isdigit():
            if last is not None:
                break
            cur, last, j = int(w), "d", j + 1
            # «1 500» — разряды, разбитые пробелом
            while j < len(words) and len(words[j]) == 3 and words[j].isdigit() \
                    and cur < 1000:
                cur, j = cur * 1000 + int(words[j]), j + 1
            continue
        if w in _N_HUNDREDS and last in (None, "m"):
            cur, last = cur + _N_HUNDREDS[w], "h"
        elif (w in _N_TENS or w in _N_TEENS) and last in (None, "m", "h"):
            cur, last = cur + _N_TENS.get(w, _N_TEENS.get(w)), "t"
        elif w in _N_ONES and last in (None, "m", "h", "t") \
                and not (_N_ONES[w] == 0 and last is not None):
            cur, last = cur + _N_ONES[w], ("z" if _N_ONES[w] == 0 else "o")
            if last == "z":          # «ноль» — само по себе, без продолжения
                j += 1
                break
        elif w in _N_HALF and last is None:
            cur, last = _N_HALF[w], "half"
        elif w in _N_MULT and last != "m":
            total, cur, last = total + (cur or 1) * _N_MULT[w], 0, "m"
        else:
            break
        if last == "half" and not (j + 1 < len(words) and words[j + 1] in _N_MULT):
            j += 1
            break
        j += 1
    if j == i:
        return None, i
    value = total + cur
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value, j


# --------------------------------------------------------------------------
# Погода
# --------------------------------------------------------------------------

_GEO_URL = ("https://geocoding-api.open-meteo.com/v1/search?name={}&count=5"
            "&language=ru&format=json")
_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation"
    "&daily=temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max"
    "&timezone=auto&forecast_days=3&wind_speed_unit=ms")
_FORECAST_TTL = 600          # прогноз — 10 минут
_FORECAST_STALE = 3 * 3600   # нет связи — сойдёт и такой

_WMO = {
    0: "ясно", 1: "в основном ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "туман", 51: "слабая морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "ледяная морось", 61: "небольшой дождь", 63: "дождь",
    65: "сильный дождь", 66: "ледяной дождь", 67: "ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "небольшой ливень", 81: "ливень", 82: "сильный ливень", 85: "снегопад",
    86: "сильный снегопад", 95: "гроза", 96: "гроза с градом", 99: "гроза с градом",
}
_WET = set(range(51, 100))   # всё, что с осадками

# частые города в предложном падеже — сразу, без перебора окончаний
_CITY_ALIASES = {
    "алмате": "Алматы", "алма ате": "Алматы", "алма ата": "Алматы",
    "астане": "Астана", "нур султане": "Астана", "нурсултане": "Астана",
    "шымкенте": "Шымкент", "караганде": "Караганда", "таразе": "Тараз",
    "павлодаре": "Павлодар", "усть каменогорске": "Усть-Каменогорск",
    "семее": "Семей", "костанае": "Костанай", "кызылорде": "Кызылорда",
    "уральске": "Уральск", "петропавловске": "Петропавловск",
    "туркестане": "Туркестан", "талдыкоргане": "Талдыкорган",
    "экибастузе": "Экибастуз", "москве": "Москва", "питере": "Санкт-Петербург",
    "петербурге": "Санкт-Петербург", "санкт петербурге": "Санкт-Петербург",
    "новосибирске": "Новосибирск", "екатеринбурге": "Екатеринбург",
    "казани": "Казань", "омске": "Омск", "ташкенте": "Ташкент",
    "бишкеке": "Бишкек", "минске": "Минск",
}

# после «в» — не город
_NOT_CITY = {
    "сегодня", "завтра", "послезавтра", "сейчас", "будет", "ли", "на", "улице",
    "утром", "днем", "вечером", "ночью", "выходные", "выходных", "течение",
    "ближайшие", "этот", "эти", "понедельник", "вторник", "среду", "четверг",
    "пятницу", "субботу", "воскресенье", "градусах", "цельсиях", "целом",
    "общем", "погода", "погоду", "погоды", "какая", "какой", "там", "городе",
    "город", "г", "тепло", "холодно", "жарко", "дождь", "снег", "зонт", "нужен",
    "сколько", "градусов", "прогноз", "температура", "как", "обед", "течении",
    "полдень", "час", "часы", "минуты", "дорогу",
}
# «сколько градусов в комнате» — это не к синоптикам
_INDOOR = {"комнате", "доме", "квартире", "помещении", "офисе", "машине", "холодильнике"}

_WEEKDAYS_ACC = {"понедельник": 0, "вторник": 1, "среду": 2, "четверг": 3,
                 "пятницу": 4, "субботу": 5, "воскресенье": 6}
_WEEKDAYS_IN = ["в понедельник", "во вторник", "в среду", "в четверг",
                "в пятницу", "в субботу", "в воскресенье"]

_W_REJECT = re.compile(
    r"^(?:включи|поставь|запусти|открой|найди|напиши|напомни|сыграй|отправь|"
    r"закрой|почему|зачем|отчего|кто)\b|\b(?:вчера|была|было|были)\b")
# «прогноз» сам по себе бывает и про матч, и про курс — берём только короткое
_W_MAIN = re.compile(r"\bпогод\w*|^(?:а\s+)?(?:какой\s+)?прогноз(?:\s+(?:на\s+)?"
                     r"(?:сегодня|завтра|послезавтра|выходные))?$")
# «сколько градусов в водке», «какая температура воды» — не к синоптикам
_W_WHEN = (r"(?:\s+(?:на улице|за окном|сейчас|сегодня|завтра|послезавтра|там|тепла|"
           r"мороза|воздуха|в\s+\w+(?:\s+\w+)?|во\s+\w+))*")
_W_TEMP = re.compile(
    rf"^(?:а\s+)?сколько\s+(?:(?:сейчас|сегодня|завтра|там|на улице|за окном)\s+)*"
    rf"градус\w*{_W_WHEN}$|"
    rf"^(?:а\s+)?какая\s+(?:(?:сейчас|сегодня|завтра|там)\s+)*температура{_W_WHEN}$|"
    rf"^(?:а\s+)?температура\s+(?:на улице|за окном|воздуха){_W_WHEN}$")
_HOTCOLD = r"(?:холодно|тепло|жарко|прохладно|морозно|холод|жара)"
_W_FEEL = re.compile(
    rf"\b{_HOTCOLD}\s+ли\b|^(?:сегодня\s+|завтра\s+)?(?:будет\s+)?{_HOTCOLD}\s+"
    rf"(?:на улице|за окном|сегодня|завтра)|\b(?:на улице|за окном)\s+(?:сейчас\s+)?"
    rf"{_HOTCOLD}$|^(?:а\s+)?(?:как|что)\s+(?:там\s+)?(?:на улице|за окном)$")
_W_RAIN = re.compile(
    r"\b(?:будет|пойдет|ожидается|обещают|идет|собирается)\s+(?:ли\s+)?(?:\w+\s+)?"
    r"(?:\w+\s+)?(?:дожд\w*|снег\w*|гроз\w*|ливень|осадки)\b|"
    r"^(?:а\s+)?(?:дожд\w*|снег|ливень|гроза|осадки)\s+(?:\w+\s+)?(?:будет|пойдет|ожидается)\b")
_W_UMBRELLA = re.compile(r"\b(?:нужен|нужно|надо|брать|взять|понадобится)\b.*\bзонт\w*|"
                         r"^(?:а\s+)?зонт\w*\s+(?:нужен|брать|взять)")

_geo_cache = {}
_forecast_cache = {}


def _key(name):
    return re.sub(r"\s+", " ", name.lower().replace("ё", "е").replace("-", " ")).strip()


def _geocode(name):
    """Город по названию в именительном падеже → {"name", "lat", "lon"} или None."""
    key = _key(name)
    if key in _geo_cache:
        return _geo_cache[key]
    data = json.loads(_fetch(_GEO_URL.format(urllib.parse.quote(name))))
    best, best_score = None, -1
    for r in data.get("results") or []:
        # поиск ищет по началу слова: на «москве» отдаёт «Москверо» — не то
        if _key(r.get("name", "")) != key:
            continue
        if not str(r.get("feature_code") or "PPL").startswith("PPL"):
            continue
        score = (r.get("population") or 0) * (3 if r.get("country_code") in ("KZ", "RU") else 1)
        if r.get("feature_code") in ("PPLC", "PPLA"):
            score += 1
        if score > best_score:
            best, best_score = r, score
    place = None
    if best is not None:
        place = {"name": best["name"], "lat": best["latitude"], "lon": best["longitude"],
                 "score": best_score,
                 # город, а не деревня-тёзка: «тюмени» — тоже чья-то деревня
                 "big": (best.get("population") or 0) >= 10000
                 or best.get("feature_code") in ("PPLC", "PPLA", "PPLA2")}
    _geo_cache[key] = place
    return place


def _city_candidates(words):
    """«астане» → Астана, «шымкенте» → Шымкент, «усть каменогорске» →
    Усть-Каменогорск: падеж снимаем перебором окончаний."""
    out = []
    phrase = " ".join(words)
    if phrase in _CITY_ALIASES:
        out.append(_CITY_ALIASES[phrase])
    head, last = words[:-1], words[-1]
    forms = [last]
    if last.endswith("е"):
        stem = last[:-1]
        forms += [stem + "а", stem, stem + "ь", stem + "я", stem + "й"]
    elif last.endswith("и"):
        stem = last[:-1]
        forms += [stem + "ь", stem + "а", stem + "я"]
    for f in forms:
        out.append("-".join(head + [f]))
    seen, uniq = set(), []
    for c in out:
        if _key(c) not in seen and len(_key(c)) >= 2:
            seen.add(_key(c))
            uniq.append(c)
    return uniq


def _locative(name):
    """Алматы → Алматы, Астана → Астане, Шымкент → Шымкенте, Казань → Казани."""
    m = re.match(r"^(.*?)([А-Яа-яЁё]+)$", name)
    if not m:
        return name
    head, last = m.groups()
    low = last.lower()
    if low.endswith("ия"):
        last = last[:-1] + "и"
    elif low.endswith(("а", "я", "й")):
        last = last[:-1] + "е"
    elif low.endswith("ь"):
        last = last[:-1] + "и"
    elif low[-1] not in "аеёиоуыэюя":
        last = last + "е"
    return head + last


def _city_words(t):
    """Город из фразы: слова после «в»/«во» или сразу после «погода».
    Вернуть список слов, [] — город не назван, None — это про комнату."""
    words = t.split()
    for i, w in enumerate(words):
        if w in ("в", "во") or (w.startswith("погод") and i + 1 < len(words)
                                and words[i + 1] not in ("в", "во")):
            got = []
            for x in words[i + 1:i + 4]:
                if x in _NOT_CITY or x.isdigit() or x in _N_ONES:
                    break
                got.append(x)
            if got and got[0] in _INDOOR:
                return None
            if got:
                return got
    return []


def _day(t, today):
    """0 — сегодня, 1 — завтра, 2 — послезавтра; день недели — сколько до него."""
    if re.search(r"\bпослезавтра\b", t):
        return 2, "послезавтра"
    if re.search(r"\bзавтра\b", t):
        return 1, "завтра"
    m = re.search(r"\b(?:в|во|на)\s+(понедельник|вторник|среду|четверг|пятницу|субботу|"
                  r"воскресенье|выходные|выходных)\b", t)
    if m:
        wd = _WEEKDAYS_ACC.get(m.group(1), 5)
        delta = (wd - today.weekday()) % 7
        label = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(delta, _WEEKDAYS_IN[wd])
        return delta, label
    return 0, "сегодня"


def _round(x):
    return int(math.floor(x + 0.5)) if x is not None else None


def _temp(n):
    """-5 → «минус 5»."""
    return f"минус {abs(n)}" if n < 0 else str(n)


def _deg(n):
    return f"{_temp(n)} {_plural(abs(n), 'градус', 'градуса', 'градусов')}"


def _deg_gen(n):
    """После «до»: до 21 градуса, до 11 градусов."""
    return f"{_temp(n)} {_plural(abs(n), 'градуса', 'градусов', 'градусов')}"


def _forecast(place):
    key = (round(place["lat"], 2), round(place["lon"], 2))
    hit = _forecast_cache.get(key)
    if hit and time.monotonic() - hit[0] < _FORECAST_TTL:
        return hit[1]
    try:
        data = json.loads(_fetch(_FORECAST_URL.format(lat=place["lat"], lon=place["lon"])))
        if "daily" not in data or "current" not in data:
            raise ValueError("нет данных в ответе Open-Meteo")
    except Exception:
        if hit and time.monotonic() - hit[0] < _FORECAST_STALE:
            log.warning("Open-Meteo недоступен — беру прогноз из памяти")
            return hit[1]
        raise
    _forecast_cache[key] = (time.monotonic(), data)
    return data


def _daily(data, day, field):
    vals = data["daily"].get(field) or []
    return vals[day] if day < len(vals) else None


def _weather_kind(t):
    if _W_REJECT.search(t):
        return None
    if _W_UMBRELLA.search(t):
        return "umbrella"
    if _W_RAIN.search(t):
        return "rain"
    if _W_MAIN.search(t):
        return "full"
    if _W_TEMP.search(t):
        return "temp"
    if _W_FEEL.search(t):
        return "full"
    return None


def _say_now(data, loc, short=False):
    cur = data["current"]
    temp = _round(cur["temperature_2m"])
    feels = _round(cur.get("apparent_temperature"))
    parts = [f"В {loc} сейчас {_deg(temp)}"]
    if feels is not None and abs(feels - temp) >= 3:
        parts.append(f"ощущается как {_temp(feels)}")
    cond = _WMO.get(cur.get("weather_code"))
    if cond:
        parts.append(cond)
    if short:
        return ", ".join(parts) + "."
    wind = _round(cur.get("wind_speed_10m"))
    if wind is not None:
        parts.append(f"ветер {wind} {_plural(wind, 'метр', 'метра', 'метров')} в секунду"
                     if wind else "без ветра")
    out = ", ".join(parts) + "."
    hi = _round(_daily(data, 0, "temperature_2m_max"))
    night = _round(_daily(data, 1, "temperature_2m_min"))   # ночной минимум — утро завтра
    hour = int(cur.get("time", "T12")[11:13] or 12)
    tail = []
    if hi is not None and hour < 16:
        tail.append(f"днём до {_temp(hi)}")
    if night is not None:
        tail.append(f"ночью до {_temp(night)}")
    if tail:
        out += " " + _cap(", ".join(tail)) + "."
    return out


def _say_day(data, day, label, loc):
    lo = _round(_daily(data, day, "temperature_2m_min"))
    hi = _round(_daily(data, day, "temperature_2m_max"))
    if lo == hi:
        text = f"{_cap(label)} в {loc} около {_deg_gen(hi)}"
    else:
        text = f"{_cap(label)} в {loc} от {_temp(lo)} до {_deg_gen(hi)}"
    cond = _WMO.get(_daily(data, day, "weather_code"))
    if cond:
        text += f", {cond}"
    p = _daily(data, day, "precipitation_probability_max")
    if p is not None:
        p = _round(p)
        text += (f", вероятность осадков {p} {_plural(p, 'процент', 'процента', 'процентов')}"
                 if p >= 10 else ", без осадков")
    return text + "."


def _say_rain(data, day, label, loc, umbrella):
    code = _daily(data, day, "weather_code")
    cond = _WMO.get(code, "")
    p = _round(_daily(data, day, "precipitation_probability_max"))
    pct = f"{p} {_plural(p, 'процент', 'процента', 'процентов')}" if p is not None else ""
    now_code = data["current"].get("weather_code")
    if day == 0 and now_code in _WET:
        if umbrella:
            return f"Да, зонт нужен: в {loc} сейчас {_WMO[now_code]}."
        return f"Да, в {loc} сейчас {_WMO[now_code]}."
    likely = (p or 0) >= 50 or (code in _WET and (p is None or p >= 30))
    if likely:
        what = cond if code in _WET else "осадки"
        if umbrella:
            return f"Да, зонт лучше взять: {label} в {loc} {what}" + \
                (f", вероятность {pct}." if pct else ".")
        return f"Да, {label} в {loc} {what}" + (f", вероятность {pct}." if pct else ".")
    if (p or 0) >= 20:
        if umbrella:
            return f"На всякий случай возьмите: {label} в {loc} вероятность осадков {pct}."
        return f"Возможно: {label} в {loc} вероятность осадков {pct}."
    if umbrella:
        return f"Нет, зонт не понадобится: {label} в {loc} {cond or 'без осадков'}."
    return f"Нет, {label} в {loc} осадков не обещают" + (f", {cond}." if cond else ".")


def weather(t, cfg=None):
    """Ответ про погоду или None, если фраза не про неё."""
    kind = _weather_kind(t)
    if kind is None:
        return None
    city = _city_words(t)
    if city is None:
        return None
    day, label = _day(t, datetime.date.today())
    if day > 2:
        return "Прогноз у меня только на 3 дня, сэр: сегодня, завтра и послезавтра."
    try:
        if city:
            place = None
            for cand in _city_candidates(city):
                found = _geocode(cand)
                if found and (place is None or found["score"] > place["score"]):
                    place = found
                if place and place["big"]:
                    break
            if place is None:
                # «погода в …» — точно про город; «сколько градусов в водке» — нет
                if not _W_MAIN.search(t):
                    return None
                return f"Не знаю город {' '.join(city).title()}, сэр."
        else:
            default = (cfg or {}).get("city") or "Алматы"
            place = _geocode(default)
            if place is None:
                return f"Не нашёл город {default}, сэр. Проверьте настройку city."
        data = _forecast(place)
        loc = _locative(place["name"])
        if kind in ("rain", "umbrella"):
            return _say_rain(data, day, label, loc, kind == "umbrella")
        if day:
            return _say_day(data, day, label, loc)
        return _say_now(data, loc, short=(kind == "temp"))
    except Exception as e:
        log.warning("Погода недоступна: %r", e)
        return "Не могу узнать погоду, сэр, нет связи с сервером."


# --------------------------------------------------------------------------
# Курсы валют — Нацбанк Казахстана
# --------------------------------------------------------------------------

_NB_URL = "https://nationalbank.kz/rss/get_rates.cfm?fdate={}"
_RATES_TTL = 3600
_RATES_STALE = 24 * 3600

# код → как узнать в речи
_CUR_WORDS = [
    ("USD", r"доллар\w*|бакс\w*"),
    ("EUR", r"евро"),
    ("RUB", r"рубл\w*|рубь"),
    ("CNY", r"юан\w*"),
    ("GBP", r"фунт\w*"),
    ("KGS", r"сом|сома|сомов|сому"),
    ("UZS", r"сум|сума|сумов|суму"),
    ("TRY", r"лир[аыу]?"),
    ("AED", r"дирхам\w*"),
    ("JPY", r"[ий]ен[аыу]?"),
    ("CHF", r"франк\w*"),
    ("KZT", r"тенге|тенги|тэнге"),
]
_CUR_RE = [(code, re.compile(rf"^(?:{rx})$")) for code, rx in _CUR_WORDS]

# код → (1, 2, 5) для «1 доллар, 2 доллара, 5 долларов»
_CUR_FORMS = {
    "USD": ("доллар", "доллара", "долларов"), "EUR": ("евро", "евро", "евро"),
    "RUB": ("рубль", "рубля", "рублей"), "CNY": ("юань", "юаня", "юаней"),
    "GBP": ("фунт", "фунта", "фунтов"), "KGS": ("сом", "сома", "сомов"),
    "UZS": ("сум", "сума", "сумов"), "TRY": ("лира", "лиры", "лир"),
    "AED": ("дирхам", "дирхама", "дирхамов"), "JPY": ("иена", "иены", "иен"),
    "CHF": ("франк", "франка", "франков"), "KZT": ("тенге", "тенге", "тенге"),
}
_SUBUNIT = {
    "KZT": ("тиын", "тиын", "тиын"), "USD": ("цент", "цента", "центов"),
    "EUR": ("цент", "цента", "центов"), "RUB": ("копейка", "копейки", "копеек"),
    "GBP": ("пенс", "пенса", "пенсов"), "CNY": ("фэнь", "фэнь", "фэнь"),
}

# какие ещё слова могут быть во фразе про курс — всё прочее значит «не наше»
_CUR_ALLOWED = {
    "а", "ну", "какой", "какая", "какие", "каков", "курс", "курса", "курсы", "курсе",
    "курсу", "валют", "валюты", "валюта", "сегодня", "сейчас", "завтра", "на", "в",
    "во", "к", "по", "сколько", "скок", "стоит", "стоят", "будет", "это", "переведи",
    "перевести", "посчитай", "конвертируй", "почем", "скажи", "мне", "нацбанка",
    "нацбанку", "нацбанк", "официальный", "обменный", "за", "получится", "выйдет",
    "нынче", "у", "нас", "там", "джарвис", "пожалуйста", "текущий", "ли",
}
_CUR_ASK = {"сколько", "скок", "стоит", "стоят", "почем", "переведи", "перевести",
            "посчитай", "конвертируй"}

_rates_cache = {}


def _cur_code(word):
    for code, rx in _CUR_RE:
        if rx.match(word):
            return code
    return None


def _parse_rates(raw):
    """XML Нацбанка → {"USD": тенге за 1 доллар, ...}."""
    root = ET.fromstring(raw)
    rates = {}
    for item in root.iter("item"):
        code = (item.findtext("title") or "").strip().upper()
        try:
            value = float((item.findtext("description") or "").replace(",", "."))
            quant = float(item.findtext("quant") or 1) or 1
        except ValueError:
            continue
        if code and value > 0:
            rates[code] = value / quant
    return rates


def _rates_for(day):
    key = day.strftime("%d.%m.%Y")
    hit = _rates_cache.get(key)
    if hit and time.monotonic() - hit[0] < _RATES_TTL:
        return hit[1]
    try:
        rates = _parse_rates(_fetch(_NB_URL.format(key)))
    except Exception:
        if hit and time.monotonic() - hit[0] < _RATES_STALE:
            return hit[1]
        raise
    _rates_cache[key] = (time.monotonic(), rates)
    return rates


def _rates(tomorrow=False):
    """Курсы на сегодня (нет — на ближайший прошлый день) или на завтра."""
    today = datetime.date.today()
    if tomorrow:
        return _rates_for(today + datetime.timedelta(days=1))
    for back in range(4):
        rates = _rates_for(today - datetime.timedelta(days=back))
        if rates:
            return rates
    return {}


def _money(x, code):
    """478.5 KZT → «478 тенге 50 тиын»; крупные суммы — без копеек."""
    one, few, many = _CUR_FORMS[code]
    sub = _SUBUNIT.get(code)
    if x >= 1000 or sub is None:
        n = int(math.floor(x + 0.5))
        if n == 0 and x > 0:
            return f"меньше 1 {few}"
        return f"{n} {_plural(n, one, few, many)}"
    cents = int(math.floor(x * 100 + 0.5))
    n, c = divmod(cents, 100)
    parts = []
    if n or not c:
        parts.append(f"{n} {_plural(n, one, few, many)}")
    if c:
        parts.append(f"{c} {_plural(c, *sub)}")
    return " ".join(parts)


def currency(t, cfg=None):
    """Ответ про курс или пересчёт валют, или None."""
    words = t.split()
    mentions, amount = [], None     # mentions: (код, позиция)
    i = 0
    while i < len(words):
        w = words[i]
        code = _cur_code(w)
        if code:
            mentions.append((code, i))
            i += 1
            continue
        n, j = number_at(words, i)
        if n is not None:
            if amount is not None:
                return None          # два числа — не понимаю, что считать
            amount, i = n, j
            continue
        if w not in _CUR_ALLOWED:
            return None
        i += 1
    # во что переводим: валюта после «в», «на», «к»; не сказали — в тенге
    target = None
    for code, pos in mentions:
        if pos > 0 and words[pos - 1] in ("в", "во", "на", "к"):
            target = (code, pos)

    has_kurs = any(w.startswith("курс") for w in words)
    foreign = [m for m in mentions if m[0] != "KZT"]
    asks = (any(w in _CUR_ASK for w in words) or any(m[0] == "KZT" for m in mentions)
            or (amount is not None and target is not None))   # «сто долларов в рублях»
    if not (has_kurs or (foreign and asks)):
        return None
    if amount is not None and not mentions:
        return None
    tgt = target[0] if target else "KZT"
    source = next((m for m in mentions if m != target and m[0] != tgt), None)
    src = source[0] if source else None

    try:
        rates = _rates(tomorrow="завтра" in words)
    except Exception as e:
        log.warning("Курсы Нацбанка недоступны: %r", e)
        return "Не могу узнать курс, сэр, нет связи с Нацбанком."
    if not rates:
        if "завтра" in words:
            return "Курс на завтра Нацбанк ещё не объявил, сэр."
        return "Нацбанк не отдал курсы, сэр."
    rates = dict(rates, KZT=1.0)

    def rate(code):
        return rates.get(code)

    if src is None:
        if mentions or amount is not None:
            return None
        # «какой курс», «курсы валют» — главное
        parts = [f"{_CUR_FORMS[c][0]} — {_money(rate(c), 'KZT')}"
                 for c in ("USD", "EUR", "RUB") if rate(c)]
        return _cap(", ".join(parts) + ".")
    if src == tgt:
        return None
    if rate(src) is None or rate(tgt) is None:
        missing = src if rate(src) is None else tgt
        return f"Нацбанк не даёт курс {_CUR_FORMS[missing][1]}, сэр."

    if amount is None:
        if src == "KZT":                 # «курс тенге к доллару» — назовём доллар
            src, tgt = tgt, "KZT"
        value = rate(src) / rate(tgt)
        return f"{_cap(_CUR_FORMS[src][0])} — {_money(value, tgt)}."
    value = amount * rate(src) / rate(tgt)
    return f"{_money(amount, src)} — {_money(value, tgt)}."


def handle(t, cfg=None):
    """Погода или курс. Не наше — None."""
    t = _norm(t or "")
    if not t:
        return None
    return weather(t, cfg) or currency(t, cfg)
