#!/usr/bin/env python3
"""radio-taiso core: balance logic, Claude Code hooks, CLI.

Design: 03_design.md. Инварианты, которые нельзя нарушать:
- fail-open: любая ошибка => работа НЕ блокируется (deny только при
  положительно прочитанном turn_allowed=0 своей сессии);
- все записи BEGIN IMMEDIATE + check-and-set, списания относительным SQL;
- stdin-данные только параметрами (SQL ?, argv), никаких shell-строк.
"""
import datetime
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time

STDIN_CAP = 262144  # 256 KB: больше — не hook-вход, fail-open
SQLITE_TIMEOUT = 0.15
METACHARS = set(';|&$(){}<>`\n')

DEFAULT_CONFIG = {
    "work_minutes_per_exercise": 40,
    "exercise_seconds": 180,
    "debt_limit_minutes": 20,
    "debt_penalty_seconds_per_minute": 4.5,
    "free_morning_minutes": 40,
    "idle_gap_minutes": 15,
    "video_path": "",  # install.sh заполняет абсолютным путём внутри TAISO_DIR
    "video_url": "https://www.youtube.com/watch?v=UVwKbfYlJUM",
    "lang": "en",  # "en" | "ru" — язык menubar, окна и статус-строк
    "count_all_apps": True,  # учёт активности во всех приложениях (menubar-тикер)
    "feedback_prompt": True,  # вечерний диалог «как прошёл день» (menubar)
    "video_muted": 0,  # 1 = видео без звука (кто работает под свою музыку)
}
CONFIG_BOUNDS = {  # (min, max) — отрицательные/дикие значения не должны ломать экономику
    "work_minutes_per_exercise": (5, 480),
    "exercise_seconds": (30, 3600),
    "debt_limit_minutes": (0, 240),
    "debt_penalty_seconds_per_minute": (0, 60),
    "free_morning_minutes": (0, 480),
    "idle_gap_minutes": (1, 120),
    "video_muted": (0, 1),
}


def taiso_dir():
    return os.environ.get("TAISO_DIR") or os.path.expanduser("~/.radio-taiso")


def db_path():
    return os.path.join(taiso_dir(), "taiso.db")


def config_path():
    return os.path.join(taiso_dir(), "config.json")


def log_error(msg):
    try:
        with open(os.path.join(taiso_dir(), "error.log"), "a") as f:
            f.write("%s %s\n" % (datetime.datetime.utcnow().isoformat(), msg))
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path()) as f:
            raw = json.load(f)
        for k, v in raw.items():
            if k not in cfg:
                continue
            if isinstance(v, bool) and k in ("count_all_apps", "feedback_prompt"):
                cfg[k] = v
            elif k in CONFIG_BOUNDS:
                lo, hi = CONFIG_BOUNDS[k]
                if isinstance(v, (int, float)) and lo <= v <= hi:
                    cfg[k] = v
            elif isinstance(v, str):
                if k == "video_url" and not re.match(
                        r"^https://(www\.)?(youtube\.com|youtu\.be)/", v):
                    continue
                cfg[k] = v
    except (OSError, ValueError):
        pass  # дефолты
    return cfg


def connect():
    os.makedirs(taiso_dir(), mode=0o700, exist_ok=True)
    fresh = not os.path.exists(db_path())
    last_err = None
    for _ in range(3):  # retry 3×50 мс (03_design W1)
        try:
            con = sqlite3.connect(db_path(), timeout=SQLITE_TIMEOUT)
            con.execute("PRAGMA busy_timeout=150")
            con.execute("PRAGMA journal_mode=WAL")
            ensure_schema(con, fresh)
            try:
                os.chmod(db_path(), 0o600)
            except OSError:
                pass
            return con
        except sqlite3.OperationalError as e:
            last_err = e
            time.sleep(0.05)
    raise last_err


def ensure_schema(con, fresh):
    have = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "state" in have and "events" in have and "sessions" in have and "profile" in have:
        try:
            con.execute("ALTER TABLE state ADD COLUMN pause_until_utc INTEGER NOT NULL DEFAULT 0")
            con.commit()
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        return
    recreated = bool(have) and "state" not in have  # что-то было, но схема битая
    con.executescript("""
        CREATE TABLE IF NOT EXISTS state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            balance_seconds INTEGER NOT NULL,
            last_activity_utc INTEGER NOT NULL,
            day_credit_date TEXT NOT NULL,
            video_position_sec REAL NOT NULL DEFAULT 0,
            pause_until_utc INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            turn_allowed INTEGER NOT NULL,
            updated_utc INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc INTEGER NOT NULL,
            type TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS profile (
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            ts_utc INTEGER NOT NULL
        );
    """)
    con.execute(
        "INSERT OR IGNORE INTO state (id, balance_seconds, last_activity_utc, day_credit_date)"
        " VALUES (1, 0, ?, '')", (int(time.time()),))
    con.commit()
    if fresh or recreated:
        add_event(con, "db_recreated" if recreated else "db_created", {})


