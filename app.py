"""
Code Sabotage — Flask + SocketIO server.

Architecture
============
* One Flask process, eventlet greenlets for async background timers.
* `rooms` is the authoritative state for every match.  All gameplay decisions
  (who is the impostor, which problems are used, who's been voted out, build
  status, etc.) are decided here — clients only display + send intent.
* A single shared editor per room.  When any client edits the code, the diff
  size is counted; for the impostor that draws against the per-minute "sniper"
  budget.  After the budget is exhausted the server simply rejects further
  changes from that socket until the minute window resets.
* A background eventlet greenlet drives the meeting cadence (30s lock-down
  every 60s by default).  Voting works Among-Us-style: skip + named votes,
  ties => no ejection, otherwise highest tally is ejected.
"""
from __future__ import annotations

# eventlet.monkey_patch() MUST happen before any other stdlib imports so that
# socket / threading / time get patched in-place for the entire process.
import eventlet
eventlet.monkey_patch()

import base64
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import string
import time
from threading import Lock
from typing import Any
from urllib.parse import quote

# Load .env in local development; in Lambda, set env vars via zappa_settings.json.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request,
    session,
)
from flask_socketio import SocketIO, emit, join_room, leave_room

from problems import build_codebase


# --------------------------------------------------------------------------- #
# app boilerplate
# --------------------------------------------------------------------------- #
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("CODE_SABOTAGE_SECRET", "dev-secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")


# --------------------------------------------------------------------------- #
# colour palette for player tags / cursors (Apple-developer light aesthetic)
# --------------------------------------------------------------------------- #
PLAYER_COLORS = [
    "#2563EB",  # blue 600
    "#DC2626",  # red 600
    "#16A34A",  # green 600
    "#D97706",  # amber 600
    "#7C3AED",  # violet 600
    "#DB2777",  # pink 600
    "#0891B2",  # cyan 600
    "#65A30D",  # lime 600
    "#EA580C",  # orange 600
    "#475569",  # slate 600
]


# --------------------------------------------------------------------------- #
# room state
# --------------------------------------------------------------------------- #
rooms: dict[str, dict[str, Any]] = {}
rooms_lock = Lock()


def new_room_code() -> str:
    """Generate a unique 4-character A-Z room code."""
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=4))
        if code not in rooms:
            return code


def make_room(host_sid: str, host_name: str) -> dict[str, Any]:
    code = new_room_code()
    room = {
        "code": code,
        "host_sid": host_sid,
        "players": {},          # sid -> {name, color, is_impostor, alive, sniper_used, sniper_window_start}
        "settings": {
            "game_time": 180,
            "meeting_time": 30,
            "impostor_limit": 3,
            "mode": "hard",         # "easy" | "hard"
            "has_impostor": True,   # False => co-op, no meetings, no sniper
        },
        "state": "lobby",       # lobby | playing | meeting | ended
        "editor_code": "",
        "tests": [],            # hidden tests (server-side)
        "problems_summary": [], # public list of problem descriptions
        "build_status": 0,      # 0..100
        "test_results": [],     # most recent run result list
        "cursors": {},          # sid -> {line, column}
        "votes": {},            # voter_sid -> target_sid | "__skip__"
        "game_started_at": None,
        "next_meeting_at": None,
        "meeting_ends_at": None,
        "meeting_started_at": None,    # wall-clock start of CURRENT meeting
        "meeting_paused_total": 0.0,   # cumulative seconds spent in meetings
        "winner": None,         # "crew" | "impostor" | None
        "loop_alive": False,
        "chat": [],             # ring-buffer of recent chat messages
    }
    rooms[code] = room
    return room


# Chat: cap recent history sent to late-joiners and rate-limit per player.
CHAT_HISTORY_MAX = 100
CHAT_MIN_INTERVAL = 0.4   # seconds between messages from a single player
CHAT_TEXT_MAX = 300       # truncate cap


def player_payload(room: dict[str, Any]) -> list[dict[str, Any]]:
    """Public, safely-serialisable list of players for the lobby UI."""
    return [
        {
            "sid": sid,
            "name": p["name"],
            "color": p["color"],
            "is_host": sid == room["host_sid"],
            "alive": p.get("alive", True),
        }
        for sid, p in room["players"].items()
    ]


def lobby_state(room: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": room["code"],
        "players": player_payload(room),
        "settings": room["settings"],
        "host_sid": room["host_sid"],
        "state": room["state"],
    }


def effective_paused(room: dict[str, Any], now: float) -> float:
    """Total seconds that should be excluded from the game timer
    (completed meetings + current ongoing meeting)."""
    paused = room.get("meeting_paused_total", 0.0)
    if room["state"] == "meeting" and room.get("meeting_started_at"):
        paused += now - room["meeting_started_at"]
    return paused


def public_game_state(room: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    if room["game_started_at"]:
        elapsed = now - room["game_started_at"]
        time_left = max(0, int(room["settings"]["game_time"]
                               - (elapsed - effective_paused(room, now))))
    else:
        time_left = room["settings"]["game_time"]
    return {
        "code": room["code"],
        "state": room["state"],
        "players": player_payload(room),
        "build_status": room["build_status"],
        "problems": room["problems_summary"],
        "host_sid": room["host_sid"],
        "settings": room["settings"],
        "time_left": time_left,
        "next_meeting_in": max(0, int((room["next_meeting_at"] or now) - now))
            if room["next_meeting_at"] else None,
        "meeting_ends_in": max(0, int((room["meeting_ends_at"] or now) - now))
            if room["meeting_ends_at"] else None,
        "winner": room["winner"],
    }


# --------------------------------------------------------------------------- #
# diff helper used to charge the impostor's per-minute character budget
# --------------------------------------------------------------------------- #
def chars_changed(a: str, b: str) -> int:
    """Cheap edit-distance proxy: max length minus common prefix and suffix."""
    if a == b:
        return 0
    pre = 0
    m = min(len(a), len(b))
    while pre < m and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while (suf < m - pre
           and a[len(a) - 1 - suf] == b[len(b) - 1 - suf]):
        suf += 1
    return max(len(a), len(b)) - pre - suf


# --------------------------------------------------------------------------- #
# CatoCode — anonymous HTML/CSS/JS project hosting
# --------------------------------------------------------------------------- #
DATABASE_URL: str | None = os.environ.get("DATABASE_URL")
PROJECTS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "projects.json"
)
USERS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "users.json"
)
projects_lock = Lock()
users_lock = Lock()

PROJECT_MAX_BYTES = 8 * 1024 * 1024
PROJECT_ID_ALPHABET = string.ascii_lowercase + string.digits
TEXT_FILE_EXTS = {"html", "htm", "css", "js", "json"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp"}
AUDIO_EXTS = {"mp3", "wav", "ogg", "m4a"}
# MIME types for binary assets served from the images dict (images + audio).
ASSET_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
    "ico": "image/x-icon", "bmp": "image/bmp",
    "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg", "m4a": "audio/mp4",
}

