# -*- coding: utf-8 -*-
"""Самопроверка Джарвиса: разбор команд, поиск программ, голос, агент.

Всё, что реально запускает программы и открывает браузер, подменено заглушками —
тест ничего не открывает и никуда не лезет, кроме одного вопроса агенту.
"""

import json
import sys
import types
from pathlib import Path

HOME = Path(__file__).resolve().parent
sys.path.insert(0, str(HOME))

import skills
from jarvis import Jarvis

cfg = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
ok = fail = 0


def check(label, condition, detail=""):
    global ok, fail
    if condition:
        ok += 1
        print(f"  [ok]   {label} {detail}")
    else:
        fail += 1
        print(f"  [ПЛОХО] {label} {detail}")


# --- заглушки вместо реальных действий -------------------------------------
done = []
skills.launch = lambda target: done.append(("launch", str(target)))
skills.play_youtube = lambda q: done.append(("youtube", q)) or f"Включаю: {q}"
skills._press = lambda vk: done.append(("key", hex(vk)))
skills.media_skip = lambda d: done.append(("skip", d)) or "Следующая"
skills.youtube_volume = lambda percent=None, delta=None: done.append(("ytvol", percent)) or "Громкость ютуба 10"
skills.set_volume = lambda n: done.append(("volume", n)) or int(n)
skills.nudge_volume = lambda d: done.append(("nudge", d)) or 50
skills.screenshot = lambda: done.append(("screenshot",)) or Path("test.png")


print("\n1. Индекс программ из меню «Пуск»")
programs = skills.index_programs()
check("найдено ярлыков", len(programs) > 5, f"({len(programs)})")
for spoken in ("хром", "блокнот", "ворд", "телеграм", "стим"):
    path, score = skills.find_program(spoken)
    hit = path.stem if path is not None and score >= 0.62 else "— не найдено"
    print(f"         «{spoken}» -> {hit}  (близость {score:.2f})")


print("\n2. Числа словами")
for text, want in (("громкость тридцать", 30), ("громкость 15", 15),
                   ("громкость сорок пять", 45), ("громкость сто", 100)):
    check(text, skills.parse_number(text) == want, f"-> {skills.parse_number(text)}")


print("\n3. Разбор команд")
cases = [
    ("запусти блокнот", "launch"),
    ("открой хром", "launch"),
    ("включи на ютубе цой группа крови", "youtube"),
    ("поставь песню император на ютубе", "youtube"),
    ("громкость тридцать", "volume"),
    ("сделай тише", "nudge"),
    ("пауза", "key"),
    ("следующий трек", "skip"),
    ("следующая песня", "skip"),
    ("включи следующую песню на ютубе", "skip"),
    ("переключи песню", "skip"),
    ("переключи на следующую", "skip"),
    ("давай другую", "skip"),
    ("предыдущая песня", "skip"),
    ("громкость ютуба на десять", "ytvol"),
    ("на ютубе музыку потише", "ytvol"),
    ("который час", None),
    ("какое сегодня число", None),
    ("выключи компьютер", "ask"),
]
for phrase, expect in cases:
    done.clear()
    res = skills.handle(phrase, cfg)
    if expect == "ask":
        check(phrase, res is not None and res.ask, f"-> спросит: {res.ask if res else ''}")
    elif expect is None:
        check(phrase, res is not None and res.say, f"-> «{res.say if res else ''}»")
    else:
        check(phrase, res is not None and done and done[0][0] == expect,
              f"-> {done[0] if done else 'ничего'}")

done.clear()
check("незнакомая фраза уходит агенту", skills.handle("кто написал войну и мир", cfg) is None)


print("\n4. Обращение по имени")
fake = types.SimpleNamespace(cfg=cfg)
fake.is_wake = lambda w: Jarvis.is_wake(fake, w)
for phrase, want_text, want_woke in (
    ("джарвис запусти хром", "запусти хром", True),
    ("джарвис", "", True),
    ("жарвис включи музыку", "включи музыку", True),   # типичная ослышка
    ("эй джарвис который час", "который час", True),
    ("переключи песню джарис", "переключи песню", True),   # имя в конце
    ("да я вчера говорил ему", "да я вчера говорил ему", False),
):
    text, woke = Jarvis.split_wake(fake, phrase)
    check(f"«{phrase}»", (text, woke) == (want_text, want_woke), f"-> ({text!r}, {woke})")


print("\n5. Голос")
try:
    from speech import Voice

    v = Voice(prefer=cfg["voice"]["name"], rate=cfg["voice"]["rate"],
              pitch=cfg["voice"].get("pitch", 1.0),
              engine=cfg["voice"].get("engine", "auto"))
    v.say("Проверка связи. Джарвис готов к работе, сэр.")
    check("озвучка", v.wait_done(timeout=30))
except Exception as exc:
    check("озвучка", False, str(exc))


print("\n6. Агент (только чтение)")
try:
    import brain

    answer = brain.ask("Скажи одним предложением, что ты готов к работе.", cfg)
    check("ответ агента", bool(answer) and "ошибк" not in answer.lower(), f"-> {answer[:120]}")
except Exception as exc:
    check("ответ агента", False, str(exc))


print(f"\nИтого: успешно {ok}, провалено {fail}")
sys.exit(1 if fail else 0)
