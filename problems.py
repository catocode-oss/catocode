"""
Procedural Bug Engine — Problem Library.

Each generator is a callable ``gen(rng, used) -> dict`` returning:
    {
      'fn_name':  unique JS identifier injected into the codebase,
      'desc':     short description shown to players,
      'code':     a JS function definition (with a planted bug) the players see,
      'tests':    list of {args, expected, desc} — kept on the server, sent to
                  the client only at run-time so the test source isn't visible
                  in the editor itself.
    }

There are three difficulty tiers, each with **200 unique tasks** (600 total):

    * EASY    — single scalar, one obvious wrong operator/constant, no loops.
    * MEDIUM  — one array / string pass: filter, map, count, sum-with-condition.
    * HARD    — trickier: reduce, sort, regex, off-by-one ranges, classic bugs.

The library is generated from compact template factories parametrised over
constants, so every entry has a genuinely distinct description + planted bug.
The server randomly draws ``count`` problems per match, randomises the function
names, and concatenates the bodies into a single "codebase".
"""

import json
import random
import re
import string


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
NAME_POOL = [
    "alpha", "bravo", "delta", "echo", "foxtrot", "gamma", "hotel",
    "india", "juliet", "kilo", "lima", "mike", "nova", "oscar",
    "papa", "quebec", "romeo", "sierra", "tango", "victor",
]


def _rand_suffix(rng: random.Random, used: set) -> str:
    """Return a name like `alpha_42` that hasn't been used in this match."""
    while True:
        word = rng.choice(NAME_POOL)
        num = rng.randint(10, 999)
        candidate = f"{word}_{num}"
        if candidate not in used:
            used.add(candidate)
            return candidate


def _js_args(args):
    """Serialise a Python args list to a JS-callable argument string."""
    return ", ".join(json.dumps(a) for a in args)


def _build(prefix, desc, params_sig, body, py, arg_gen, ncases=3):
    """Compile a template into a ``gen(rng, used)`` generator callable."""
    def generator(rng, used):
        fn = f"{prefix}_" + _rand_suffix(rng, used)
        code = f"function {fn}({params_sig}) {{\n{body}\n}}"
        cases = []
        for _ in range(ncases):
            args = arg_gen(rng)
            cases.append({
                "args": args,
                "expected": py(*args),
                "desc": f"{fn}({_js_args(args)})",
            })
        return {"fn_name": fn, "desc": desc, "code": code, "tests": cases}
    return generator


WORDS = [
    "education", "elephant", "umbrella", "octopus", "australia", "interview",
    "mountain", "alphabetical", "monolithic", "sabotage", "javascript",
    "snowflake", "monaco", "robot", "puzzle", "cloud", "input", "rainbow",
    "keyboard", "function", "variable", "computer", "developer", "platform",
]


def _word(rng, lo=4, hi=10):
    return "".join(rng.choices(string.ascii_lowercase, k=rng.randint(lo, hi)))


