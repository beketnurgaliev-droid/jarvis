import json, os, re, sys, time
os.chdir(r"C:\Users\beket\projects\jarvis")
sys.path.insert(0, os.getcwd())
import brain, games, skills, windows

cfg = json.load(open("config.json", encoding="utf-8"))
games.refresh(cfg)
skills.index_programs()
skills.LEARNED = skills.LEARNED.with_name("learned.test.json")   # не пачкаем настоящий

done = []
windows.focus = lambda w: done.append(("вперёд", w.exe))
windows.minimize = lambda w: done.append(("свернуть", w.exe))
windows.close = lambda w: done.append(("закрыть", w.exe))
windows.minimize_all = lambda: done.append(("свернуть всё",))
windows.restore_all = lambda: done.append(("вернуть всё",))
skills.launch = lambda t: done.append(("запуск", t))
skills.set_volume = lambda n: done.append(("громкость", n)) or n
skills.nudge_volume = lambda d: done.append(("громкость", f"{d:+}")) or 50
skills._press = lambda vk: done.append(("клавиша", hex(vk)))

print("=== Локально, без агента ===")
for phrase in ["севернее все окна", "не все окна", "свер ни все окна", "сверни все окно",
               "сверну все окна", "громчее", "запуск ти доту", "закрои хром", "откройте дискорд",
               "игравой режим", "которые час",
               # это должно уйти агенту, а не превратиться в команду
               "закрою глаза и расскажи сказку", "какая погода в алматы",
               "который час в москве", "не все окна закрыты", "открою тебе секрет"]:
    done.clear()
    t = time.time()
    r = skills.handle(phrase, cfg)
    ms = (time.time() - t) * 1000
    print(f"{phrase:32} -> {((r.ask or r.say) if r else '(агенту)')!s:40} {done} {ms:.0f} мс")

print("\n=== Через агента (то, что локально не угадать) ===")
hint = skills.command_hint(cfg)
for phrase in ["убери с экрана все окошки", "поставь звук на половину", "какая погода в алматы"]:
    done.clear()
    t = time.time()
    text = brain.ask(phrase, cfg, hint)
    m = re.search(r"КОМАНДА\s*:\s*(.+)", text or "", re.I)
    if m:
        cmd = m.group(1).strip(" .!«»\"'")
        r = skills.handle(cmd, cfg)
        if r:
            skills.remember(phrase, cmd)
        text = f"КОМАНДА «{cmd}» -> {(r.ask or r.say) if r else 'не разобрал'}"
    print(f"{phrase:32} -> {text}  {done}  {time.time() - t:.1f} с")

print("\n=== Повтор: выученное работает без агента ===")
for phrase in ["убери с экрана все окошки", "поставь звук на половину"]:
    done.clear()
    t = time.time()
    r = skills.handle(phrase, cfg)
    print(f"{phrase:32} -> {(r.say if r else '(агенту)')}  {done}  {(time.time() - t) * 1000:.0f} мс")

skills.LEARNED.unlink(missing_ok=True)