# --------------------------------------------------------------------------- #
# PostgreSQL backend
# --------------------------------------------------------------------------- #
_pg_conn = None
_pg_lock = Lock()

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS catocode_projects (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    author        TEXT NOT NULL,
    files         JSONB NOT NULL DEFAULT '{}',
    images        JSONB NOT NULL DEFAULT '{}',
    ratings_count INTEGER NOT NULL DEFAULT 0,
    total_rating  INTEGER NOT NULL DEFAULT 0,
    created_at    DOUBLE PRECISION NOT NULL,
    owner         TEXT,
    published     BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at    DOUBLE PRECISION,
    type          TEXT NOT NULL DEFAULT 'html'
);
ALTER TABLE catocode_projects ADD COLUMN IF NOT EXISTS owner TEXT;
ALTER TABLE catocode_projects ADD COLUMN IF NOT EXISTS published BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE catocode_projects ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION;
ALTER TABLE catocode_projects ADD COLUMN IF NOT EXISTS type TEXT NOT NULL DEFAULT 'html';

CREATE TABLE IF NOT EXISTS catocode_users (
    username      TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    password      TEXT NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS hackathons (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    type          TEXT NOT NULL DEFAULT 'html',
    host_id       TEXT NOT NULL,
    starts_at     DOUBLE PRECISION NOT NULL,
    ends_at       DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS hackathon_participants (
    hackathon_id  TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    PRIMARY KEY (hackathon_id, user_id)
);

CREATE TABLE IF NOT EXISTS hackathon_submissions (
    hackathon_id  TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    submitted_at  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (hackathon_id, user_id)
);
"""

_PROJECT_COLS = (
    "id, title, description, author, files, images, "
    "ratings_count, total_rating, created_at, owner, published, updated_at, type"
)


def _pg_connect():
    """Return a live psycopg2 connection, reconnecting if needed."""
    global _pg_conn
    import psycopg2
    try:
        if _pg_conn is None or _pg_conn.closed:
            raise Exception("no connection")
        _pg_conn.cursor().execute("SELECT 1")
    except Exception:
        _pg_conn = psycopg2.connect(DATABASE_URL)
    return _pg_conn


def init_db() -> None:
    """Create tables if they don't exist. Called at module load (Lambda cold start)."""
    if not DATABASE_URL:
        return
    try:
        conn = _pg_connect()
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
        conn.commit()
    except Exception as exc:
        # Don't crash the whole app if DB is temporarily unreachable at startup.
        print(f"[catocode] DB init warning: {exc}", flush=True)


def _pg_row_to_dict(row: tuple) -> dict[str, Any]:
    (pid, title, desc, author, files, images, rc, tr, ca,
     owner, published, updated, ptype) = row
    return {
        "id": pid, "title": title, "description": desc or "",
        "author": author, "files": files or {}, "images": images or {},
        "ratings_count": rc or 0, "total_rating": tr or 0, "created_at": ca,
        "owner": owner, "published": bool(published) if published is not None else True,
        "updated_at": updated, "type": ptype or "html",
    }


def _pg_get_all() -> list[dict[str, Any]]:
    conn = _pg_connect()
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_PROJECT_COLS} FROM catocode_projects")
        return [_pg_row_to_dict(r) for r in cur.fetchall()]


def _pg_get_one(pid: str) -> dict[str, Any] | None:
    conn = _pg_connect()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_PROJECT_COLS} FROM catocode_projects WHERE id=%s", (pid,),
        )
        row = cur.fetchone()
    return _pg_row_to_dict(row) if row else None


def _pg_insert(p: dict[str, Any]) -> None:
    conn = _pg_connect()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catocode_projects "
            "(id, title, description, author, files, images, "
            " ratings_count, total_rating, created_at, owner, published, updated_at, type) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                p["id"], p["title"], p["description"], p["author"],
                json.dumps(p["files"]), json.dumps(p["images"]),
                0, 0, p["created_at"],
                p.get("owner"), p.get("published", True), p.get("updated_at"),
                p.get("type", "html"),
            ),
        )
    conn.commit()


def _pg_update(pid: str, fields: dict[str, Any]) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k in ("files", "images"):
            sets.append(f"{k} = %s")
            vals.append(json.dumps(v))
        else:
            sets.append(f"{k} = %s")
            vals.append(v)
    vals.append(pid)
    conn = _pg_connect()
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE catocode_projects SET {', '.join(sets)} WHERE id=%s", vals,
        )
    conn.commit()


def _pg_delete(pid: str) -> None:
    conn = _pg_connect()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM catocode_projects WHERE id=%s", (pid,))
    conn.commit()


def _pg_user_get(username: str) -> dict[str, Any] | None:
    conn = _pg_connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT username, display_name, password, created_at "
            "FROM catocode_users WHERE username=%s", (username,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"username": row[0], "display_name": row[1],
            "password": row[2], "created_at": row[3]}


def _pg_user_create(u: dict[str, Any]) -> None:
    conn = _pg_connect()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catocode_users "
            "(username, display_name, password, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (u["username"], u["display_name"], u["password"], u["created_at"]),
        )
    conn.commit()


def _pg_update_rating(
    pid: str, prior: int, rating: int
) -> dict[str, Any] | None:
    """Atomically update ratings. Returns updated row or None if not found."""
    count_delta = (
        (1 if rating > 0 else 0) - (1 if prior > 0 else 0)
    )
    total_delta = rating - prior
    conn = _pg_connect()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE catocode_projects "
            "SET ratings_count = GREATEST(0, ratings_count + %s), "
            "    total_rating  = GREATEST(0, total_rating  + %s) "
            "WHERE id = %s "
            "RETURNING ratings_count, total_rating",
            (count_delta, total_delta, pid),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    count, total = row
    return {"ratings_count": count, "total_rating": total,
            "average": (total / count) if count > 0 else 0.0}


def _pg_id_exists(pid: str) -> bool:
    conn = _pg_connect()
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM catocode_projects WHERE id=%s", (pid,))
        return cur.fetchone() is not None


# --------------------------------------------------------------------------- #
# JSON fallback backend (local development without DATABASE_URL)
# --------------------------------------------------------------------------- #
def _json_load() -> dict[str, Any]:
    if not os.path.exists(PROJECTS_PATH):
        return {}
    try:
        with open(PROJECTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _json_save(data: dict[str, Any]) -> None:
    tmp = PROJECTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, PROJECTS_PATH)


def _users_load() -> dict[str, Any]:
    if not os.path.exists(USERS_PATH):
        return {}
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _users_save(data: dict[str, Any]) -> None:
    tmp = USERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, USERS_PATH)


def _normalize_project(p: dict[str, Any]) -> dict[str, Any]:
    """Backfill account fields on legacy (pre-accounts) projects."""
    p.setdefault("owner", None)
    p.setdefault("published", True)
    p.setdefault("updated_at", p.get("created_at"))
    p.setdefault("type", "html")
    return p


# --------------------------------------------------------------------------- #
# Unified storage API (routes call these)
# --------------------------------------------------------------------------- #
def db_get_all() -> list[dict[str, Any]]:
    if DATABASE_URL:
        return _pg_get_all()
    return [_normalize_project(p) for p in _json_load().values()]


def db_get_one(pid: str) -> dict[str, Any] | None:
    if DATABASE_URL:
        return _pg_get_one(pid)
    p = _json_load().get(pid)
    return _normalize_project(p) if p else None


def db_update(pid: str, fields: dict[str, Any]) -> None:
    if DATABASE_URL:
        _pg_update(pid, fields)
        return
    with projects_lock:
        data = _json_load()
        if pid in data:
            data[pid].update(fields)
            _json_save(data)


