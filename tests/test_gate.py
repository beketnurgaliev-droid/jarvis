# -*- coding: utf-8 -*-
"""Пропуск тишины (speech.Listener, skip_silence): не теряет ли фразы и сколько
экономит процессора. Без микрофона: фразы синтезатором Pavel, вокруг — шум
комнаты, часть фраз тихо, одна — поверх «музыки». Ничего не говорит вслух.

    venv\\Scripts\\python.exe tests\\test_gate.py [модель]
"""
import asyncio
import audioop
import io
import math
import os
import random
import struct
import sys
import time
import wave
from difflib import SequenceMatcher

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOME)
import speech  # noqa: E402

RATE, BLOCK = speech.SAMPLE_RATE, speech.BLOCK_FRAMES
PHRASES = [  # (фраза, громкость, фон)
    ("джарвис открой доту", 1.0, "quiet"),
    ("джарвис сверни все окна", 0.3, "quiet"),
    ("джарвис какая погода в алматы", 1.0, "fan"),
    ("джарвис громкость тридцать", 0.15, "quiet"),
    ("джарвис переключись на телеграм", 0.6, "music"),
    ("джарвис следующая песня", 0.3, "fan"),
    ("джарвис", 0.5, "quiet"),
    ("закрой хром", 0.5, "quiet"),
    ("джарвис запусти вальхейм", 1.0, "quiet"),
    ("джарвис поставь таймер на пять минут", 0.2, "fan"),
]
GAP = (4, 9)        # сек тишины между фразами — дома тихо почти всё время


def tts(text):
    eng = speech._OneCore("Pavel", 0, 100, 1.0)
    w = wave.open(io.BytesIO(asyncio.run(eng._render(text))))
    data = w.readframes(w.getnframes())
    if w.getnchannels() == 2:
        data = audioop.tomono(data, 2, .5, .5)
    return audioop.ratecv(data, 2, 1, w.getframerate(), RATE, None)[0]


def noise(seconds, kind, rnd):
    n = int(seconds * RATE)
    sigma = {"quiet": 60, "fan": 250, "music": 60}[kind]
    out = []
    for i in range(n):
        v = rnd.gauss(0, sigma)
        if kind == "music":  # аккорд из динамиков
            t = i / RATE
            v += 900 * (math.sin(2 * math.pi * 220 * t) + 0.7 * math.sin(2 * math.pi * 277 * t)
                        + 0.5 * math.sin(2 * math.pi * 330 * t)) * (0.6 + 0.4 * math.sin(2 * math.pi * 0.5 * t))
        out.append(int(max(-32768, min(32767, v))))
    return out


_TTS = {}


def build_track(seed=7):
    rnd = random.Random(seed)
    pcm = []
    for text, gain, kind in PHRASES:
        if text not in _TTS:
            _TTS[text] = tts(text)
        voice = audioop.mul(_TTS[text], 2, gain)
        voice = list(struct.unpack("<%dh" % (len(voice) // 2), voice))
        lead, tail = noise(rnd.uniform(*GAP), kind, rnd), noise(2.0, kind, rnd)
        bed = noise(len(voice) / RATE, kind, rnd)
        mixed = [max(-32768, min(32767, a + b)) for a, b in zip(voice, bed)]
        pcm += lead + mixed + tail
    # а так проходит почти весь день: минута тихой комнаты, минута ноутбука в игре
    pcm += noise(60, "quiet", rnd) + noise(60, "fan", rnd)
    raw = struct.pack("<%dh" % len(pcm), *pcm)
    step = BLOCK * 2
    return [raw[i:i + step] for i in range(0, len(raw) - step + 1, step)]


def run(model_path, blocks, skip):
    lis = speech.Listener(model_path, skip_silence=skip)
    for b in blocks:
        lis._audio.put(b)
    lis._audio.put(None)
    t0 = time.process_time()
    got = list(lis.phrases())
    return got, time.process_time() - t0, lis.stats


def _wake_conf():
    import json
    for name in ("config.json", "config.example.json"):
        try:
            cfg = json.load(open(os.path.join(HOME, name), encoding="utf-8"))
            return cfg["wake_words"], cfg.get("wake_similarity", 0.78)
        except (OSError, KeyError, ValueError):
            pass
    return ["джарвис"], 0.78


WAKE, SIM = _wake_conf()


def is_wake(word):                    # как Jarvis.is_wake
    w = word.lower().replace("ё", "е")
    return any(w == k or (len(w) >= 4 and SequenceMatcher(None, w, k).ratio() >= SIM) for k in WAKE)


def score(got):
    """(фраз нашлось, имён услышано). Фраза нашлась — похожесть ≥ 0.8;
    имя — первое слово распознанного Джарвис узнал бы как обращение."""
    hits = wakes = 0
    for text, _, _ in PHRASES:
        match = max(got, key=lambda g: SequenceMatcher(None, text, g).ratio(), default="")
        if SequenceMatcher(None, text, match).ratio() >= 0.8:
            hits += 1
            if text.startswith("джарвис") and match.split() and is_wake(match.split()[0]):
                wakes += 1
    return hits, wakes


def main():
    if "-v" in sys.argv:              # видно, когда пропуск открывается и закрывается
        import logging
        sys.argv.remove("-v")
        logging.basicConfig(level=logging.DEBUG, format="      %(message)s")
    model = os.path.join(HOME, sys.argv[1] if len(sys.argv) > 1 else "vosk-model-small-ru-0.22")
    seeds = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    n_wake = sum(t.startswith("джарвис") for t, _, _ in PHRASES)
    total = {False: [0, 0, 0.0, 0, 0], True: [0, 0, 0.0, 0, 0]}
    for seed in range(7, 7 + seeds):
        blocks = build_track(seed)
        print(f"Запись {seed}: {len(blocks) * BLOCK / RATE:.0f} с, фраз {len(PHRASES)}; "
              f"модель {os.path.basename(model)}")
        for skip in (False, True):
            got, cpu, st = run(model, blocks, skip)
            hits, wakes = score(got)
            t = total[skip]
            t[0] += hits; t[1] += wakes; t[2] += cpu; t[3] += st["decoded"]; t[4] += st["blocks"]
            print(f"  {'пропуск тишины' if skip else 'всё подряд   '}: фраз {hits}/{len(PHRASES)}, "
                  f"имя {wakes}/{n_wake}, ЦП {cpu:.1f} с, Vosk работал "
                  f"{100 * st['decoded'] / max(1, st['blocks']):.0f}%")
            if seeds == 1:
                for g in got:
                    print("     ", g)
    (h0, w0, c0, *_), (h1, w1, c1, d1, b1) = total[False], total[True]
    print(f"\nИтог ({seeds} зап.): фраз {h0} → {h1}, имя услышано {w0} → {w1} "
          f"из {n_wake * seeds}, процессор −{100 * (1 - c1 / c0):.0f}% "
          f"(Vosk работал {100 * d1 / b1:.0f}% времени)")
    ok = h1 >= h0 and w1 >= w0 - max(1, seeds // 3)   # разброс на грани слышимости
    print("ПРОВЕРКА ПРОЙДЕНА" if ok else "ПРОВЕРКА НЕ ПРОЙДЕНА: с пропуском тишины теряются фразы")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