def add_event(con, etype, payload):
    # payload: только числа/типы/таймстампы — никаких текстов промптов (security 8)
    con.execute("INSERT INTO events (ts_utc, type, payload) VALUES (?, ?, ?)",
                (int(time.time()), etype, json.dumps(payload)))
    con.commit()


def local_date_str(ts=None):
    return datetime.datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- turn logic

def process_turn(con, cfg, session_id, now=None):
    """Граница хода: кредит утра, списание активности, вердикт для сессии.
    Возвращает (balance_seconds, allowed)."""
    now = int(now or time.time())
    today = local_date_str(now)
    free = int(cfg["free_morning_minutes"] * 60)
    idle_gap = int(cfg["idle_gap_minutes"] * 60)
    debt_limit = int(cfg["debt_limit_minutes"] * 60)

    con.execute("BEGIN IMMEDIATE")
    cur = con.execute(
        "UPDATE state SET balance_seconds = balance_seconds + ?, day_credit_date = ?"
        " WHERE id = 1 AND day_credit_date != ?", (free, today, today))
    got_credit = cur.rowcount > 0
    row = con.execute(
        "SELECT balance_seconds, last_activity_utc, pause_until_utc"
        " FROM state WHERE id = 1").fetchone()
    last, pause_until = row[1], row[2]
    if now < pause_until:  # режим встречи: заморозка счёта и блока
        con.execute("UPDATE state SET last_activity_utc = ? WHERE id = 1", (now,))
        con.execute(
            "INSERT INTO sessions (session_id, turn_allowed, updated_utc) VALUES (?, 1, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET turn_allowed = 1, updated_utc = ?",
            (session_id, now, now))
        con.commit()
        return row[0], 1
    gap = now - last
    charged = gap if 0 < gap < idle_gap else 0
    con.execute(
        "UPDATE state SET balance_seconds = balance_seconds - ?, last_activity_utc = ?"
        " WHERE id = 1", (charged, now))
    balance = con.execute(
        "SELECT balance_seconds FROM state WHERE id = 1").fetchone()[0]
    allowed = compute_allowed(con, cfg, balance)
    con.execute(
        "INSERT INTO sessions (session_id, turn_allowed, updated_utc) VALUES (?, ?, ?)"
        " ON CONFLICT(session_id) DO UPDATE SET turn_allowed = ?, updated_utc = ?",
        (session_id, allowed, now, allowed, now))
    con.commit()

    if got_credit:
        add_event(con, "morning_credit", {"seconds": free})
    if charged:
        add_event(con, "activity", {"charged_seconds": charged})
    if balance < 0 and balance + charged >= 0:
        add_event(con, "debt_enter", {"balance": balance})
    if not allowed:
        add_event(con, "block", {"balance": balance, "session": session_id[:16]})
    return balance, allowed


def postponed_since_exercise(con):
    """Был ли уже «долг по кнопке» после последней зарядки (раз за период)."""
    row = con.execute(
        "SELECT type FROM events WHERE type IN ('postpone', 'exercise_done')"
        " ORDER BY id DESC LIMIT 1").fetchone()
    return row is not None and row[0] == "postpone"


def compute_allowed(con, cfg, balance):
    """Блок на нуле; после разовой кнопки долга — блок на −debt_limit."""
    debt_limit = int(cfg["debt_limit_minutes"] * 60)
    if balance > 0:
        return 1
    if postponed_since_exercise(con) and balance > -debt_limit:
        return 1
    return 0


def required_exercise_seconds(cfg, balance):
    # Вход всегда стоит один ролик: привычка строится на дешёвом входе.
    # Долг наказывает выплатой (гасится из次 payout), а не длительностью зарядки.
    return min(int(cfg["exercise_seconds"]), 900)  # потолок: битые метаданные — не пытка


def lang(cfg):
    return "ru" if cfg.get("lang") == "ru" else "en"