def db_delete(pid: str) -> None:
    if DATABASE_URL:
        _pg_delete(pid)
        return
    with projects_lock:
        data = _json_load()
        if data.pop(pid, None) is not None:
            _json_save(data)


def db_user_get(username: str) -> dict[str, Any] | None:
    if DATABASE_URL:
        return _pg_user_get(username)
    return _users_load().get(username)


def db_user_create(u: dict[str, Any]) -> None:
    if DATABASE_URL:
        _pg_user_create(u)
        return
    with users_lock:
        data = _users_load()
        data[u["username"]] = u
        _users_save(data)


def db_new_id() -> str:
    while True:
        pid = "".join(random.choices(PROJECT_ID_ALPHABET, k=8))
        if DATABASE_URL:
            if not _pg_id_exists(pid):
                return pid
        else:
            if pid not in _json_load():
                return pid


def db_insert(p: dict[str, Any]) -> None:
    if DATABASE_URL:
        _pg_insert(p)
        return
    with projects_lock:
        data = _json_load()
        data[p["id"]] = p
        _json_save(data)


def db_rate(pid: str, prior: int, rating: int) -> dict[str, Any] | None:
    if DATABASE_URL:
        return _pg_update_rating(pid, prior, rating)
    with projects_lock:
        data = _json_load()
        if pid not in data:
            return None
        p = data[pid]
        if "ratings_count" not in p:
            legacy = int(p.pop("stars", 0))
            p["ratings_count"] = legacy
            p["total_rating"] = legacy * 5
        count = max(0, int(p["ratings_count"])
                    + (1 if rating > 0 else 0) - (1 if prior > 0 else 0))
        total = max(0, int(p["total_rating"]) + (rating - prior))
        p["ratings_count"] = count
        p["total_rating"] = total
        _json_save(data)
    return {"ratings_count": count, "total_rating": total,
            "average": (total / count) if count > 0 else 0.0}


# Run at module load so tables exist before the first request.
# On Lambda, this fires on each cold start.
init_db()

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def rating_summary(p: dict[str, Any]) -> dict[str, Any]:
    count = int(p.get("ratings_count", 0))
    total = int(p.get("total_rating", 0))
    if count == 0 and "stars" in p:
        legacy = int(p.get("stars", 0))
        count, total = legacy, legacy * 5
    avg = (total / count) if count > 0 else 0.0
    return {"ratings_count": count, "total_rating": total, "average": avg}


def project_card(p: dict[str, Any]) -> dict[str, Any]:
    r = rating_summary(p)
    return {
        "id": p["id"],
        "title": p["title"],
        "description": p.get("description") or "",
        "author": p["author"],
        "ratings_count": r["ratings_count"],
        "total_rating": r["total_rating"],
        "average": r["average"],
        "created_at": p.get("created_at", 0),
        "owner": p.get("owner"),
        "published": p.get("published", True),
        "type": p.get("type", "html"),
    }


# --------------------------------------------------------------------------- #
# accounts / auth
# --------------------------------------------------------------------------- #
def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(actual, expected)


def current_user() -> str | None:
    """Return the logged-in username (the canonical lowercase key) or None."""
    return session.get("user")


def current_display() -> str | None:
    return session.get("display")


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


def safe_filename(name: str) -> str | None:
    name = (name or "").strip().strip("/")
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_\-. ]+", name):
        return None
    if "." not in name:
        return None
    if len(name) > 80:
        return None
    return name


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.context_processor
def inject_user():
    """Make the logged-in user available to every template."""
    return {"current_user": current_user(), "current_display": current_display()}


@app.route("/")
def home():
    cards = [project_card(p) for p in db_get_all() if p.get("published", True)]
    cards.sort(
        key=lambda c: (
            0 if c["ratings_count"] > 0 else 1,
            -c["average"],
            -c["ratings_count"],
            -c["created_at"],
        )
    )
    return render_template("catocode_home.html", projects=cards[:10])


@app.route("/explore")
def explore():
    projects = sorted(
        (p for p in db_get_all() if p.get("published", True)),
        key=lambda p: -p.get("created_at", 0),
    )
    return render_template(
        "catocode_explore.html",
        projects=[project_card(p) for p in projects],
    )


@app.route("/new")
def new_project():
    # Creation is gated behind login: send anon users to sign in, preserving
    # the project type so they land back on the right editor.
    ptype = "game" if request.args.get("type") == "game" else "html"
    if not current_user():
        nxt = "/new?type=game" if ptype == "game" else "/new"
        return redirect("/login?next=" + quote(nxt, safe=""))
    template = "catocode_game.html" if ptype == "game" else "catocode_editor.html"
    return render_template(template, project=None, mode="new")


@app.route("/edit/<pid>")
def edit_project(pid):
    """Load an owned project (published or private draft) into the editor."""
    project = db_get_one(pid)
    if not project:
        abort(404)
    user = current_user()
    if not user or project.get("owner") != user:
        abort(403)
    editor_project = {
        "id": project["id"],
        "title": project["title"],
        "description": project.get("description", ""),
        "files": project.get("files", {}),
        "images": project.get("images", {}),
        "published": project.get("published", True),
        "type": project.get("type", "html"),
    }
    template = ("catocode_game.html"
               if project.get("type") == "game" else "catocode_editor.html")
    return render_template(template, project=editor_project, mode="edit")


@app.route("/p/<pid>")
def view_project(pid):
    project = db_get_one(pid)
    if not project:
        abort(404)
    is_owner = bool(current_user()) and project.get("owner") == current_user()
    # Private drafts are visible only to their owner.
    if not project.get("published", True) and not is_owner:
        abort(404)
    return render_template(
        "catocode_project.html",
        project=project_card(project),
        files=list(project.get("files", {}).keys()),
        images=list(project.get("images", {}).keys()),
        is_owner=is_owner,
    )


@app.route("/codesabotage")
def code_sabotage():
    return render_template("index.html")


# --------------------------------------------------------------------------- #
# auth pages + api
# --------------------------------------------------------------------------- #
@app.route("/signup")
def signup_page():
    return render_template("catocode_auth.html", page="signup")


@app.route("/login")
def login_page():
    return render_template("catocode_auth.html", page="login")


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.pop("user", None)
    session.pop("display", None)
    if request.method == "GET":
        return redirect("/")
    return jsonify({"ok": True})