# --------------------------------------------------------------------------- #
# EASY tier — scalar bugs (no loops)
# --------------------------------------------------------------------------- #
def _easy_factories():
    out = []

    for K in range(1, 21):
        out.append(_build(
            f"add{K}", f"Add {K} to n", "n",
            f"  // Should ADD {K} to n — but it subtracts\n  return n - {K};",
            (lambda k: (lambda n: n + k))(K),
            lambda rng: [rng.randint(1, 40)]))

    for K in range(1, 21):
        out.append(_build(
            f"sub{K}", f"Subtract {K} from n", "n",
            f"  // Should SUBTRACT {K} from n — but it adds\n  return n + {K};",
            (lambda k: (lambda n: n - k))(K),
            lambda rng: [rng.randint(1, 40)]))

    for K in range(2, 22):
        out.append(_build(
            f"mul{K}", f"Multiply n by {K}", "n",
            f"  // Should MULTIPLY n by {K} — but it adds\n  return n + {K};",
            (lambda k: (lambda n: n * k))(K),
            lambda rng: [rng.randint(2, 20)]))

    for K in range(2, 22):
        out.append(_build(
            f"div{K}", f"Divide n by {K}", "n",
            f"  // Should DIVIDE n by {K} — but it multiplies\n  return n * {K};",
            (lambda k: (lambda n: n / k))(K),
            (lambda k: (lambda rng: [k * rng.randint(1, 12)]))(K)))

    for K in range(2, 22):
        out.append(_build(
            f"mod{K}", f"Return the remainder of n divided by {K}", "n",
            f"  // Should be the REMAINDER (n % {K}) — not the quotient\n"
            f"  return Math.floor(n / {K});",
            (lambda k: (lambda n: n % k))(K),
            (lambda k: (lambda rng: [rng.randint(1, k * 8)]))(K)))

    for K in range(1, 21):
        out.append(_build(
            f"gt{K}", f"Return true if n is greater than {K}", "n",
            f"  // Greater-than means n > {K} — this has it backwards\n"
            f"  return n < {K};",
            (lambda k: (lambda n: n > k))(K),
            (lambda k: (lambda rng: [rng.choice(
                list(range(max(0, k - 5), k)) + list(range(k + 1, k + 6)))]))(K)))

    for K in range(1, 21):
        out.append(_build(
            f"lt{K}", f"Return true if n is less than {K}", "n",
            f"  // Less-than means n < {K} — this has it backwards\n"
            f"  return n > {K};",
            (lambda k: (lambda n: n < k))(K),
            (lambda k: (lambda rng: [rng.choice(
                list(range(max(0, k - 5), k)) + list(range(k + 1, k + 6)))]))(K)))

    for K in range(1, 21):
        out.append(_build(
            f"eq{K}", f"Return true only when n equals {K}", "n",
            f"  // Should be TRUE when n equals {K} — this is inverted\n"
            f"  return n !== {K};",
            (lambda k: (lambda n: n == k))(K),
            (lambda k: (lambda rng: [rng.randint(k - 3, k + 3)]))(K)))

    for K in range(5, 25):
        out.append(_build(
            f"from{K}", f"Return {K} minus n", "n",
            f"  // Should be {K} - n — operands are flipped\n  return n - {K};",
            (lambda k: (lambda n: k - n))(K),
            (lambda k: (lambda rng: [rng.choice(
                [x for x in range(0, k + 16) if x != k])]))(K)))

    for K in range(1, 21):
        out.append(_build(
            f"absdiff{K}", f"Return the absolute difference between n and {K}", "n",
            f"  // Should be the ABSOLUTE difference |n - {K}|\n"
            f"  return n - {K};",
            (lambda k: (lambda n: abs(n - k)))(K),
            (lambda k: (lambda rng: [rng.randint(0, max(0, k - 1))]))(K)))

    return out



# --------------------------------------------------------------------------- #
# MEDIUM / HARD tiers — multi-function "pipeline" tasks.
#
# A pipeline is several element-wise transform helpers chained together by a
# clean `main` function.  Each helper carries a planted bug and a comment that
# states *what it should do* (intent) — never how to fix it.  To make the build
# pass the player must read every helper and repair it, so a medium task spans
# 2–3 functions and a hard task spans 4–5.
# --------------------------------------------------------------------------- #
import itertools


