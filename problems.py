"""
Procedural Bug Engine — Problem Library.

Each generator returns a dict:
    {
      'fn_name':  unique JS identifier injected into the codebase,
      'desc':     short description shown to players,
      'code':     a JS function definition (with a planted bug) the players see,
      'tests':    list of {args, expected, desc} — kept on the server, sent to
                  the client only at run-time so the test source isn't visible
                  in the editor itself.
    }

The server randomly draws 10 problems per match, randomises variable / function
names and target constants, and concatenates the function bodies into a single
"codebase".  All 10 hidden tests must pass for build-status to reach 100 %.
"""

import json
import random
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


# --------------------------------------------------------------------------- #
# problem generators
# --------------------------------------------------------------------------- #
def p_sum_less_than(rng, used):
    fn = "sumLessThan_" + _rand_suffix(rng, used)
    threshold = rng.randint(8, 60)
    code = (
        f"function {fn}(arr) {{\n"
        f"  // BUG: should be `<` not `<=`\n"
        f"  let total = 0;\n"
        f"  for (let i = 0; i < arr.length; i++) {{\n"
        f"    if (arr[i] <= {threshold}) total += arr[i];\n"
        f"  }}\n"
        f"  return total;\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        arr = [rng.randint(1, 100) for _ in range(rng.randint(4, 8))]
        expected = sum(x for x in arr if x < threshold)
        cases.append({"args": [arr], "expected": expected,
                      "desc": f"{fn}({arr}) — strictly less than {threshold}"})
    return {"fn_name": fn, "desc": f"Sum of values strictly less than {threshold}",
            "code": code, "tests": cases}


def p_count_even(rng, used):
    fn = "countEven_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(arr) {{\n"
        f"  // BUG: even means n % 2 === 0\n"
        f"  return arr.filter(n => n % 2 === 1).length;\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        arr = [rng.randint(0, 50) for _ in range(rng.randint(5, 9))]
        expected = sum(1 for x in arr if x % 2 == 0)
        cases.append({"args": [arr], "expected": expected,
                      "desc": f"{fn}({arr})"})
    return {"fn_name": fn, "desc": "Count even numbers in array",
            "code": code, "tests": cases}


def p_reverse_string(rng, used):
    fn = "reverseString_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(s) {{\n"
        f"  // BUG: off-by-one — should iterate while i >= 0\n"
        f"  let out = '';\n"
        f"  for (let i = s.length - 1; i > 0; i--) out += s[i];\n"
        f"  return out;\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        s = "".join(rng.choices(string.ascii_lowercase, k=rng.randint(4, 9)))
        cases.append({"args": [s], "expected": s[::-1], "desc": f"{fn}({s!r})"})
    return {"fn_name": fn, "desc": "Reverse a string",
            "code": code, "tests": cases}


def p_find_max(rng, used):
    fn = "findMax_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(arr) {{\n"
        f"  // BUG: comparison is reversed\n"
        f"  let best = arr[0];\n"
        f"  for (let i = 1; i < arr.length; i++) {{\n"
        f"    if (arr[i] < best) best = arr[i];\n"
        f"  }}\n"
        f"  return best;\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        arr = [rng.randint(-50, 50) for _ in range(rng.randint(4, 8))]
        cases.append({"args": [arr], "expected": max(arr),
                      "desc": f"{fn}({arr})"})
    return {"fn_name": fn, "desc": "Return largest number in array",
            "code": code, "tests": cases}


def p_factorial(rng, used):
    fn = "factorial_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(n) {{\n"
        f"  // BUG: starts at 0, so result is always 0\n"
        f"  let result = 0;\n"
        f"  for (let i = 1; i <= n; i++) result *= i;\n"
        f"  return result;\n"
        f"}}"
    )
    cases = []
    for n in rng.sample([3, 4, 5, 6, 7], 3):
        exp = 1
        for i in range(1, n + 1):
            exp *= i
        cases.append({"args": [n], "expected": exp, "desc": f"{fn}({n})"})
    return {"fn_name": fn, "desc": "Compute n!",
            "code": code, "tests": cases}