@app.route("/api/signup", methods=["POST"])
def api_signup():
    payload = request.get_json(silent=True) or {}
    display = (payload.get("username") or "").strip()[:20]
    password = payload.get("password") or ""
    if not USERNAME_RE.match(display):
        return jsonify({"error": "Username must be 3–20 letters, numbers or _."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400
    key = display.lower()
    if db_user_get(key):
        return jsonify({"error": "That username is taken."}), 409
    db_user_create({
        "username": key, "display_name": display,
        "password": hash_password(password), "created_at": time.time(),
    })
    session["user"] = key
    session["display"] = display
    return jsonify({"ok": True, "redirect": "/account"})


@app.route("/api/login", methods=["POST"])
def api_login():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    user = db_user_get(name.lower())
    if not user or not verify_password(password, user["password"]):
        return jsonify({"error": "Wrong username or password."}), 401
    session["user"] = user["username"]
    session["display"] = user["display_name"]
    return jsonify({"ok": True, "redirect": "/account"})


@app.route("/account")
def account_page():
    user = current_user()
    if not user:
        return redirect("/login")
    mine = [p for p in db_get_all() if p.get("owner") == user]
    mine.sort(key=lambda p: -(p.get("updated_at") or p.get("created_at", 0)))
    cards = []
    for p in mine:
        c = project_card(p)
        c["file_count"] = len(p.get("files", {}))
        cards.append(c)
    return render_template(
        "catocode_account.html",
        projects=cards,
        published=[c for c in cards if c["published"]],
        drafts=[c for c in cards if not c["published"]],
    )


# --------------------------------------------------------------------------- #
# project create / update / delete / remix
# --------------------------------------------------------------------------- #
def _clean_project_payload(payload: dict[str, Any]):
    """Validate & normalise an incoming project body.
    Returns (data_dict, None) on success or (None, (error, status))."""
    title       = (payload.get("title")       or "").strip()[:80]
    description = (payload.get("description") or "").strip()[:500]
    files  = payload.get("files")  or {}
    images = payload.get("images") or {}

    if not title:
        return None, ("Title is required.", 400)
    if not isinstance(files, dict) or not isinstance(images, dict):
        return None, ("Invalid project payload.", 400)
    if "index.html" not in files:
        return None, ("Project must have an index.html.", 400)

    clean_files: dict[str, str] = {}
    for name, content in files.items():
        safe = safe_filename(name)
        if not safe:
            return None, (f"Invalid filename: {name}", 400)
        ext = safe.rsplit(".", 1)[-1].lower()
        if ext not in TEXT_FILE_EXTS:
            return None, (f"Unsupported file type: {safe}", 400)
        if not isinstance(content, str):
            return None, (f"Bad content for {safe}", 400)
        clean_files[safe] = content

    clean_images: dict[str, str] = {}
    for name, data_url in images.items():
        safe = safe_filename(name)
        if not safe:
            return None, (f"Invalid asset name: {name}", 400)
        ext = safe.rsplit(".", 1)[-1].lower()
        if ext not in IMAGE_EXTS and ext not in AUDIO_EXTS:
            return None, (f"Unsupported asset type: {safe}", 400)
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            return None, (f"Bad asset data for {safe}", 400)
        clean_images[safe] = data_url

    size = sum(len(v) for v in clean_files.values()) + sum(
        len(v) for v in clean_images.values()
    )
    if size > PROJECT_MAX_BYTES:
        return None, ("Project exceeds 8MB.", 413)

    return {"title": title, "description": description,
            "files": clean_files, "images": clean_images}, None


@app.route("/api/projects", methods=["POST"])
def api_publish():
    user = current_user()
    if not user:
        return jsonify({"error": "Please sign in to publish.", "login": True}), 401
    payload = request.get_json(silent=True) or {}
    data, err = _clean_project_payload(payload)
    if err:
        return jsonify({"error": err[0]}), err[1]

    publish = payload.get("published", True) is not False
    ptype = payload.get("type") if payload.get("type") in ("html", "game") else "html"
    now = time.time()
    pid = db_new_id()
    db_insert({
        "id": pid, "title": data["title"], "description": data["description"],
        "author": current_display() or "Anonymous",
        "files": data["files"], "images": data["images"],
        "ratings_count": 0, "total_rating": 0, "created_at": now,
        "owner": user, "published": publish, "updated_at": now,
        "type": ptype,
    })
    return jsonify({"id": pid, "url": f"/p/{pid}", "edit_url": f"/edit/{pid}"})


@app.route("/api/projects/<pid>", methods=["PATCH", "PUT"])
def api_update(pid):
    user = current_user()
    if not user:
        return jsonify({"error": "Please sign in.", "login": True}), 401
    project = db_get_one(pid)
    if not project:
        return jsonify({"error": "Not found"}), 404
    if project.get("owner") != user:
        return jsonify({"error": "You don't own this project."}), 403

    payload = request.get_json(silent=True) or {}
    data, err = _clean_project_payload(payload)
    if err:
        return jsonify({"error": err[0]}), err[1]

    fields = {
        "title": data["title"], "description": data["description"],
        "files": data["files"], "images": data["images"],
        "updated_at": time.time(),
    }
    if "published" in payload and isinstance(payload["published"], bool):
        fields["published"] = payload["published"]
    db_update(pid, fields)
    published = fields.get("published", project.get("published", True))
    return jsonify({"id": pid, "url": f"/p/{pid}", "published": published})


@app.route("/api/projects/<pid>", methods=["DELETE"])
def api_delete(pid):
    user = current_user()
    if not user:
        return jsonify({"error": "Please sign in.", "login": True}), 401
    project = db_get_one(pid)
    if not project:
        return jsonify({"error": "Not found"}), 404
    if project.get("owner") != user:
        return jsonify({"error": "You don't own this project."}), 403
    db_delete(pid)
    return jsonify({"ok": True})


@app.route("/api/projects/mine", methods=["GET"])
def api_projects_mine():
    """Return the logged-in user's projects (published + drafts) as cards."""
    user = current_user()
    if not user:
        return jsonify({"error": "Not signed in.", "login": True}), 401
    mine = [p for p in db_get_all() if p.get("owner") == user]
    mine.sort(key=lambda p: -(p.get("updated_at") or p.get("created_at", 0)))
    return jsonify([project_card(p) for p in mine])


@app.route("/api/projects/<pid>/remix", methods=["POST"])
def api_remix(pid):
    user = current_user()
    if not user:
        return jsonify({"error": "Please sign in to remix.", "login": True}), 401
    project = db_get_one(pid)
    if not project:
        return jsonify({"error": "Not found"}), 404
    if not project.get("published", True) and project.get("owner") != user:
        return jsonify({"error": "Not found"}), 404

    now = time.time()
    new_pid = db_new_id()
    title = (project.get("title") or "Untitled")[:70] + " (remix)"
    db_insert({
        "id": new_pid, "title": title[:80],
        "description": project.get("description", ""),
        "author": current_display() or "Anonymous",
        "files": dict(project.get("files", {})),
        "images": dict(project.get("images", {})),
        "ratings_count": 0, "total_rating": 0, "created_at": now,
        "owner": user, "published": False, "updated_at": now,
        "type": project.get("type", "html"),
    })
    return jsonify({"id": new_pid, "edit_url": f"/edit/{new_pid}"})


@app.route("/api/projects/<pid>/rate", methods=["POST"])
def api_rate(pid):
    if not current_user():
        return jsonify({"error": "Please sign in to star projects.",
                        "login": True}), 401
    payload = request.get_json(silent=True) or {}
    try:
        prior  = int(payload.get("prior",  0))
        rating = int(payload.get("rating", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid rating"}), 400
    if not (0 <= prior <= 5) or not (0 <= rating <= 5):
        return jsonify({"error": "Rating must be 0-5"}), 400

    result = db_rate(pid, prior, rating)
    if result is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(result)


@app.route("/r/<pid>/")
@app.route("/r/<pid>/<path:filename>")
def project_asset(pid, filename: str = "index.html"):
    safe = safe_filename(filename)
    if not safe:
        abort(404)
    project = db_get_one(pid)
    if not project:
        abort(404)

    ext = safe.rsplit(".", 1)[-1].lower()

    if safe in project.get("files", {}):
        mime = {
            "html": "text/html", "htm": "text/html",
            "css": "text/css", "js": "application/javascript",
        }.get(ext, "text/plain")
        return Response(project["files"][safe], mimetype=mime)

    if safe in project.get("images", {}):
        data_url = project["images"][safe]
        m = re.match(r"data:([^;]+);base64,(.+)", data_url, re.DOTALL)
        if not m:
            abort(404)
        try:
            body = base64.b64decode(m.group(2))
        except Exception:
            abort(404)
        # Prefer the extension-based MIME (covers audio) and fall back to the
        # type declared in the data: URL.
        return Response(body, mimetype=ASSET_MIME.get(ext) or m.group(1))

    abort(404)


# --------------------------------------------------------------------------- #
# Hackathon storage — JSON fallback (no DATABASE_URL) + PostgreSQL
# --------------------------------------------------------------------------- #
HACKATHONS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "hackathons.json"
)
hackathons_lock = Lock()

_HACK_EMPTY: dict[str, Any] = {"hackathons": {}, "participants": {}, "submissions": {}}


def _hack_load() -> dict[str, Any]:
    if not os.path.exists(HACKATHONS_PATH):
        return {k: dict(v) for k, v in _HACK_EMPTY.items()}
    try:
        with open(HACKATHONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {k: dict(v) for k, v in _HACK_EMPTY.items()}


def _hack_save(data: dict[str, Any]) -> None:
    tmp = HACKATHONS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, HACKATHONS_PATH)


def hackathon_get_all() -> list[dict[str, Any]]:
    if DATABASE_URL:
        conn = _pg_connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, description, type, host_id, starts_at, ends_at "
                "FROM hackathons ORDER BY starts_at DESC"
            )
            rows = cur.fetchall()
        return [
            {"id": r[0], "title": r[1], "description": r[2] or "",
             "type": r[3], "host_id": r[4], "starts_at": r[5], "ends_at": r[6]}
            for r in rows
        ]
    data = _hack_load()
    return sorted(data["hackathons"].values(), key=lambda h: -h["starts_at"])


def hackathon_get_one(hid: str) -> dict[str, Any] | None:
    if DATABASE_URL:
        conn = _pg_connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, description, type, host_id, starts_at, ends_at "
                "FROM hackathons WHERE id=%s", (hid,)
            )
            r = cur.fetchone()
        if not r:
            return None
        return {"id": r[0], "title": r[1], "description": r[2] or "",
                "type": r[3], "host_id": r[4], "starts_at": r[5], "ends_at": r[6]}
    return _hack_load()["hackathons"].get(hid)


def hackathon_insert(h: dict[str, Any]) -> None:
    if DATABASE_URL:
        conn = _pg_connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hackathons (id, title, description, type, host_id, starts_at, ends_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (h["id"], h["title"], h["description"], h["type"],
                 h["host_id"], h["starts_at"], h["ends_at"])
            )
        conn.commit()
        return
    with hackathons_lock:
        data = _hack_load()
        data["hackathons"][h["id"]] = h
        _hack_save(data)


def hackathon_participants(hid: str) -> list[str]:
    if DATABASE_URL:
        conn = _pg_connect()
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM hackathon_participants WHERE hackathon_id=%s", (hid,))
            return [r[0] for r in cur.fetchall()]
    return _hack_load()["participants"].get(hid, [])


def hackathon_join(hid: str, user: str) -> None:
    if DATABASE_URL:
        conn = _pg_connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hackathon_participants (hackathon_id, user_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING", (hid, user)
            )
        conn.commit()
        return
    with hackathons_lock:
        data = _hack_load()
        data["participants"].setdefault(hid, [])
        if user not in data["participants"][hid]:
            data["participants"][hid].append(user)
        _hack_save(data)


def hackathon_submissions(hid: str) -> list[dict[str, Any]]:
    if DATABASE_URL:
        conn = _pg_connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, project_id, submitted_at FROM hackathon_submissions "
                "WHERE hackathon_id=%s ORDER BY submitted_at", (hid,)
            )
            return [{"user_id": r[0], "project_id": r[1], "submitted_at": r[2]}
                    for r in cur.fetchall()]
    subs = _hack_load()["submissions"].get(hid, {})
    return sorted(subs.values(), key=lambda s: s["submitted_at"])