def _step(kind, K=0):
    """Return one pipeline stage: intent label, buggy JS expr (per element),
    the correct Python transform, and the *buggy* Python transform (used to
    prove the planted bug actually changes the output)."""
    if kind == "add":
        return {"label": f"Add {K} to every value", "expr": f"arr[i] - {K}",
                "py": (lambda k: (lambda a: [x + k for x in a]))(K),
                "buggy": (lambda k: (lambda a: [x - k for x in a]))(K)}
    if kind == "sub":
        return {"label": f"Subtract {K} from every value", "expr": f"arr[i] + {K}",
                "py": (lambda k: (lambda a: [x - k for x in a]))(K),
                "buggy": (lambda k: (lambda a: [x + k for x in a]))(K)}
    if kind == "mul":
        return {"label": f"Multiply every value by {K}", "expr": f"arr[i] + {K}",
                "py": (lambda k: (lambda a: [x * k for x in a]))(K),
                "buggy": (lambda k: (lambda a: [x + k for x in a]))(K)}
    if kind == "double":
        return {"label": "Double every value", "expr": "arr[i] + 2",
                "py": lambda a: [x * 2 for x in a],
                "buggy": lambda a: [x + 2 for x in a]}
    if kind == "triple":
        return {"label": "Triple every value", "expr": "arr[i] + 3",
                "py": lambda a: [x * 3 for x in a],
                "buggy": lambda a: [x + 3 for x in a]}
    if kind == "square":
        return {"label": "Replace every value with its square",
                "expr": "arr[i] + arr[i]",
                "py": lambda a: [x * x for x in a],
                "buggy": lambda a: [x + x for x in a]}
    if kind == "negate":
        return {"label": "Flip the sign of every value", "expr": "arr[i]",
                "py": lambda a: [-x for x in a],
                "buggy": lambda a: [x for x in a]}
    raise ValueError(kind)


def _diverges(stages):
    """True iff the buggy pipeline differs from the correct one for *every*
    value we test with (stages are element-wise, so this guarantees every
    generated test case fails until the player fixes all the bugs)."""
    for x in range(3, 13):
        cur_ok, cur_bad = [x], [x]
        for st in stages:
            cur_ok = st["py"](cur_ok)
            cur_bad = st["buggy"](cur_bad)
        if cur_ok[0] == cur_bad[0]:
            return False
    return True


# Stage palette — every entry diverges from its bug on inputs >= 3.
_STAGE_SPECS = (
    [("double", 0), ("triple", 0), ("square", 0), ("negate", 0)]
    + [("add", k) for k in range(1, 7)]
    + [("sub", k) for k in range(1, 4)]
    + [("mul", k) for k in range(2, 6)]
)


def _pipeline_build(steps):
    """Compile a fixed list of stage specs into a generator callable."""
    stages = [_step(kind, k) for kind, k in steps]

    def py_all(arr):
        cur = arr
        for st in stages:
            cur = st["py"](cur)
        return cur

    labels = " → ".join(st["label"] for st in stages)
    desc = f"{len(stages)}-step pipeline: {labels}"

    def generator(rng, used):
        token = _rand_suffix(rng, used)
        names, blocks = [], []
        for idx, st in enumerate(stages, 1):
            hname = f"step{idx}_{token}"
            names.append(hname)
            blocks.append(
                f"function {hname}(arr) {{\n"
                f"  // {st['label']}.\n"
                f"  const out = [];\n"
                f"  for (let i = 0; i < arr.length; i++) {{\n"
                f"    out.push({st['expr']});\n"
                f"  }}\n"
                f"  return out;\n}}"
            )
        main = f"build_{token}"
        call = "arr"
        for hname in names:
            call = f"{hname}({call})"
        blocks.append(
            f"function {main}(arr) {{\n"
            f"  // Runs each step above in order. This part is already correct.\n"
            f"  return {call};\n}}"
        )
        code = "\n\n".join(blocks)
        cases = []
        for _ in range(3):
            arr = [rng.randint(3, 12) for _ in range(rng.randint(4, 7))]
            cases.append({"args": [arr], "expected": py_all(arr),
                          "desc": f"{main}({arr})"})
        return {"fn_name": main, "desc": desc, "code": code, "tests": cases}

    return generator