def balance_line(cfg, balance, allowed):
    mins = balance // 60
    if lang(cfg) == "ru":
        if not allowed:
            return "⛩ БЛОК — скажи «Начать зарядку»"
        if balance < 0:
            return "⛩ −%d мин ДОЛГ" % (abs(balance) // 60)
        return "⛩ %d мин" % mins
    if not allowed:
        return "⛩ BLOCKED — say “Start exercise”"
    if balance < 0:
        return "⛩ −%d min DEBT" % (abs(balance) // 60)
    return "⛩ %d min" % mins


# ---------------------------------------------------------------- hooks

def read_stdin_json():
    data = sys.stdin.read(STDIN_CAP + 1)
    if len(data) > STDIN_CAP:
        raise ValueError("stdin too large")
    return json.loads(data)


def hook_prompt():
    """UserPromptSubmit: учёт + вердикт + контекст агенту. Всегда exit 0."""
    try:
        inp = read_stdin_json()
        session_id = str(inp.get("session_id", "unknown"))[:128]
        cfg = load_config()
        con = connect()
        balance, allowed = process_turn(con, cfg, session_id)
        line = balance_line(cfg, balance, allowed)
        ru = lang(cfg) == "ru"
        if allowed:
            if ru:
                print("[radio-taiso] Баланс оператора: %s. Закончи ЭТОТ ответ отдельной строкой: %s "
                      "(только когда такая инструкция есть в текущем ходе; без неё строку не добавляй)." % (line, line))
            else:
                print("[radio-taiso] Operator balance: %s. End THIS reply with this line on its own: %s "
                      "(only when this instruction appears in the current turn; never add it otherwise)." % (line, line))
        else:
            req = required_exercise_seconds(cfg, balance)
            can_postpone = not postponed_since_exercise(con)
            hint_ru = (" Если оператору совсем некогда: в окне зарядки есть разовая "
                       "кнопка «Взять в долг +20 мин»." if can_postpone else "")
            hint_en = (" If the operator truly can't now: the exercise window has a "
                       "one-time 'Borrow +20 min' button." if can_postpone else "")
            if ru:
                print("[radio-taiso] РАБОТА ЗАБЛОКИРОВАНА: баланс движения оператора исчерпан "
                      "(долг {} мин). Инструменты недоступны, пока оператор не сделает зарядку "
                      "Radio Taiso ({}:{:02d}). Твоя задача: доброжелательно, своими словами, с "
                      "привязкой к текущей работе предложить размяться прямо сейчас. "
                      "Единственная разрешённая команда: Bash `{} go` — предложи запустить "
                      "зарядку ею. Не выполняй другую работу и не обещай её выполнить до зарядки. "
                      "Если у оператора звонок/встреча — скажи ему про `taiso pause 60` "
                      "(пауза, раз в звонок). Закончи ЭТОТ ответ строкой: {}".format(
                          abs(balance) // 60, req // 60, req % 60, unlock_cli(), line)
                      + hint_ru)
            else:
                print("[radio-taiso] WORK BLOCKED: the operator's movement balance is spent "
                      "(debt {} min). Tools are unavailable until the operator does the "
                      "Radio Taiso exercise ({}:{:02d}). Your job: kindly, in your own words, "
                      "tied to the current work, invite them to move right now. The only "
                      "allowed command is Bash `{} go` — offer to start the exercise with it. "
                      "Do not perform or promise other work before the exercise. "
                      "If the operator is on a call/meeting — mention `taiso pause 60` "
                      "(meeting pause). End THIS reply with: {}".format(
                          abs(balance) // 60, req // 60, req % 60, unlock_cli(), line)
                      + hint_en)
        con.close()
    except Exception as e:  # fail-open всегда
        log_error("hook_prompt: %r" % e)
    sys.exit(0)


RC_MARKER = "# radio-taiso codex adapter"
RC_LINE = 'export PATH="$HOME/.radio-taiso/shim:$PATH"'


def rc_set_path(enable):
    """PATH-строка шима: только в СУЩЕСТВУЮЩИХ rc-файлах; снятие — хирургическое."""
    for rcname in ("~/.zshrc", "~/.bashrc"):
        rc = os.path.expanduser(rcname)
        if not os.path.exists(rc):
            continue
        try:
            lines = open(rc).read().split("\n")
            cleaned = [l for l in lines if l.strip() not in (RC_MARKER, RC_LINE)
                       and not l.strip().startswith("export PATH=\"$HOME/.radio-taiso/")]
            if enable:
                cleaned += [RC_MARKER, RC_LINE]
            with open(rc, "w") as f:
                f.write("\n".join(cleaned).rstrip("\n") + "\n")
        except OSError:
            pass


def unlock_cli():
    """Команда разблокировки для сообщений: bare `taiso`, если есть в PATH, иначе абсолютный путь."""
    import shutil
    if shutil.which("taiso"):
        return "taiso"
    return os.path.join(taiso_dir(), "bin", "taiso")


def is_unlock_command(raw_cmd):
    """Строгий allowlist: ровно `taiso go|status`, без метасимволов и обвязок."""
    if not isinstance(raw_cmd, str) or set(raw_cmd) & METACHARS:
        return False
    try:
        argv = shlex.split(raw_cmd)
    except ValueError:
        return False
    ok2 = len(argv) == 2 and argv[1] in ("go", "status", "resume")
    ok3 = (len(argv) == 3 and argv[1] == "pause" and argv[2].isdigit()
           and 1 <= int(argv[2]) <= 180)
    if not (ok2 or ok3):
        return False
    prog = argv[0]
    return os.path.basename(prog) == "taiso" and (
        prog == "taiso" or os.path.isabs(prog))


def charge_activity(con, cfg, now=None):
    """Лёгкое списание активности (для PreToolUse: работа агента = работа)."""
    now = int(now or time.time())
    idle_gap = int(cfg["idle_gap_minutes"] * 60)
    con.execute("BEGIN IMMEDIATE")
    last, pause_until = con.execute(
        "SELECT last_activity_utc, pause_until_utc FROM state WHERE id = 1").fetchone()
    if now < pause_until:
        con.execute("UPDATE state SET last_activity_utc = ? WHERE id = 1", (now,))
        con.commit()
        return
    gap = now - last
    charged = gap if 0 < gap < idle_gap else 0
    con.execute(
        "UPDATE state SET balance_seconds = balance_seconds - ?, last_activity_utc = ?"
        " WHERE id = 1", (charged, now))
    con.commit()


def hook_tool():
    """PreToolUse: deny только при положительно прочитанном turn_allowed=0."""
    try:
        inp = read_stdin_json()
        session_id = str(inp.get("session_id", "unknown"))[:128]
        tool = inp.get("tool_name", "")
        con = connect()
        charge_activity(con, load_config())  # агент работает — время идёт
        row = con.execute(
            "SELECT turn_allowed FROM sessions WHERE session_id = ?",
            (session_id,)).fetchone()
        con.close()
        if row is None or row[0] != 0:
            sys.exit(0)  # allow: нет положительного запрета
        if tool == "Bash":
            cmd = (inp.get("tool_input") or {}).get("command", "")
            if is_unlock_command(cmd):
                # НЕ пропускаем в shell (там может быть подложенный `taiso`):
                # действие выполняем сами, фиксированным путём, и всё равно deny.
                argv = shlex.split(cmd)
                if argv[1] == "pause":
                    mins = int(argv[2])
                    do_pause(mins)
                    print(json.dumps({"hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason":
                            "radio-taiso: пауза на %d мин включена (режим встречи) — "
                            "работа разблокирована со следующего сообщения оператора." % mins}}))
                    sys.exit(0)
                if argv[1] == "resume":
                    do_pause(0)
                    print(json.dumps({"hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason":
                            "radio-taiso: пауза снята — обычный режим со следующего сообщения."}}))
                    sys.exit(0)
                opened = spawn_window()
                msg = ("radio-taiso: окно зарядки открыто — пусть оператор сделает "
                       "Radio Taiso, потом продолжим." if lang(load_config()) == "ru" else
                       "radio-taiso: exercise window opened — let the operator do "
                       "Radio Taiso, then continue.") if opened else (
                       "radio-taiso: окно зарядки не установлено (taiso doctor)."
                       if lang(load_config()) == "ru" else
                       "radio-taiso: exercise window not installed (taiso doctor).")
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": msg}}))
                sys.exit(0)
        reason_ru = ("radio-taiso: работа заблокирована до зарядки. Единственная "
                     "разрешённая команда — ровно `%s go` (без пайпов и цепочек). "
                     "Предложи оператору запустить её." % unlock_cli())
        reason_en = ("radio-taiso: work is blocked until the exercise is done. The only "
                     "allowed command is exactly `%s go` (no pipes or chains). "
                     "Offer the operator to run it." % unlock_cli())
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason":
                    reason_ru if lang(load_config()) == "ru" else reason_en,
            }
        }))
    except Exception as e:
        log_error("hook_tool: %r" % e)
    sys.exit(0)  # fail-open


