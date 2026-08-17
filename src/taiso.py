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
}
CONFIG_BOUNDS = {  # (min, max) — отрицательные/дикие значения не должны ломать экономику
    "work_minutes_per_exercise": (5, 480),
    "exercise_seconds": (30, 3600),
    "debt_limit_minutes": (0, 240),
    "debt_penalty_seconds_per_minute": (0, 60),
    "free_morning_minutes": (0, 480),
    "idle_gap_minutes": (1, 120),
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
            if k in CONFIG_BOUNDS:
                lo, hi = CONFIG_BOUNDS[k]
                if isinstance(v, (int, float)) and lo <= v <= hi:
                    cfg[k] = v
            elif isinstance(v, str):
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
        return
    recreated = bool(have) and "state" not in have  # что-то было, но схема битая
    con.executescript("""
        CREATE TABLE IF NOT EXISTS state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            balance_seconds INTEGER NOT NULL,
            last_activity_utc INTEGER NOT NULL,
            day_credit_date TEXT NOT NULL,
            video_position_sec REAL NOT NULL DEFAULT 0
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
        "SELECT balance_seconds, last_activity_utc FROM state WHERE id = 1").fetchone()
    last = row[1]
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
    debt_min = max(0, -balance) / 60.0
    return int(cfg["exercise_seconds"] + cfg["debt_penalty_seconds_per_minute"] * debt_min)


def lang(cfg):
    return "ru" if cfg.get("lang") == "ru" else "en"


def balance_line(cfg, balance, allowed):
    mins = balance // 60
    if lang(cfg) == "ru":
        if not allowed:
            return "⛩ БЛОК — скажи «давай зарядку»"
        if balance < 0:
            return "⛩ −%d мин ДОЛГ" % (abs(balance) // 60)
        return "⛩ %d мин" % mins
    if not allowed:
        return "⛩ BLOCKED — say “start exercise”"
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
                print("[radio-taiso] Баланс оператора: %s. "
                      "Заканчивай каждый свой ответ отдельной строкой: %s" % (line, line))
            else:
                print("[radio-taiso] Operator balance: %s. "
                      "End every reply with this line on its own: %s" % (line, line))
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
                      "Заканчивай ответ строкой: {}".format(
                          abs(balance) // 60, req // 60, req % 60, unlock_cli(), line)
                      + hint_ru)
            else:
                print("[radio-taiso] WORK BLOCKED: the operator's movement balance is spent "
                      "(debt {} min). Tools are unavailable until the operator does the "
                      "Radio Taiso exercise ({}:{:02d}). Your job: kindly, in your own words, "
                      "tied to the current work, invite them to move right now. The only "
                      "allowed command is Bash `{} go` — offer to start the exercise with it. "
                      "Do not perform or promise other work before the exercise. "
                      "End your reply with: {}".format(
                          abs(balance) // 60, req // 60, req % 60, unlock_cli(), line)
                      + hint_en)
        con.close()
    except Exception as e:  # fail-open всегда
        log_error("hook_prompt: %r" % e)
    sys.exit(0)


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
    if len(argv) != 2 or argv[1] not in ("go", "status"):
        return False
    prog = argv[0]
    return os.path.basename(prog) == "taiso" and (
        prog == "taiso" or os.path.isabs(prog))


def charge_activity(con, cfg, now=None):
    """Лёгкое списание активности (для PreToolUse: работа агента = работа)."""
    now = int(now or time.time())
    idle_gap = int(cfg["idle_gap_minutes"] * 60)
    con.execute("BEGIN IMMEDIATE")
    last = con.execute(
        "SELECT last_activity_utc FROM state WHERE id = 1").fetchone()[0]
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


def exercise_done(duration):
    cfg = load_config()
    grant = int(cfg["work_minutes_per_exercise"] * 60)
    con = connect()
    con.execute("BEGIN IMMEDIATE")
    balance = con.execute(
        "SELECT balance_seconds FROM state WHERE id = 1").fetchone()[0]
    new_balance = max(balance, 0) + grant  # долг сгорает, кредит начисляется
    con.execute(
        "UPDATE state SET balance_seconds = ?, video_position_sec = 0 WHERE id = 1",
        (new_balance,))
    con.execute("UPDATE sessions SET turn_allowed = 1")
    con.commit()
    add_event(con, "exercise_done",
              {"duration": int(duration), "debt_cleared": max(0, -balance)})
    con.close()


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
    balance, last = con.execute(
        "SELECT balance_seconds, last_activity_utc FROM state WHERE id = 1").fetchone()
    # живой каунтдаун: прогноз с учётом натёкшего с последней активности
    gap = int(time.time()) - last
    if 0 < gap < int(cfg["idle_gap_minutes"] * 60):
        balance -= gap
    debt_limit = int(cfg["debt_limit_minutes"] * 60)
    allowed = compute_allowed(con, cfg, balance)
    con.close()
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


def cmd_go():
    window = os.path.join(taiso_dir(), "bin", "TaisoWindow")
    if not os.path.exists(window):
        print("Окно зарядки не установлено (%s). Запусти install.sh." % window)
        sys.exit(1)
    # detached: переживает конец Bash-вызова и сессии
    subprocess.Popen([window], start_new_session=True,
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                      if t == "exercise_abort" and ts >= week_ago)
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
        print("yes" if balance <= 0 and not postponed_since_exercise(con) else "no")
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