def p_is_palindrome(rng, used):
    fn = "isPalindrome_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(s) {{\n"
        f"  // BUG: should compare s[i] to s[s.length-1-i]\n"
        f"  for (let i = 0; i < s.length / 2; i++) {{\n"
        f"    if (s[i] !== s[s.length - i]) return false;\n"
        f"  }}\n"
        f"  return true;\n"
        f"}}"
    )
    palindromes = ["racecar", "level", "noon", "kayak", "civic", "rotor"]
    others = ["hello", "world", "puzzle", "banana", "monaco"]
    samples = rng.sample(palindromes, 2) + rng.sample(others, 1)
    rng.shuffle(samples)
    cases = [{"args": [s], "expected": s == s[::-1], "desc": f"{fn}({s!r})"}
             for s in samples]
    return {"fn_name": fn, "desc": "True if input is a palindrome",
            "code": code, "tests": cases}


def p_multiply_all(rng, used):
    fn = "multiplyAll_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(arr) {{\n"
        f"  // BUG: accumulator should start at 1\n"
        f"  return arr.reduce((acc, n) => acc * n, 0);\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        arr = [rng.randint(1, 6) for _ in range(rng.randint(3, 5))]
        prod = 1
        for x in arr:
            prod *= x
        cases.append({"args": [arr], "expected": prod, "desc": f"{fn}({arr})"})
    return {"fn_name": fn, "desc": "Product of all numbers",
            "code": code, "tests": cases}


def p_remove_vowels(rng, used):
    fn = "removeVowels_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(s) {{\n"
        f"  // BUG: regex missing /g flag\n"
        f"  return s.replace(/[aeiou]/, '');\n"
        f"}}"
    )
    samples = rng.sample(
        ["education", "elephant", "umbrella", "octopus", "australia",
         "interview", "mountain"], 3)
    cases = []
    import re
    for s in samples:
        expected = re.sub("[aeiou]", "", s)
        cases.append({"args": [s], "expected": expected, "desc": f"{fn}({s!r})"})
    return {"fn_name": fn, "desc": "Strip all lowercase vowels",
            "code": code, "tests": cases}


def p_divisible_by(rng, used):
    fn = "divisibleBy_" + _rand_suffix(rng, used)
    divisor = rng.randint(2, 9)
    code = (
        f"function {fn}(arr) {{\n"
        f"  // BUG: divisible test should use modulo (%) not division (/)\n"
        f"  return arr.filter(n => n / {divisor} === 0);\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        arr = [rng.randint(1, 60) for _ in range(rng.randint(5, 8))]
        expected = [n for n in arr if n % divisor == 0]
        cases.append({"args": [arr], "expected": expected,
                      "desc": f"{fn}({arr}) — divisible by {divisor}"})
    return {"fn_name": fn, "desc": f"Keep numbers divisible by {divisor}",
            "code": code, "tests": cases}


def p_word_count(rng, used):
    fn = "wordCount_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(s) {{\n"
        f"  // BUG: splitting on '' yields characters, not words\n"
        f"  return s.split('').length;\n"
        f"}}"
    )
    samples = [
        "the quick brown fox",
        "monaco editor is fun",
        "code sabotage rocks",
        "social deduction games",
    ]
    cases = []
    for s in rng.sample(samples, 3):
        cases.append({"args": [s], "expected": len(s.split()),
                      "desc": f"{fn}({s!r})"})
    return {"fn_name": fn, "desc": "Count words in a sentence",
            "code": code, "tests": cases}


def p_average(rng, used):
    fn = "average_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(arr) {{\n"
        f"  // BUG: divides by length+1 instead of length\n"
        f"  const total = arr.reduce((a, b) => a + b, 0);\n"
        f"  return total / (arr.length + 1);\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        arr = [rng.randint(2, 30) * 2 for _ in range(rng.randint(3, 5))]
        cases.append({"args": [arr], "expected": sum(arr) / len(arr),
                      "desc": f"{fn}({arr})"})
    return {"fn_name": fn, "desc": "Arithmetic mean of an array",
            "code": code, "tests": cases}


def p_contains_substring(rng, used):
    fn = "containsSubstring_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(s, t) {{\n"
        f"  // BUG: should use includes, not startsWith\n"
        f"  return s.startsWith(t);\n"
        f"}}"
    )
    cases = [
        {"args": ["javascript", "scrip"], "expected": True,
         "desc": f"{fn}('javascript', 'scrip')"},
        {"args": ["monaco", "ona"], "expected": True,
         "desc": f"{fn}('monaco', 'ona')"},
        {"args": ["editor", "xyz"], "expected": False,
         "desc": f"{fn}('editor', 'xyz')"},
    ]
    rng.shuffle(cases)
    return {"fn_name": fn, "desc": "True if t appears anywhere inside s",
            "code": code, "tests": cases}


