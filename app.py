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

import os
import random
import string
import time
from threading import Lock
from typing import Any

from flask import Flask, render_template, request
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
# routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


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
    if mode in ("easy", "hard"):
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
    if len(room["players"]) < 2:
        emit("error_msg", {"message": "Need at least 2 players to start."})
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
