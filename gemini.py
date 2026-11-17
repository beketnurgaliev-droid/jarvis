# -*- coding: utf-8 -*-
"""Gemini по ключу Google AI Studio — один из ИИ в ai.py.

Спрашиваем напрямую по API: ответ за секунду-две. Ключ бесплатный
(aistudio.google.com/apikey); подписка Gemini доступа к API не даёт.
На бесплатном ключе нет поиска в Google — такие вопросы Джарвис отдаёт тому
ИИ, который умеет искать.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("jarvis.gemini")

API = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.5-flash-lite"   # на бесплатном ключе — самая быстрая
BACKUP_MODELS = ["gemini-flash-lite-latest"]   # спрашиваем параллельно


def _post(url, key, body, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _thinking(value):
    """«minimal»/«low» — уровень (Gemini 3), число — бюджет токенов (2.5)."""
    if value in ("", None):
        return None
    if isinstance(value, int) or str(value).isdigit():
        return {"thinkingBudget": int(value)}
    return {"thinkingLevel": str(value)}


def ask(prompt, system, conf, key, image=None):
    """Короткий ответ. Вернуть (текст, None) или (None, причина).
    image — {"mime", "b64"} (ai.image): снимок экрана и т.п.

    Бесплатный Gemini то отвечает за секунду, то зависает на десять. Поэтому
    спрашиваем основную и запасную модели сразу и берём первый ответ."""
    names = [conf.get("model") or DEFAULT_MODEL]
    names += [m for m in conf.get("backup_models", BACKUP_MODELS) if m not in names]
    pool = ThreadPoolExecutor(max_workers=len(names), thread_name_prefix="gemini")
    futures = [pool.submit(_ask_one, name, key, prompt, system, conf, image) for name in names]
    errors = []
    try:
        for fut in as_completed(futures, timeout=conf.get("timeout", 20)):
            text, error = fut.result()
            if text:
                return text, None
            errors.append(error)
    except TimeoutError:
        errors.append("не уложился во время")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return None, errors[0] if errors else "нет ответа"


def _ask_one(model, key, prompt, system, conf, image=None):
    """Спросить одну модель. Вернуть (текст, None) или (None, причина)."""
    parts = [{"text": prompt}]
    if image:
        parts.append({"inlineData": {"mimeType": image["mime"], "data": image["b64"]}})
    base = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"maxOutputTokens": conf.get("max_tokens", 400)}}
    if system:
        base["systemInstruction"] = {"parts": [{"text": system}]}
    # что-то из этого модель может не поддерживать — тогда пробуем проще
    attempts = []
    thinking = _thinking(conf.get("thinking", ""))
    tools = [{"google_search": {}}] if conf.get("search") else None
    if thinking and tools:
        attempts.append({"tools": tools, "thinking": thinking})
    if tools:
        attempts.append({"tools": tools})
    if thinking:
        attempts.append({"thinking": thinking})
    attempts.append({})

    url = f"{API}/models/{model}:generateContent"
    started = time.monotonic()
    for extra in attempts:
        body = json.loads(json.dumps(base))
        if extra.get("tools"):
            body["tools"] = extra["tools"]
        if extra.get("thinking"):
            body["generationConfig"]["thinkingConfig"] = extra["thinking"]
        try:
            data = _post(url, key, body, conf.get("timeout", 20))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 400 and "API_KEY_INVALID" not in detail:
                log.warning("Gemini %s не принял запрос (%s) — пробую проще: %s",
                            model, ", ".join(extra) or "минимальный", detail)
                continue
            log.warning("Gemini %s: HTTP %s: %s", model, exc.code, detail)
            return None, {400: "ключ не подходит", 403: "доступ запрещён",
                          404: "такой модели нет", 429: "кончился лимит запросов",
                          503: "Gemini перегружен"}.get(exc.code, f"ошибка HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("Gemini %s недоступен: %s", model, exc)
            return None, "нет связи или не уложился во время"
        cand = (data.get("candidates") or [{}])[0]
        parts = cand.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        log.info("Gemini (%s) ответил за %.1f с", model, time.monotonic() - started)
        if not text:
            log.warning("Gemini %s вернул пустой ответ: %s", model, cand.get("finishReason"))
            return None, "пустой ответ"
        return text, None
    return None, "модель не приняла запрос"


def models(key):
    """Модели, которыми можно отвечать: без картинок, голоса и встраиваний."""
    req = urllib.request.Request(f"{API}/models?pageSize=1000",
                                 headers={"x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for m in data.get("models", []):
        name = m.get("name", "").removeprefix("models/")
        if ("generateContent" in m.get("supportedGenerationMethods", [])
                and name.startswith("gemini")
                and not any(x in name for x in ("image", "tts", "embedding", "audio", "live"))):
            out.append(name)
    return out