def p_range(rng, used):
    fn = "range_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(n) {{\n"
        f"  // BUG: <= n includes n; should be < n\n"
        f"  const out = [];\n"
        f"  for (let i = 0; i <= n; i++) out.push(i);\n"
        f"  return out;\n"
        f"}}"
    )
    cases = []
    for n in rng.sample([3, 4, 5, 6, 7, 8], 3):
        cases.append({"args": [n], "expected": list(range(n)),
                      "desc": f"{fn}({n})"})
    return {"fn_name": fn, "desc": "Build [0..n-1]",
            "code": code, "tests": cases}


def p_map_squares(rng, used):
    fn = "mapSquares_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(arr) {{\n"
        f"  // BUG: arrow body uses braces but never returns\n"
        f"  return arr.map(n => {{ n * n; }});\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        arr = [rng.randint(1, 9) for _ in range(rng.randint(3, 5))]
        cases.append({"args": [arr], "expected": [n * n for n in arr],
                      "desc": f"{fn}({arr})"})
    return {"fn_name": fn, "desc": "Square each element",
            "code": code, "tests": cases}


def p_sort_ascending(rng, used):
    fn = "sortAscending_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(arr) {{\n"
        f"  // BUG: default .sort() is lexicographic — needs (a,b)=>a-b\n"
        f"  return [...arr].sort();\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        arr = rng.sample(range(1, 100), rng.randint(4, 6))
        cases.append({"args": [arr], "expected": sorted(arr),
                      "desc": f"{fn}({arr})"})
    return {"fn_name": fn, "desc": "Sort numbers ascending",
            "code": code, "tests": cases}


def p_capitalize(rng, used):
    fn = "capitalize_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(s) {{\n"
        f"  // BUG: drops first char by slicing from index 0\n"
        f"  return s[0].toUpperCase() + s.slice(0);\n"
        f"}}"
    )
    samples = rng.sample(
        ["python", "monaco", "sabotage", "rooms", "snippet", "lobby"], 3)
    cases = []
    for s in samples:
        cases.append({"args": [s], "expected": s[0].upper() + s[1:],
                      "desc": f"{fn}({s!r})"})
    return {"fn_name": fn, "desc": "Capitalise the first letter",
            "code": code, "tests": cases}


def p_unique_elements(rng, used):
    fn = "uniqueElements_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(arr) {{\n"
        f"  // BUG: indexOf returns -1 when missing, > 0 misses index 0\n"
        f"  const out = [];\n"
        f"  for (const x of arr) if (out.indexOf(x) > 0) continue;\n"
        f"  else out.push(x);\n"
        f"  return out;\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        base = [rng.randint(1, 5) for _ in range(rng.randint(5, 9))]
        seen, expected = set(), []
        for x in base:
            if x not in seen:
                seen.add(x)
                expected.append(x)
        cases.append({"args": [base], "expected": expected,
                      "desc": f"{fn}({base})"})
    return {"fn_name": fn, "desc": "Drop duplicate values, preserve order",
            "code": code, "tests": cases}


def p_count_chars(rng, used):
    fn = "countChars_" + _rand_suffix(rng, used)
    target = rng.choice(list("aeolns"))
    code = (
        f"function {fn}(s) {{\n"
        f"  // BUG: returns position of first hit instead of count\n"
        f"  return s.indexOf('{target}');\n"
        f"}}"
    )
    samples = rng.sample(
        ["alphabetical", "monolithic", "sabotage", "javascript", "snowflake"], 3)
    cases = []
    for s in samples:
        cases.append({"args": [s], "expected": s.count(target),
                      "desc": f"{fn}({s!r}) — count '{target}'"})
    return {"fn_name": fn, "desc": f"Count occurrences of letter '{target}'",
            "code": code, "tests": cases}


def p_min_index(rng, used):
    fn = "minIndex_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(arr) {{\n"
        f"  // BUG: tracks the value, not the index\n"
        f"  let best = arr[0];\n"
        f"  for (let i = 1; i < arr.length; i++) {{\n"
        f"    if (arr[i] < best) best = arr[i];\n"
        f"  }}\n"
        f"  return best;\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        arr = rng.sample(range(1, 99), rng.randint(4, 7))
        cases.append({"args": [arr], "expected": arr.index(min(arr)),
                      "desc": f"{fn}({arr})"})
    return {"fn_name": fn, "desc": "Return INDEX of smallest number",
            "code": code, "tests": cases}