def hackathon_submit(hid: str, user: str, project_id: str, now: float) -> None:
    if DATABASE_URL:
        conn = _pg_connect()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hackathon_submissions (hackathon_id, user_id, project_id, submitted_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (hackathon_id, user_id) "
                "DO UPDATE SET project_id=EXCLUDED.project_id, submitted_at=EXCLUDED.submitted_at",
                (hid, user, project_id, now)
            )
        conn.commit()
        return
    with hackathons_lock:
        data = _hack_load()
        data["submissions"].setdefault(hid, {})
        data["submissions"][hid][user] = {"user_id": user, "project_id": project_id, "submitted_at": now}
        _hack_save(data)


def hackathon_is_participant(hid: str, user: str) -> bool:
    return user in hackathon_participants(hid)


def hackathon_submission_for(hid: str, user: str) -> dict[str, Any] | None:
    for s in hackathon_submissions(hid):
        if s["user_id"] == user:
            return s
    return None


# --------------------------------------------------------------------------- #
# Hackathon routes
# --------------------------------------------------------------------------- #
@app.route("/hackathons")
def hackathons_page():
    return render_template("hackathons.html", active="hackathons")


@app.route("/hackathons/<hid>")
def hackathon_detail_page(hid):
    h = hackathon_get_one(hid)
    if not h:
        abort(404)
    return render_template("hackathon_detail.html", hid=hid, active="hackathons")


@app.route("/api/hackathons", methods=["GET"])
def api_hackathons_list():
    now = time.time()
    all_h = hackathon_get_all()
    result = []
    for h in all_h:
        parts = hackathon_participants(h["id"])
        user = current_user()
        is_joined = user in parts if user else False
        sub = hackathon_submission_for(h["id"], user) if user else None
        host_user = db_user_get(h["host_id"])
        result.append({
            **h,
            "participant_count": len(parts),
            "ended": now >= h["ends_at"],
            "time_remaining": max(0, int(h["ends_at"] - now)),
            "is_joined": is_joined,
            "submitted_project": sub["project_id"] if sub else None,
            "host_display": (host_user["display_name"] if host_user else h["host_id"]),
        })
    return jsonify(result)


@app.route("/api/hackathons", methods=["POST"])
def api_hackathons_create():
    user = current_user()
    if not user:
        return jsonify({"error": "Sign in to host a hackathon.", "login": True}), 401
    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()[:80]
    description = (payload.get("description") or "").strip()[:500]
    htype = payload.get("type") if payload.get("type") in ("html", "game") else "html"
    duration = int(payload.get("duration") or 60)
    duration = max(5, min(10080, duration))
    if not title:
        return jsonify({"error": "Title is required."}), 400
    now = time.time()
    hid = "".join(random.choices(PROJECT_ID_ALPHABET, k=8))
    h = {"id": hid, "title": title, "description": description, "type": htype,
         "host_id": user, "starts_at": now, "ends_at": now + duration * 60}
    hackathon_insert(h)
    hackathon_join(hid, user)
    return jsonify({"id": hid})