# ---------------------------------------------------------------- exercise

def exercise_required():
    cfg = load_config()
    con = connect()
    balance = con.execute(
        "SELECT balance_seconds FROM state WHERE id = 1").fetchone()[0]
    con.close()
    print(required_exercise_seconds(cfg, balance))


def partial_credit_since_done(con):
    """Сумма частичных кредитов (Esc посреди ролика) с последней полной зарядки."""
    last = con.execute("SELECT COALESCE(MAX(ts_utc), 0) FROM events "
                       "WHERE type = 'exercise_done'").fetchone()[0]
    total = 0
    for (payload,) in con.execute(
            "SELECT payload FROM events WHERE type = 'exercise_partial' "
            "AND ts_utc > ?", (last,)):
        try:
            total += int(json.loads(payload).get("credit", 0))
        except (ValueError, TypeError):
            pass
    return total


def exercise_done(duration):
    cfg = load_config()
    grant = int(cfg["work_minutes_per_exercise"] * 60)
    con = connect()
    con.execute("BEGIN IMMEDIATE")
    balance = con.execute(
        "SELECT balance_seconds FROM state WHERE id = 1").fetchone()[0]
    # выплата за период одна: полный grant минус уже выданные частичные кредиты;
    # долг гасится из выплаты (balance отрицательный — просто прибавляем)
    grant_left = max(0, grant - partial_credit_since_done(con))
    new_balance = balance + grant_left
    con.execute(
        "UPDATE state SET balance_seconds = ?, video_position_sec = 0 WHERE id = 1",
        (new_balance,))
    con.execute("UPDATE sessions SET turn_allowed = 1")
    con.commit()
    add_event(con, "exercise_done",
              {"duration": int(duration), "grant": grant_left,
               "debt_repaid": max(0, -balance)})
    con.close()