def p_repeat_string(rng, used):
    fn = "repeatString_" + _rand_suffix(rng, used)
    code = (
        f"function {fn}(s, n) {{\n"
        f"  // BUG: loops n+1 times — should be `i < n`\n"
        f"  let out = '';\n"
        f"  for (let i = 0; i <= n; i++) out += s;\n"
        f"  return out;\n"
        f"}}"
    )
    cases = []
    for _ in range(3):
        s = rng.choice(["ab", "x", "go", "no"])
        n = rng.randint(2, 5)
        cases.append({"args": [s, n], "expected": s * n,
                      "desc": f"{fn}({s!r}, {n})"})
    return {"fn_name": fn, "desc": "Repeat string n times",
            "code": code, "tests": cases}


# Master list of HARD-mode problem generators
ALL_PROBLEMS = [
    p_sum_less_than, p_count_even, p_reverse_string, p_find_max,
    p_factorial, p_is_palindrome, p_multiply_all, p_remove_vowels,
    p_divisible_by, p_word_count, p_average, p_contains_substring,
    p_range, p_map_squares, p_sort_ascending, p_capitalize,
    p_unique_elements, p_count_chars, p_min_index, p_repeat_string,
]


# --------------------------------------------------------------------------- #
# EASY-mode problem library.
#
# These are intentionally simple bugs — no array methods, regex, reduce, or
# tricky language quirks.  Each is a 1-liner with an obviously-wrong operator
# or off-by-one constant, so a non-JS player can still spot the issue by
# reading the comment + code carefully.
# --------------------------------------------------------------------------- #
def e_add(rng, used):
    fn = "add_" + _rand_suffix(rng, used)
    code = (f"function {fn}(a, b) {{\n"
            f"  // Should add a and b together\n"
            f"  return a - b;\n}}")
    cases = []
    for _ in range(3):
        a, b = rng.randint(1, 20), rng.randint(1, 20)
        cases.append({"args": [a, b], "expected": a + b, "desc": f"{fn}({a}, {b})"})
    return {"fn_name": fn, "desc": "Add two numbers together",
            "code": code, "tests": cases}


def e_subtract(rng, used):
    fn = "subtract_" + _rand_suffix(rng, used)
    code = (f"function {fn}(a, b) {{\n"
            f"  // Should subtract b from a\n"
            f"  return a + b;\n}}")
    cases = []
    for _ in range(3):
        a, b = rng.randint(10, 30), rng.randint(1, 9)
        cases.append({"args": [a, b], "expected": a - b, "desc": f"{fn}({a}, {b})"})
    return {"fn_name": fn, "desc": "Subtract b from a",
            "code": code, "tests": cases}


def e_multiply(rng, used):
    fn = "multiply_" + _rand_suffix(rng, used)
    code = (f"function {fn}(a, b) {{\n"
            f"  // Should multiply a by b\n"
            f"  return a + b;\n}}")
    cases = []
    for _ in range(3):
        a, b = rng.randint(2, 9), rng.randint(2, 9)
        cases.append({"args": [a, b], "expected": a * b, "desc": f"{fn}({a}, {b})"})
    return {"fn_name": fn, "desc": "Multiply two numbers",
            "code": code, "tests": cases}


def e_double(rng, used):
    fn = "double_" + _rand_suffix(rng, used)
    code = (f"function {fn}(n) {{\n"
            f"  // Should DOUBLE n (multiply by 2), not add 2\n"
            f"  return n + 2;\n}}")
    cases = []
    for n in rng.sample(range(2, 25), 3):
        cases.append({"args": [n], "expected": n * 2, "desc": f"{fn}({n})"})
    return {"fn_name": fn, "desc": "Return n doubled (n × 2)",
            "code": code, "tests": cases}


def e_triple(rng, used):
    fn = "triple_" + _rand_suffix(rng, used)
    code = (f"function {fn}(n) {{\n"
            f"  // Should TRIPLE n (multiply by 3), not add 3\n"
            f"  return n + 3;\n}}")
    cases = []
    for n in rng.sample(range(2, 25), 3):
        cases.append({"args": [n], "expected": n * 3, "desc": f"{fn}({n})"})
    return {"fn_name": fn, "desc": "Return n tripled (n × 3)",
            "code": code, "tests": cases}