@app.route("/api/hackathons/<hid>", methods=["GET"])
def api_hackathon_get(hid):
    h = hackathon_get_one(hid)
    if not h:
        return jsonify({"error": "Not found"}), 404
    now = time.time()
    ended = now >= h["ends_at"]
    parts = hackathon_participants(hid)
    subs_raw = hackathon_submissions(hid)
    user = current_user()
    host_user = db_user_get(h["host_id"])
    subs_out = []
    if ended:
        for s in subs_raw:
            p = db_get_one(s["project_id"])
            subs_out.append({
                "user_id": s["user_id"],
                "project_id": s["project_id"],
                "submitted_at": s["submitted_at"],
                "project_title": p["title"] if p else "Unknown",
                "project_url": f"/p/{s['project_id']}" if p else None,
            })
    return jsonify({
        **h,
        "participant_count": len(parts),
        "time_remaining": max(0, int(h["ends_at"] - now)),
        "ended": ended,
        "is_joined": (user in parts) if user else False,
        "submitted_project": hackathon_submission_for(hid, user)["project_id"] if (user and hackathon_submission_for(hid, user)) else None,
        "submissions": subs_out,
        "submission_count": len(subs_raw),
        "host_display": (host_user["display_name"] if host_user else h["host_id"]),
    })


@app.route("/api/hackathons/<hid>/join", methods=["POST"])
def api_hackathon_join(hid):
    user = current_user()
    if not user:
        return jsonify({"error": "Sign in to join.", "login": True}), 401
    h = hackathon_get_one(hid)
    if not h:
        return jsonify({"error": "Not found"}), 404
    if time.time() >= h["ends_at"]:
        return jsonify({"error": "Hackathon has ended."}), 400
    hackathon_join(hid, user)
    return jsonify({"ok": True})


@app.route("/api/hackathons/<hid>/submit", methods=["POST"])
def api_hackathon_submit(hid):
    user = current_user()
    if not user:
        return jsonify({"error": "Sign in to submit.", "login": True}), 401
    h = hackathon_get_one(hid)
    if not h:
        return jsonify({"error": "Not found"}), 404
    now = time.time()
    if now >= h["ends_at"]:
        return jsonify({"error": "Hackathon has ended."}), 400
    payload = request.get_json(silent=True) or {}
    project_id = payload.get("project_id")
    if not project_id:
        return jsonify({"error": "project_id is required."}), 400
    project = db_get_one(project_id)
    if not project or project.get("owner") != user or not project.get("published", True):
        return jsonify({"error": "Invalid or unpublished project."}), 400
    if project.get("type", "html") != h["type"]:
        return jsonify({"error": f"Only {h['type']} projects can be submitted here."}), 400
    hackathon_submit(hid, user, project_id, now)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# socket helpers
# --------------------------------------------------------------------------- #
def broadcast_lobby(room: dict[str, Any]) -> None:
    socketio.emit("lobby_update", lobby_state(room), to=room["code"])


def broadcast_game(room: dict[str, Any]) -> None:
    socketio.emit("game_state", public_game_state(room), to=room["code"])


def find_room_by_sid(sid: str) -> dict[str, Any] | None:
    for room in rooms.values():
        if sid in room["players"]:
            return room
    return None


def assign_color(room: dict[str, Any]) -> str:
    used = {p["color"] for p in room["players"].values()}
    for c in PLAYER_COLORS:
        if c not in used:
            return c
    # Fallback if >10 players
    return random.choice(PLAYER_COLORS)


# --------------------------------------------------------------------------- #
# socket handlers — room creation / joining
# --------------------------------------------------------------------------- #
@socketio.on("connect")
def on_connect():
    emit("connected", {"sid": request.sid})


@socketio.on("create_room")
def on_create_room(data):
    name = (data or {}).get("name", "").strip()[:18] or "Player"
    with rooms_lock:
        room = make_room(request.sid, name)
        room["players"][request.sid] = {
            "name": name,
            "color": assign_color(room),
            "is_impostor": False,
            "alive": True,
            "sniper_used": 0,
            "sniper_window_start": time.time(),
        }
    join_room(room["code"])
    emit("room_joined", {"code": room["code"], "you": request.sid})
    emit("chat_history", {"messages": room["chat"]})
    broadcast_lobby(room)


@socketio.on("join_room")
def on_join_room(data):
    code = (data or {}).get("code", "").upper().strip()
    name = (data or {}).get("name", "").strip()[:18] or "Player"
    room = rooms.get(code)
    if not room:
        emit("error_msg", {"message": f"Room '{code}' not found."})
        return
    if room["state"] != "lobby":
        emit("error_msg", {"message": "That room is already in-game."})
        return
    if len(room["players"]) >= 10:
        emit("error_msg", {"message": "Room is full (10 max)."})
        return
    with rooms_lock:
        room["players"][request.sid] = {
            "name": name,
            "color": assign_color(room),
            "is_impostor": False,
            "alive": True,
            "sniper_used": 0,
            "sniper_window_start": time.time(),
        }
    join_room(code)
    emit("room_joined", {"code": code, "you": request.sid})
    emit("chat_history", {"messages": room["chat"]})
    broadcast_lobby(room)


@socketio.on("update_settings")
def on_update_settings(data):
    room = find_room_by_sid(request.sid)
    if not room or room["host_sid"] != request.sid or room["state"] != "lobby":
        return
    s = room["settings"]
    for key, lo, hi in [("game_time", 60, 600),
                        ("meeting_time", 10, 120),
                        ("impostor_limit", 1, 20)]:
        v = data.get(key)
        if isinstance(v, (int, float)):
            s[key] = int(max(lo, min(hi, v)))
    mode = data.get("mode")
    if mode in ("easy", "medium", "hard"):
        s["mode"] = mode
    if "has_impostor" in data and isinstance(data["has_impostor"], bool):
        s["has_impostor"] = data["has_impostor"]
    broadcast_lobby(room)


@socketio.on("chat_msg")
def on_chat_msg(data):
    room = find_room_by_sid(request.sid)
    if not room:
        return
    player = room["players"].get(request.sid)
    if not player:
        return
    raw = (data or {}).get("text")
    if not isinstance(raw, str):
        return
    text = raw.strip()[:CHAT_TEXT_MAX]
    if not text:
        return
    now = time.time()
    if now - player.get("last_chat_at", 0.0) < CHAT_MIN_INTERVAL:
        return
    player["last_chat_at"] = now
    msg = {
        "sid": request.sid,
        "name": player["name"],
        "color": player["color"],
        "text": text,
        "ts": int(now * 1000),
    }
    room["chat"].append(msg)
    if len(room["chat"]) > CHAT_HISTORY_MAX:
        del room["chat"][: len(room["chat"]) - CHAT_HISTORY_MAX]
    socketio.emit("chat_msg", msg, to=room["code"])


@socketio.on("leave_room")
def on_leave_room():
    cleanup_disconnect(request.sid)


@socketio.on("disconnect")
def on_disconnect():
    cleanup_disconnect(request.sid)