def exercise_partial(accrued, position):
    """Esc посреди ролика: прогресс банкуется пропорционально, а не сгорает."""
    cfg = load_config()
    grant = int(cfg["work_minutes_per_exercise"] * 60)
    exsec = max(1, int(cfg["exercise_seconds"]))
    con = connect()
    con.execute("BEGIN IMMEDIATE")
    balance = con.execute(
        "SELECT balance_seconds FROM state WHERE id = 1").fetchone()[0]
    already = partial_credit_since_done(con)
    credit = min(int(grant * min(1.0, accrued / exsec)), max(0, grant - already))
    new_balance = balance + credit
    con.execute(
        "UPDATE state SET balance_seconds = ?, video_position_sec = ? WHERE id = 1",
        (new_balance, float(position)))
    if new_balance > 0:
        con.execute("UPDATE sessions SET turn_allowed = 1")
    con.commit()
    add_event(con, "exercise_partial",
              {"accrued": int(accrued), "credit": credit,
               "position": float(position)})
    con.close()
    print(credit)


def exercise_abort(position):
    con = connect()
    con.execute("BEGIN IMMEDIATE")
    con.execute("UPDATE state SET video_position_sec = ? WHERE id = 1",
                (float(position),))
    con.commit()
    add_event(con, "exercise_abort", {"position": float(position)})
    con.close()


def video_pos(set_value=None):
    con = connect()
    if set_value is not None:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE state SET video_position_sec = ? WHERE id = 1",
                    (float(set_value),))
        con.commit()
    else:
        print(con.execute(
            "SELECT video_position_sec FROM state WHERE id = 1").fetchone()[0])
    con.close()


# ---------------------------------------------------------------- CLI