def e_square(rng, used):
    fn = "square_" + _rand_suffix(rng, used)
    code = (f"function {fn}(n) {{\n"
            f"  // Square = n times n, not n plus n\n"
            f"  return n + n;\n}}")
    cases = []
    for n in rng.sample(range(3, 12), 3):
        cases.append({"args": [n], "expected": n * n, "desc": f"{fn}({n})"})
    return {"fn_name": fn, "desc": "Return n squared (n × n)",
            "code": code, "tests": cases}


def e_addOne(rng, used):
    fn = "addOne_" + _rand_suffix(rng, used)
    code = (f"function {fn}(n) {{\n"
            f"  // Should add ONE — but it's adding two\n"
            f"  return n + 2;\n}}")
    cases = []
    for n in rng.sample(range(1, 30), 3):
        cases.append({"args": [n], "expected": n + 1, "desc": f"{fn}({n})"})
    return {"fn_name": fn, "desc": "Add 1 to n",
            "code": code, "tests": cases}


def e_isPositive(rng, used):
    fn = "isPositive_" + _rand_suffix(rng, used)
    code = (f"function {fn}(n) {{\n"
            f"  // Positive means n is GREATER than zero\n"
            f"  return n < 0;\n}}")
    nums = rng.sample(list(range(-10, -1)) + list(range(1, 11)), 3)
    cases = [{"args": [n], "expected": n > 0, "desc": f"{fn}({n})"} for n in nums]
    return {"fn_name": fn, "desc": "Return true if n is greater than 0",
            "code": code, "tests": cases}


def e_isNegative(rng, used):
    fn = "isNegative_" + _rand_suffix(rng, used)
    code = (f"function {fn}(n) {{\n"
            f"  // Negative means n is LESS than zero\n"
            f"  return n > 0;\n}}")
    nums = rng.sample(list(range(-10, -1)) + list(range(1, 11)), 3)
    cases = [{"args": [n], "expected": n < 0, "desc": f"{fn}({n})"} for n in nums]
    return {"fn_name": fn, "desc": "Return true if n is less than 0",
            "code": code, "tests": cases}


def e_bigger(rng, used):
    fn = "bigger_" + _rand_suffix(rng, used)
    code = (f"function {fn}(a, b) {{\n"
            f"  // Should return the BIGGER one — but returns the smaller\n"
            f"  if (a < b) return a;\n"
            f"  return b;\n}}")
    cases = []
    for _ in range(3):
        a, b = rng.sample(range(1, 50), 2)
        cases.append({"args": [a, b], "expected": max(a, b), "desc": f"{fn}({a}, {b})"})
    return {"fn_name": fn, "desc": "Return the larger of a and b",
            "code": code, "tests": cases}


def e_smaller(rng, used):
    fn = "smaller_" + _rand_suffix(rng, used)
    code = (f"function {fn}(a, b) {{\n"
            f"  // Should return the SMALLER one — but returns the bigger\n"
            f"  if (a > b) return a;\n"
            f"  return b;\n}}")
    cases = []
    for _ in range(3):
        a, b = rng.sample(range(1, 50), 2)
        cases.append({"args": [a, b], "expected": min(a, b), "desc": f"{fn}({a}, {b})"})
    return {"fn_name": fn, "desc": "Return the smaller of a and b",
            "code": code, "tests": cases}


def e_half(rng, used):
    fn = "half_" + _rand_suffix(rng, used)
    code = (f"function {fn}(n) {{\n"
            f"  // Half means divide by TWO, not three\n"
            f"  return n / 3;\n}}")
    nums = rng.sample([6, 8, 10, 12, 14, 16, 20, 24], 3)
    cases = [{"args": [n], "expected": n / 2, "desc": f"{fn}({n})"} for n in nums]
    return {"fn_name": fn, "desc": "Return half of n (n ÷ 2)",
            "code": code, "tests": cases}


def e_firstLetter(rng, used):
    fn = "firstLetter_" + _rand_suffix(rng, used)
    code = (f"function {fn}(s) {{\n"
            f"  // Letters are numbered starting at 0 — so first is s[0]\n"
            f"  return s[1];\n}}")
    samples = rng.sample(["apple", "monaco", "robot", "puzzle", "cloud", "input"], 3)
    cases = [{"args": [s], "expected": s[0], "desc": f"{fn}({s!r})"} for s in samples]
    return {"fn_name": fn, "desc": "Return the FIRST letter of s",
            "code": code, "tests": cases}