def cleanup_disconnect(sid: str) -> None:
    room = find_room_by_sid(sid)
    if not room:
        return
    was_host = room["host_sid"] == sid
    room["players"].pop(sid, None)
    room["cursors"].pop(sid, None)
    room["votes"].pop(sid, None)
    leave_room(room["code"], sid=sid)

    if not room["players"]:
        room["loop_alive"] = False
        rooms.pop(room["code"], None)
        return

    if was_host:
        # promote first remaining player
        room["host_sid"] = next(iter(room["players"]))

    if room["state"] == "lobby":
        broadcast_lobby(room)
    else:
        broadcast_game(room)
        # if impostor disconnected (and the match has one), crew wins immediately
        if room["settings"].get("has_impostor", True) and not any(
                p["is_impostor"] and p["alive"]
                for p in room["players"].values()):
            end_game(room, "crew", reason="Impostor disconnected.")


# --------------------------------------------------------------------------- #
# starting a match
# --------------------------------------------------------------------------- #
@socketio.on("start_game")
def on_start_game():
    room = find_room_by_sid(request.sid)
    if not room or room["host_sid"] != request.sid or room["state"] != "lobby":
        return
    if len(room["players"]) < 3:
        emit("error_msg", {"message": "Need at least 3 players to start."})
        return

    # pick impostor (or skip entirely in co-op mode)
    sids = list(room["players"].keys())
    has_impostor = bool(room["settings"].get("has_impostor", True))
    impostor = random.choice(sids) if has_impostor else None
    for sid, p in room["players"].items():
        p["is_impostor"] = (impostor is not None and sid == impostor)
        p["alive"] = True
        p["sniper_used"] = 0
        p["sniper_window_start"] = time.time()

    # generate procedural codebase
    bundle = build_codebase(mode=room["settings"].get("mode", "hard"))
    room["editor_code"] = bundle["editor_code"]
    room["tests"] = bundle["tests"]
    room["problems_summary"] = bundle["problems"]
    room["build_status"] = 0
    room["test_results"] = []

    now = time.time()
    room["state"] = "playing"
    room["game_started_at"] = now
    room["next_meeting_at"] = (now + 60) if has_impostor else None
    room["meeting_ends_at"] = None
    room["meeting_started_at"] = None
    room["meeting_paused_total"] = 0.0
    room["winner"] = None
    room["votes"] = {}
    room["cursors"] = {}

    # announce impostor role privately, everyone else gets "crew"
    for sid, p in room["players"].items():
        socketio.emit(
            "role_assigned",
            {
                "role": "impostor" if p["is_impostor"] else "crew",
                "impostor_limit": room["settings"]["impostor_limit"],
            },
            to=sid,
        )

    # send the editor contents
    socketio.emit("editor_init",
                  {"code": room["editor_code"]},
                  to=room["code"])

    broadcast_game(room)
    if not room["loop_alive"]:
        room["loop_alive"] = True
        socketio.start_background_task(room_loop, room["code"])


# --------------------------------------------------------------------------- #
# the main per-room game loop (eventlet background task)
# --------------------------------------------------------------------------- #
def room_loop(code: str) -> None:
    """Drive meeting cadence, time-limit, and sniper-window resets."""
    while True:
        eventlet.sleep(1)
        room = rooms.get(code)
        if not room or not room.get("loop_alive"):
            return
        if room["state"] == "ended":
            room["loop_alive"] = False
            return
        now = time.time()

        # reset sniper character windows every 60s
        for p in room["players"].values():
            if now - p.get("sniper_window_start", 0) >= 60:
                p["sniper_used"] = 0
                p["sniper_window_start"] = now

        # check for game-time expiry (impostor wins on timeout) — paused
        # seconds spent in meetings DO NOT count toward the game timer.
        if (room["state"] in ("playing", "meeting")
                and room["game_started_at"]):
            elapsed = now - room["game_started_at"]
            if elapsed - effective_paused(room, now) >= room["settings"]["game_time"]:
                if room["settings"].get("has_impostor", True):
                    end_game(room, "impostor", reason="Time ran out.")
                else:
                    end_game(room, "failed",
                             reason="Time ran out — build incomplete.")
                continue

        # state transitions
        if room["state"] == "playing":
            if room["next_meeting_at"] and now >= room["next_meeting_at"]:
                start_meeting(room)
        elif room["state"] == "meeting":
            if room["meeting_ends_at"] and now >= room["meeting_ends_at"]:
                conclude_meeting(room)

        broadcast_game(room)


# --------------------------------------------------------------------------- #
# meetings
# --------------------------------------------------------------------------- #
def start_meeting(room: dict[str, Any]) -> None:
    room["state"] = "meeting"
    now = time.time()
    room["meeting_ends_at"] = now + room["settings"]["meeting_time"]
    room["meeting_started_at"] = now
    room["next_meeting_at"] = None
    room["votes"] = {}
    socketio.emit(
        "meeting_started",
        {"duration": room["settings"]["meeting_time"]},
        to=room["code"],
    )
    broadcast_game(room)


def conclude_meeting(room: dict[str, Any]) -> None:
    """Tally votes Among-Us-style: highest count is ejected; ties => skip."""
    tally: dict[str, int] = {}
    for target in room["votes"].values():
        tally[target] = tally.get(target, 0) + 1

    ejected_sid = None
    kind = "no_votes"
    if tally:
        max_count = max(tally.values())
        leaders = [sid for sid, c in tally.items() if c == max_count]
        if len(leaders) > 1:
            kind = "tied"
        elif leaders[0] == "__skip__":
            kind = "skipped"
        else:
            ejected_sid = leaders[0]
            kind = "ejected"

    ejected_payload = None
    if ejected_sid and ejected_sid in room["players"]:
        ejected = room["players"][ejected_sid]
        ejected["alive"] = False
        ejected_payload = {
            "sid": ejected_sid,
            "name": ejected["name"],
            "color": ejected.get("color"),
            "was_impostor": ejected["is_impostor"],
        }

    socketio.emit(
        "meeting_ended",
        {"tally": tally, "ejected": ejected_payload, "kind": kind},
        to=room["code"],
    )

    # bank the time spent in this meeting so it doesn't count toward game_time
    if room.get("meeting_started_at"):
        room["meeting_paused_total"] = (
            room.get("meeting_paused_total", 0.0)
            + (time.time() - room["meeting_started_at"])
        )
    room["meeting_started_at"] = None

    # reset meeting / voting state
    room["votes"] = {}
    room["meeting_ends_at"] = None

    # win check: did the crew vote out the impostor?
    if ejected_payload and ejected_payload["was_impostor"]:
        end_game(room, "crew", reason="The impostor was ejected.")
        return

    room["state"] = "playing"
    room["next_meeting_at"] = time.time() + 60
    broadcast_game(room)


@socketio.on("vote")
def on_vote(data):
    room = find_room_by_sid(request.sid)
    if not room or room["state"] != "meeting":
        return
    voter = room["players"].get(request.sid)
    if not voter or not voter.get("alive"):
        return
    target = (data or {}).get("target")
    if target == "__skip__" or (target in room["players"]
                                and room["players"][target]["alive"]):
        room["votes"][request.sid] = target
        socketio.emit(
            "vote_update",
            {"voter": request.sid, "target": target,
             "totals": _vote_totals(room)},
            to=room["code"],
        )
        # all alive players have voted? end early
        alive_voters = sum(1 for p in room["players"].values()
                           if p["alive"])
        if len(room["votes"]) >= alive_voters:
            conclude_meeting(room)