def cmd_status(one_line=False):
    cfg = load_config()
    con = connect()
    balance, last, pause_until = con.execute(
        "SELECT balance_seconds, last_activity_utc, pause_until_utc"
        " FROM state WHERE id = 1").fetchone()
    # живой каунтдаун: прогноз с учётом натёкшего с последней активности
    gap = int(time.time()) - last
    if 0 < gap < int(cfg["idle_gap_minutes"] * 60):
        balance -= gap
    debt_limit = int(cfg["debt_limit_minutes"] * 60)
    allowed = compute_allowed(con, cfg, balance)
    con.close()
    if time.time() < pause_until:
        hhmm = datetime.datetime.fromtimestamp(pause_until).strftime("%H:%M")
        print(("⛩ ⏸ пауза до %s" if lang(cfg) == "ru" else "⛩ ⏸ paused until %s") % hhmm)
        if not one_line:
            print("taiso resume — вернуться." if lang(cfg) == "ru" else "taiso resume — back to work mode.")
        return
    line = balance_line(cfg, balance, allowed)
    if one_line:
        print(line)
        return
    print(line)
    ru = lang(cfg) == "ru"
    req = required_exercise_seconds(cfg, balance)
    if balance > 0:
        print("До нуля: %d мин активной работы." % (balance // 60) if ru
              else "%d min of active work until zero." % (balance // 60))
    elif allowed:
        print("Долг %d из %d мин. Следующая зарядка: %d сек."
              % (abs(balance) // 60, debt_limit // 60, req) if ru
              else "Debt %d of %d min. Next exercise: %d sec."
              % (abs(balance) // 60, debt_limit // 60, req))
    else:
        print("Заблокировано. Разблокировка: taiso go (%d сек зарядки)." % req if ru
              else "Blocked. Unlock: taiso go (%d sec of exercise)." % req)


def do_pause(minutes):
    """Режим встречи: заморозка блока и счёта на N минут (0 = снять)."""
    con = connect()
    until = int(time.time()) + minutes * 60 if minutes > 0 else 0
    con.execute("BEGIN IMMEDIATE")
    con.execute("UPDATE state SET pause_until_utc = ?, last_activity_utc = ?"
                " WHERE id = 1", (until, int(time.time())))
    con.execute("UPDATE sessions SET turn_allowed = 1")
    con.commit()
    add_event(con, "pause", {"minutes": minutes})
    con.close()
    return until


def spawn_window():
    """Запуск окна фиксированным путём, detached. True если бинарь есть."""
    window = os.path.join(taiso_dir(), "bin", "TaisoWindow")
    if not os.path.exists(window):
        return False
    subprocess.Popen([window], start_new_session=True,
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def cmd_go():
    if not spawn_window():
        print("Окно зарядки не установлено. Запусти install.sh (taiso doctor).")
        sys.exit(1)
    print("Зарядка запущена — окно открывается. Встань, отойди на шаг от стола."
          if lang(load_config()) == "ru" else
          "Exercise started — the window is opening. Stand up, step away from the desk.")


def _day_of(ts):
    return local_date_str(ts)


def cmd_stats(report=False):
    con = connect()
    rows = con.execute(
        "SELECT ts_utc, type, payload FROM events ORDER BY ts_utc").fetchall()
    con.close()
    today = local_date_str()
    week_ago = time.time() - 7 * 86400
    done_days = sorted({_day_of(ts) for ts, t, _ in rows if t == "exercise_done"})
    done_today = sum(1 for ts, t, _ in rows
                     if t == "exercise_done" and _day_of(ts) == today)
    done_week = sum(1 for ts, t, _ in rows
                    if t == "exercise_done" and ts >= week_ago)
    aborts_week = sum(1 for ts, t, _ in rows
                      if t in ("exercise_abort", "exercise_partial")
                      and ts >= week_ago)
    # стрик: подряд дней с зарядкой, заканчивая сегодня/вчера
    streak = 0
    d = datetime.date.today()
    days_set = set(done_days)
    if d.strftime("%Y-%m-%d") not in days_set:
        d -= datetime.timedelta(days=1)
    while d.strftime("%Y-%m-%d") in days_set:
        streak += 1
        d -= datetime.timedelta(days=1)
    honest = (100 * done_week // (done_week + aborts_week)) \
        if (done_week + aborts_week) else 100
    ru = lang(load_config()) == "ru"
    print("Зарядок сегодня: %d · за неделю: %d · стрик: %d дн. · "
          "завершено без обрыва: %d%%" % (done_today, done_week, streak, honest) if ru
          else "Exercises today: %d · this week: %d · streak: %d d · "
          "completed without abort: %d%%" % (done_today, done_week, streak, honest))
    if report:
        blocks_week = sum(1 for ts, t, _ in rows if t == "block" and ts >= week_ago)
        recreated = sum(1 for _, t, _ in rows if t == "db_recreated")
        print("Блокировок за неделю: %d · пересозданий базы: %d" % (blocks_week, recreated)
              if ru else
              "Blocks this week: %d · db recreations: %d" % (blocks_week, recreated))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    cmd = args[0]
    if cmd == "hook-prompt":
        hook_prompt()
    elif cmd == "hook-tool":
        hook_tool()
    elif cmd == "status":
        cmd_status(one_line="--line" in args)
    elif cmd == "go":
        cmd_go()
    elif cmd == "pause":
        mins = int(args[1]) if len(args) > 1 and args[1].isdigit() else 60
        mins = max(1, min(180, mins))
        until = do_pause(mins)
        print(("⏸ Пауза до %s — блок и счётчик заморожены. Снять: taiso resume"
               if lang(load_config()) == "ru" else
               "⏸ Paused until %s — blocking and counting frozen. Undo: taiso resume")
              % datetime.datetime.fromtimestamp(until).strftime("%H:%M"))
    elif cmd == "resume":
        do_pause(0)
        print("▶ Обычный режим." if lang(load_config()) == "ru" else "▶ Back to normal.")
    elif cmd == "stats":
        cmd_stats(report="--report" in args)
    elif cmd == "exercise-required":
        exercise_required()
    elif cmd == "exercise-done":
        exercise_done(float(args[args.index("--duration") + 1])
                      if "--duration" in args else 0)
    elif cmd == "exercise-abort":
        exercise_abort(float(args[args.index("--position") + 1])
                       if "--position" in args else 0)
    elif cmd == "exercise-partial":
        exercise_partial(
            float(args[args.index("--accrued") + 1]) if "--accrued" in args else 0,
            float(args[args.index("--position") + 1]) if "--position" in args else 0)
    elif cmd == "overlay-stats":
        # для окна: «Сегодня: N мин · стрик: N дн» — фокус на прогрессе (фидбек Юрия)
        cfg = load_config()
        con = connect()
        rows = con.execute("SELECT ts_utc, type, payload FROM events "
                           "WHERE type IN ('exercise_done', 'exercise_partial')").fetchall()
        con.close()
        today = time.strftime("%Y-%m-%d", time.localtime())

        def _day(ts):
            return time.strftime("%Y-%m-%d", time.localtime(ts))

        secs_today = 0
        for ts, t, payload in rows:
            if _day(ts) != today:
                continue
            try:
                d = json.loads(payload)
            except ValueError:
                d = {}
            secs_today += int(d.get("duration", 0) if t == "exercise_done"
                              else d.get("accrued", 0))
        done_days = sorted({_day(ts) for ts, t, _ in rows if t == "exercise_done"})
        streak = 0
        day = today
        while day in done_days:
            streak += 1
            day = time.strftime(
                "%Y-%m-%d", time.localtime(time.mktime(
                    time.strptime(day, "%Y-%m-%d")) - 86400))
        mins = (secs_today + 59) // 60
        if lang(cfg) == "ru":
            print("Сегодня: %d мин · стрик: %d дн" % (mins, streak))
        else:
            print("Today: %d min · streak: %d d" % (mins, streak))
    elif cmd == "video-pos":
        video_pos(float(args[args.index("--set") + 1])
                  if "--set" in args else None)
    elif cmd == "config-set":
        # taiso config-set <key> <value> — только известные числовые ключи в границах
        key, value = args[1], args[2]
        if key not in CONFIG_BOUNDS:
            print("Недопустимый ключ: %s" % key)
            sys.exit(1)
        lo, hi = CONFIG_BOUNDS[key]
        try:
            num = float(value) if "." in value else int(value)
        except ValueError:
            print("Не число: %s" % value)
            sys.exit(1)
        if not (lo <= num <= hi):
            print("Вне границ [%s..%s]" % (lo, hi))
            sys.exit(1)
        cfg = load_config()
        cfg[key] = num
        with open(config_path(), "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("%s = %s" % (key, num))
    elif cmd == "video":
        # докачать видео позже (обновляет yt-dlp, YouTube отдаёт 403 старым версиям)
        script = os.path.join(taiso_dir(), "bin", "fetch-video.sh")
        rc = subprocess.call(["/bin/bash", script]) if os.path.exists(script) else 1
        sys.exit(rc)
    elif cmd == "doctor":
        # диагностика для удалённой поддержки: одна команда, весь вывод — в чат
        import shutil as _sh
        import subprocess as _sp
        d = taiso_dir()
        print("== radio-taiso doctor ==")
        print("dir:", d, "| shell:", os.environ.get("SHELL", "?"), "| lang:", lang(load_config()))
        try:
            cmd_status(one_line=True)
        except Exception as e:
            print("status: ERROR", e)
        print("menubar running:", "yes" if _sp.run(["pgrep", "-f", "TaisoWindow --menubar"],
              capture_output=True).returncode == 0 else "NO")
        la = os.path.expanduser("~/Library/LaunchAgents/cy.radio-taiso.menubar.plist")
        print("launchagent file:", "yes" if os.path.exists(la) else "NO")
        lst = _sp.run(["launchctl", "list"], capture_output=True, text=True).stdout
        print("launchagent loaded:", "yes" if "cy.radio-taiso.menubar" in lst else "NO")
        print("window binary:", "yes" if os.path.exists(os.path.join(d, "bin", "TaisoWindow")) else "NO")
        print("video:", "yes" if os.path.exists(load_config().get("video_path", "")) else "NO (timer mode)")
        try:
            st = json.load(open(os.path.expanduser("~/.claude/settings.json")))
            ours = sum(1 for ev, ms in (st.get("hooks") or {}).items() for m in ms
                       for h in (m.get("hooks") or []) if h.get("_owner") == "radio-taiso")
            print("claude hooks installed:", ours, "(expect 2)")
        except Exception as e:
            print("claude hooks: cannot read settings.json:", e)
        codex_all = _sp.run(["bash", "-lc", "which -a codex 2>/dev/null"],
                            capture_output=True, text=True).stdout.split()
        shim = os.path.join(d, "bin", "codex")
        if codex_all:
            print("codex in PATH order:", " -> ".join(codex_all))
            print("codex adapter active:", "yes" if codex_all and codex_all[0] == shim else
                  ("NO (shim exists but not first in PATH)" if os.path.exists(shim) else "NO"))
        else:
            print("codex: not installed")
        try:
            with open(os.path.join(d, "error.log")) as f:
                tail = f.readlines()[-5:]
            print("error.log (last 5):"); [print("  " + l.rstrip()) for l in tail]
        except OSError:
            print("error.log: empty")
        con = connect()
        n = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        last = con.execute("SELECT type, datetime(ts_utc,'unixepoch','localtime') FROM events"
                           " ORDER BY id DESC LIMIT 3").fetchall()
        con.close()
        print("events:", n, "| last:", "; ".join("%s@%s" % r for r in last))
        running = _sp.run(["pgrep", "-f", "TaisoWindow --menubar"],
                          capture_output=True).returncode == 0
        if running:
            print("VERDICT: all good. If you don't see ⛩ by the clock — it's hidden "
                  "behind the MacBook notch: quit a couple of other menubar apps "
                  "(or use Bartender/Ice). Exercise now: taiso go")
        else:
            print("VERDICT: menubar is NOT running. Start it: "
                  "launchctl bootstrap gui/$(id -u) %s  (or: %s/bin/TaisoWindow --menubar &)"
                  % (la, d))
    elif cmd == "enable-codex":
        # обёртка codex: блок на старте сессии + тикер активности
        import shutil as _sh
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex-shim.sh")
        shim_dir = os.path.join(taiso_dir(), "shim")  # в PATH только этот каталог
        os.makedirs(shim_dir, mode=0o700, exist_ok=True)
        dst = os.path.join(shim_dir, "codex")
        _sh.copyfile(src, dst)
        os.chmod(dst, 0o755)
        rc_set_path(True)
        print("Codex adapter on. Перезапусти терминал (или source ~/.zshrc).")
    elif cmd == "disable-codex":
        try:
            os.remove(os.path.join(taiso_dir(), "shim", "codex"))
        except OSError:
            pass
        rc_set_path(False)
        print("Codex adapter off.")
    elif cmd == "ping-activity":
        # для обёрток (Codex и др.): тикер активности раз в минуту
        cfg = load_config()
        con = connect()
        charge_activity(con, cfg)
        con.close()
    elif cmd == "gate":
        # для обёрток: 0 = работать можно, 1 = блок (зарядка)
        cfg = load_config()
        con = connect()
        balance = con.execute(
            "SELECT balance_seconds FROM state WHERE id = 1").fetchone()[0]
        allowed = compute_allowed(con, cfg, balance)
        con.close()
        if not allowed:
            req = required_exercise_seconds(cfg, balance)
            ru = lang(cfg) == "ru"
            print(("⛩ Radio Taiso: баланс движения исчерпан. Зарядка %d:%02d — "
                   "и работаем дальше. Запускаю окно…" if ru else
                   "⛩ Radio Taiso: movement balance spent. Exercise %d:%02d — "
                   "then back to work. Opening the window…")
                  % (req // 60, req % 60))
            sys.exit(1)
        sys.exit(0)
    elif cmd == "postpone":
        cfg = load_config()
        con = connect()
        balance = con.execute(
            "SELECT balance_seconds FROM state WHERE id = 1").fetchone()[0]
        if balance > 0:
            print("no-need")
        elif postponed_since_exercise(con):
            print("already-used")
        else:
            add_event(con, "postpone", {"balance": balance})
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE sessions SET turn_allowed = 1")
            con.commit()
            print("ok")
        con.close()
    elif cmd == "postpone-available":
        cfg = load_config()
        con = connect()
        balance = con.execute(
            "SELECT balance_seconds FROM state WHERE id = 1").fetchone()[0]
        if balance > 0:
            print("no-need")
        elif postponed_since_exercise(con):
            print("used")
        else:
            print("yes")
        con.close()
    elif cmd == "config-set-lang":
        value = args[1]
        if value not in ("en", "ru"):
            print("en | ru")
            sys.exit(1)
        cfg = load_config()
        cfg["lang"] = value
        with open(config_path(), "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("lang = %s" % value)
    elif cmd == "config-get-lang":
        print(lang(load_config()))
    elif cmd == "config-get-interval":
        print(int(load_config()["work_minutes_per_exercise"]))
    elif cmd == "watchdog":
        # для menubar: секунды с последней активности hooks
        con = connect()
        last = con.execute(
            "SELECT last_activity_utc FROM state WHERE id = 1").fetchone()[0]
        con.close()
        print(int(time.time()) - last)
    elif cmd == "init":
        connect().close()
        cfg_p = config_path()
        if not os.path.exists(cfg_p):
            with open(cfg_p, "w") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            os.chmod(cfg_p, 0o600)
        print("ok: %s" % taiso_dir())
    else:
        print("Неизвестная команда: %s" % cmd)
        sys.exit(1)


if __name__ == "__main__":
    main()
