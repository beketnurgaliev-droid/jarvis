# -*- coding: utf-8 -*-
"""Память о пользователе: что Джарвис о нём знает — имя, привычки, кто есть
кто, как он что называет. ИИ видит это при каждом ответе и подстраивается.

Пополняется голосом («запомни, что…»), самим ИИ — строкой «ЗАПОМНИТЬ: …» в
конце ответа, если это включено, — и в панели. Лежит в memory.json рядом со
скриптом: это личное, в архив для друга не попадает.
"""

import datetime
import json
import logging
import os
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger("jarvis.memory")

FILE = Path(__file__).resolve().parent / "memory.json"
MARK = "ЗАПОМНИТЬ:"
LIMIT = 200                 # больше в подсказку ИИ не влезет без толку

# что говорим ИИ, если ему разрешено запоминать самому
AUTO_RULE = (
    "Если собеседник сообщил о себе что-то устойчивое и полезное на будущее — имя, "
    "кто есть кто среди его людей, предпочтения, привычки, как он что называет, — "
    f"добавь в самом конце ответа отдельной строкой: {MARK} <факт коротко, в третьем "
    "лице>. Не запоминай мимолётное, пароли и то, что уже есть в памяти."
)

_lock = threading.Lock()
_facts = None


def _load():
    global _facts
    if _facts is None:
        try:
            _facts = json.loads(FILE.read_text(encoding="utf-8")).get("facts", [])
        except (OSError, ValueError):
            _facts = []
    return _facts


def _save():
    tmp = FILE.with_name("memory.json.tmp")
    tmp.write_text(json.dumps({"facts": _facts}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, FILE)


def _key(text):
    return re.sub(r"[^a-zа-я0-9]+", " ", text.lower().replace("ё", "е")).strip()


def facts():
    with _lock:
        return [dict(f) for f in _load()]


def add(text, source="user"):
    """Запомнить. Вернуть запись или None, если такое уже есть или пусто."""
    text = re.sub(r"\s+", " ", str(text)).strip(" .;")
    if not text:
        return None
    with _lock:
        data = _load()
        key = _key(text)
        for f in data:
            if SequenceMatcher(None, key, _key(f["text"])).ratio() > 0.85:
                return None
        n = max((f["id"] for f in data), default=0) + 1
        fact = {"id": n, "text": text[:300], "source": source,
                "added": datetime.date.today().isoformat()}
        data.append(fact)
        del data[:-LIMIT]
        _save()
    log.info("Запомнил (%s): %s", source, text)
    return fact


def update(fid, text):
    with _lock:
        for f in _load():
            if f["id"] == fid:
                f["text"] = re.sub(r"\s+", " ", str(text)).strip()[:300]
                _save()
                return True
    return False


def remove(fid):
    with _lock:
        data = _load()
        before = len(data)
        data[:] = [f for f in data if f["id"] != fid]
        if len(data) != before:
            _save()
            return True
    return False


def forget(phrase):
    """Забыть то, что больше всего похоже на сказанное. Вернуть забытое или None."""
    q = _key(phrase)
    if not q:
        return None
    with _lock:
        best, score = None, 0.0
        for f in _load():
            k = _key(f["text"])
            r = SequenceMatcher(None, q, k).ratio()
            if len(q) >= 5 and q in k:          # «все» не должно найтись во «всегда»
                r = max(r, 0.9)
            elif set(q.split()) & set(k.split()):
                r = max(r, 0.4 + 0.4 * len(set(q.split()) & set(k.split())) / len(q.split()))
            if r > score:
                best, score = f, r
        if best is None or score < 0.55:
            return None
        _load().remove(best)
        _save()
    log.info("Забыл: %s", best["text"])
    return best


def prompt():
    """Кусок подсказки для ИИ: что известно о собеседнике."""
    data = facts()
    if not data:
        return ""
    return ("Что ты знаешь о собеседнике (учитывай, когда к месту; не пересказывай без "
            "нужды):\n" + "\n".join(f"- {f['text']}" for f in data))


def extract(text):
    """Вынуть из ответа ИИ строки «ЗАПОМНИТЬ: …». Вернуть (ответ без них, факты)."""
    found = re.findall(rf"{MARK}\s*(.+)", text, flags=re.I)
    clean = re.sub(rf"\s*{MARK}\s*.+", "", text, flags=re.I).strip()
    return clean, [f.strip() for f in found if f.strip()]