def _vote_totals(room):
    totals = {}
    for t in room["votes"].values():
        totals[t] = totals.get(t, 0) + 1
    return totals


# --------------------------------------------------------------------------- #
# shared editor + sniper enforcement
# --------------------------------------------------------------------------- #
@socketio.on("code_change")
def on_code_change(data):
    """Authoritative code update.  Charges impostor sniper budget; broadcasts
    to other clients."""
    room = find_room_by_sid(request.sid)
    if not room or room["state"] != "playing":
        return
    player = room["players"].get(request.sid)
    if not player or not player["alive"]:
        return

    new_code = (data or {}).get("code")
    if not isinstance(new_code, str):
        return

    diff = chars_changed(room["editor_code"], new_code)
    if diff == 0:
        return

    if player["is_impostor"]:
        # rolling 60-second window
        now = time.time()
        if now - player["sniper_window_start"] >= 60:
            player["sniper_used"] = 0
            player["sniper_window_start"] = now
        budget = room["settings"]["impostor_limit"]
        if player["sniper_used"] + diff > budget:
            # reject — push the canonical code back to this client to revert
            socketio.emit(
                "code_rejected",
                {
                    "code": room["editor_code"],
                    "reason": (
                        f"Sniper limit reached "
                        f"({player['sniper_used']}/{budget} used this minute)."
                    ),
                    "remaining": max(0, budget - player["sniper_used"]),
                },
                to=request.sid,
            )
            return
        player["sniper_used"] += diff
        socketio.emit(
            "sniper_status",
            {"used": player["sniper_used"], "limit": budget,
             "window_resets_in": int(60 - (time.time() - player["sniper_window_start"]))},
            to=request.sid,
        )

    room["editor_code"] = new_code
    socketio.emit(
        "code_update",
        {"code": new_code, "from": request.sid},
        to=room["code"],
        include_self=False,
    )


@socketio.on("cursor_move")
def on_cursor_move(data):
    room = find_room_by_sid(request.sid)
    if not room or room["state"] not in ("playing",):
        return
    pos = data or {}
    line = pos.get("line")
    col = pos.get("column")
    if not isinstance(line, int) or not isinstance(col, int):
        return
    room["cursors"][request.sid] = {"line": line, "column": col}
    socketio.emit(
        "cursor_update",
        {"sid": request.sid, "line": line, "column": col},
        to=room["code"],
        include_self=False,
    )


# --------------------------------------------------------------------------- #
# Run button — authoritative test runner.
# JS execution still happens client-side (browser sandbox via new Function),
# but the SERVER decides what counts: it sends the test cases, the client
# returns each test's actual return value, and the server compares.
# --------------------------------------------------------------------------- #
@socketio.on("run_request")
def on_run_request():
    room = find_room_by_sid(request.sid)
    if not room or room["state"] != "playing":
        return
    # send the hidden tests *to the requesting client only* so they can run
    emit("run_tests", {
        "code": room["editor_code"],
        "tests": [
            {"id": i, "fn_name": t["fn_name"], "args": t["args"], "desc": t["desc"]}
            for i, t in enumerate(room["tests"])
        ],
    })


@socketio.on("run_results")
def on_run_results(data):
    """Client returns the actual values it observed running the editor code.
    Server compares case-by-case, then ROLLS UP into a single pass/fail per
    problem (10 problems => build status in 10% increments, matching spec)."""
    room = find_room_by_sid(request.sid)
    if not room or room["state"] != "playing":
        return
    actuals = (data or {}).get("results") or []
    expected_tests = room["tests"]

    # case-level evaluation
    by_fn: dict[str, list[dict[str, Any]]] = {}
    for entry in actuals:
        idx = entry.get("id")
        if not isinstance(idx, int) or not (0 <= idx < len(expected_tests)):
            continue
        t = expected_tests[idx]
        if entry.get("error"):
            case = {"ok": False, "desc": t["desc"],
                    "expected": t["expected"],
                    "actual_display": f"ERROR: {entry['error']}"}
        else:
            actual = entry.get("actual")
            case = {"ok": _values_equal(actual, t["expected"]),
                    "desc": t["desc"], "expected": t["expected"],
                    "actual_display": repr(actual)}
        by_fn.setdefault(t["fn_name"], []).append(case)

    # roll up into problem-level results (one per problem, in original order)
    problem_results = []
    passed = 0
    for i, prob in enumerate(room["problems_summary"], 1):
        cases = by_fn.get(prob["fn_name"], [])
        all_ok = bool(cases) and all(c["ok"] for c in cases)
        first_fail = next((c for c in cases if not c["ok"]), None)
        if all_ok:
            passed += 1
        problem_results.append({
            "index": i,
            "fn_name": prob["fn_name"],
            "desc": prob["desc"],
            "ok": all_ok,
            "case_count": len(cases),
            "first_fail": first_fail,  # {expected, actual_display, desc} or None
        })

    total = len(room["problems_summary"]) or 1
    room["build_status"] = int(round(passed / total * 100))
    room["test_results"] = problem_results

    socketio.emit(
        "run_complete",
        {
            "results": problem_results,
            "passed": passed,
            "total": total,
            "build_status": room["build_status"],
            "by": request.sid,
        },
        to=room["code"],
    )

    if room["build_status"] >= 100:
        end_game(room, "crew", reason="Build reached 100% — crew wins!")


def _values_equal(a, b):
    """Tolerant equality (lists vs tuples, floats with epsilon)."""
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return False
    if isinstance(b, list) and isinstance(a, list):
        return len(a) == len(b) and all(_values_equal(x, y) for x, y in zip(a, b))
    return a == b


# --------------------------------------------------------------------------- #
# end-game
# --------------------------------------------------------------------------- #
def end_game(room: dict[str, Any], winner: str, reason: str = "") -> None:
    room["state"] = "ended"
    room["winner"] = winner
    impostor_sid = next((sid for sid, p in room["players"].items()
                         if p["is_impostor"]), None)
    socketio.emit(
        "game_ended",
        {
            "winner": winner,
            "reason": reason,
            "impostor": {
                "sid": impostor_sid,
                "name": room["players"][impostor_sid]["name"]
                        if impostor_sid else None,
            } if impostor_sid else None,
        },
        to=room["code"],
    )
    broadcast_game(room)


@socketio.on("return_to_lobby")
def on_return_to_lobby():
    """After a finished match the host can recycle the room back to lobby."""
    room = find_room_by_sid(request.sid)
    if not room or room["host_sid"] != request.sid or room["state"] != "ended":
        return
    room["state"] = "lobby"
    room["editor_code"] = ""
    room["tests"] = []
    room["problems_summary"] = []
    room["build_status"] = 0
    room["test_results"] = []
    room["cursors"] = {}
    room["votes"] = {}
    room["winner"] = None
    room["game_started_at"] = None
    room["next_meeting_at"] = None
    room["meeting_ends_at"] = None
    room["meeting_started_at"] = None
    room["meeting_paused_total"] = 0.0
    for p in room["players"].values():
        p["is_impostor"] = False
        p["alive"] = True
        p["sniper_used"] = 0
        p["sniper_window_start"] = time.time()
    broadcast_lobby(room)


# --------------------------------------------------------------------------- #
# entry-point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
