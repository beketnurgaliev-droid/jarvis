import asyncio, audioop, gc, io, json, sys, time, wave
import os
import psutil
HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # папка Джарвиса
sys.path.insert(0, HOME)
from speech import _OneCore
from vosk import Model, KaldiRecognizer, SetLogLevel

SetLogLevel(-1)
phrases = ["джарвис открой доту", "джарвис запусти контр страйк", "джарвис включи пабг",
           "джарвис переключись на телеграм", "джарвис сверни дискорд", "джарвис игровой режим",
           "джарвис закрой хром", "джарвис какая погода в алматы", "джарвис запусти вальхейм",
           "джарвис громкость тридцать"]
tts = _OneCore("Pavel", 0, 100, 1.0)
pcm = []
for p in phrases:
    w = wave.open(io.BytesIO(asyncio.run(tts._render(p))))
    data = w.readframes(w.getnframes())
    if w.getnchannels() == 2:
        data = audioop.tomono(data, 2, .5, .5)
    pcm.append(audioop.ratecv(data, 2, 1, w.getframerate(), 16000, None)[0] + b"\0" * 16000)

proc = psutil.Process()
for path in ["vosk-model-small-ru-0.22", "vosk-model-ru-0.42"]:
    m0 = proc.memory_info().rss
    t = time.time()
    model = Model(os.path.join(HOME, path))
    load = time.time() - t
    ok, t = 0, time.time()
    for p, data in zip(phrases, pcm):
        rec = KaldiRecognizer(model, 16000)
        for i in range(0, len(data), 8000):
            rec.AcceptWaveform(data[i:i + 8000])
        got = json.loads(rec.FinalResult())["text"]
        ok += got == p
        print(("  OK " if got == p else "  -- ") + got)
    print(f"{path}: загрузка {load:.1f} с, память +{(proc.memory_info().rss - m0) / 2**30:.1f} ГБ, "
          f"распознавание {time.time() - t:.1f} с на 10 фраз, точно {ok}/10\n")
    model = None
    gc.collect()