def e_lastLetter(rng, used):
    fn = "lastLetter_" + _rand_suffix(rng, used)
    code = (f"function {fn}(s) {{\n"
            f"  // Last index is s.length - 1, not s.length - 2\n"
            f"  return s[s.length - 2];\n}}")
    samples = rng.sample(["apple", "monaco", "robot", "puzzle", "cloud", "input"], 3)
    cases = [{"args": [s], "expected": s[-1], "desc": f"{fn}({s!r})"} for s in samples]
    return {"fn_name": fn, "desc": "Return the LAST letter of s",
            "code": code, "tests": cases}


def e_length(rng, used):
    fn = "length_" + _rand_suffix(rng, used)
    code = (f"function {fn}(s) {{\n"
            f"  // s.length is already the count — don't subtract 1\n"
            f"  return s.length - 1;\n}}")
    samples = rng.sample(["cat", "robot", "monaco", "elephant", "fix", "five"], 3)
    cases = [{"args": [s], "expected": len(s), "desc": f"{fn}({s!r})"} for s in samples]
    return {"fn_name": fn, "desc": "Return how many letters are in s",
            "code": code, "tests": cases}


def e_greet(rng, used):
    fn = "greet_" + _rand_suffix(rng, used)
    code = (f"function {fn}(name) {{\n"
            f"  // Should glue 'Hello, ' to the actual name (use +)\n"
            f"  return 'Hello, name';\n}}")
    samples = rng.sample(["Ada", "Linus", "Grace", "Marie", "Alan", "Hedy"], 3)
    cases = [{"args": [s], "expected": f"Hello, {s}", "desc": f"{fn}({s!r})"} for s in samples]
    return {"fn_name": fn, "desc": "Return 'Hello, <name>'",
            "code": code, "tests": cases}


def e_isZero(rng, used):
    fn = "isZero_" + _rand_suffix(rng, used)
    code = (f"function {fn}(n) {{\n"
            f"  // Should be true ONLY when n is zero\n"
            f"  return n === 1;\n}}")
    nums = rng.sample([-3, -1, 0, 0, 0, 1, 2, 5], 3)
    cases = [{"args": [n], "expected": n == 0, "desc": f"{fn}({n})"} for n in nums]
    return {"fn_name": fn, "desc": "Return true only when n equals 0",
            "code": code, "tests": cases}


def e_negate(rng, used):
    fn = "negate_" + _rand_suffix(rng, used)
    code = (f"function {fn}(n) {{\n"
            f"  // Should flip the sign — return -n\n"
            f"  return n;\n}}")
    nums = rng.sample(list(range(-10, 0)) + list(range(1, 11)), 3)
    cases = [{"args": [n], "expected": -n, "desc": f"{fn}({n})"} for n in nums]
    return {"fn_name": fn, "desc": "Flip the sign of n (positive ⇄ negative)",
            "code": code, "tests": cases}


def e_firstItem(rng, used):
    fn = "firstItem_" + _rand_suffix(rng, used)
    code = (f"function {fn}(arr) {{\n"
            f"  // Lists start at index 0, not 1\n"
            f"  return arr[1];\n}}")
    cases = []
    for _ in range(3):
        arr = [rng.randint(1, 99) for _ in range(rng.randint(3, 5))]
        cases.append({"args": [arr], "expected": arr[0], "desc": f"{fn}({arr})"})
    return {"fn_name": fn, "desc": "Return the FIRST item in the list",
            "code": code, "tests": cases}


def e_lastItem(rng, used):
    fn = "lastItem_" + _rand_suffix(rng, used)
    code = (f"function {fn}(arr) {{\n"
            f"  // Last index is arr.length - 1, not 0\n"
            f"  return arr[0];\n}}")
    cases = []
    for _ in range(3):
        arr = [rng.randint(1, 99) for _ in range(rng.randint(3, 5))]
        cases.append({"args": [arr], "expected": arr[-1], "desc": f"{fn}({arr})"})
    return {"fn_name": fn, "desc": "Return the LAST item in the list",
            "code": code, "tests": cases}


EASY_PROBLEMS = [
    e_add, e_subtract, e_multiply, e_double, e_triple,
    e_square, e_addOne, e_isPositive, e_isNegative, e_bigger,
    e_smaller, e_half, e_firstLetter, e_lastLetter, e_length,
    e_greet, e_isZero, e_negate, e_firstItem, e_lastItem,
]


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
    `mode` may be "easy" (newcomer-friendly bugs) or "hard" (full library).
    """
    pool = EASY_PROBLEMS if mode == "easy" else ALL_PROBLEMS
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

    mode_label = "Easy" if mode == "easy" else "Hard"
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