def _pipeline_tier(lengths):
    """Deterministically enumerate distinct pipelines whose length is drawn
    (round-robin) from `lengths`, returning >= 200 unique generators."""
    out, seen = [], set()
    palette = _STAGE_SPECS
    li = 0
    # Use combinations (with a rotating start) to fan out across the palette.
    for r in lengths:
        for combo in itertools.permutations(palette, r):
            # avoid two identical adjacent stages for readability
            if any(combo[i] == combo[i + 1] for i in range(len(combo) - 1)):
                continue
            if combo in seen:
                continue
            stages = [_step(kind, k) for kind, k in combo]
            # reject pipelines whose bugs cancel out (would be trivially solved)
            if not _diverges(stages):
                continue
            seen.add(combo)
            out.append(_pipeline_build(list(combo)))
            if len(out) >= 600:
                return out
    return out


def _medium_factories():
    # 2- and 3-step pipelines, interleaved so the library mixes both.
    two = _pipeline_tier([2])
    three = _pipeline_tier([3])
    merged = []
    for a, b in itertools.zip_longest(two, three):
        if a:
            merged.append(a)
        if b:
            merged.append(b)
    return merged


def _hard_factories():
    # 4- and 5-step pipelines, interleaved.
    four = _pipeline_tier([4])
    five = _pipeline_tier([5])
    merged = []
    for a, b in itertools.zip_longest(four, five):
        if a:
            merged.append(a)
        if b:
            merged.append(b)
    return merged


# --------------------------------------------------------------------------- #
# Build the three 200-task libraries.
# --------------------------------------------------------------------------- #
def _take(factories, n=200):
    if len(factories) < n:
        raise RuntimeError(f"tier only has {len(factories)} tasks, need {n}")
    return factories[:n]


EASY_PROBLEMS = _take(_easy_factories())
MEDIUM_PROBLEMS = _take(_medium_factories())
ALL_PROBLEMS = _take(_hard_factories())   # "hard" = default full library

POOLS = {"easy": EASY_PROBLEMS, "medium": MEDIUM_PROBLEMS, "hard": ALL_PROBLEMS}
MODE_LABELS = {"easy": "Easy", "medium": "Medium", "hard": "Hard"}


# --------------------------------------------------------------------------- #
# match-level orchestration
# --------------------------------------------------------------------------- #
def build_codebase(seed: int | None = None, count: int = 10, mode: str = "hard"):
    """Pick `count` random problems, generate concrete instances, return:
        {
          'editor_code': single big JS string the players see,
          'tests':       [{fn_name, desc, args, expected}, ...] kept on server,
          'problems':    [{fn_name, desc}, ...] cheat-sheet for UI,
        }
    `mode` may be "easy", "medium", or "hard".
    """
    pool = POOLS.get(mode, ALL_PROBLEMS)
    rng = random.Random(seed)
    chosen = rng.sample(pool, k=min(count, len(pool)))
    used_names: set = set()
    blocks, tests, summary = [], [], []
    for i, gen in enumerate(chosen, 1):
        prob = gen(rng, used_names)
        blocks.append(
            f"// ── Problem {i}: {prob['desc']} ──\n{prob['code']}\n"
        )
        for t in prob["tests"]:
            tests.append({
                "fn_name": prob["fn_name"],
                "desc": t["desc"],
                "args": t["args"],
                "expected": t["expected"],
            })
        summary.append({"fn_name": prob["fn_name"], "desc": prob["desc"]})

    mode_label = MODE_LABELS.get(mode, "Hard")
    header = (
        "// ╭──────────────────────────────────────────────────────────────╮\n"
        "// │  Code Sabotage — Build Target                                │\n"
        "// │  Each function below has a planted bug. Fix them all.        │\n"
        f"// │  Difficulty: {mode_label:<8}     Problems: {count:>2}                       │\n"
        "// ╰──────────────────────────────────────────────────────────────╯\n\n"
    )
    return {
        "editor_code": header + "\n".join(blocks),
        "tests": tests,
        "problems": summary,
    }
