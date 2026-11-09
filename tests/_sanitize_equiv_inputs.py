"""Deterministic edge inputs for the sanitize_user_content equivalence test (task 0008)."""

import random


def equivalence_cases():
    """(text, kwargs) pairs covering emoji, control and invisible chars and the limit."""
    base = [
        None, "", " ", "\n\t ", "plain", "  padded  ", "hello\x00world", "a\x07b\x1bc\x7fd",
        "tab\tnew\nline\r\nend", "zero​width‌‍﻿", "rtl‮override‬",
        "\U0001F600", "\U0001F468‍\U0001F469‍\U0001F467", "a‍b", "‍",
        "\U0001F44D\U0001F3FD", "❤️", "1️⃣", "é", "é", "\u0000",
        "\U000E0041tag", "  ", "­ soft", "[empty]", "x... [truncated]",
        "​‌", "\x07\x08",
    ]  # fmt: skip
    cases = [(t, {}) for t in base]
    cases += [(t, {"preserve_emoji": True}) for t in base]
    for n in (4095, 4096, 4097, 5000, 20000):
        cases.append(("a" * n, {}))
        cases.append(("\U0001f600" * n, {}))
        cases.append(("\U0001f600" * n, {"preserve_emoji": True}))
        cases.append(("a​" * n, {}))
        cases.append((" " + "b" * n + " ", {}))
    for n in (0, 1, 5, 10):
        cases.append(("0123456789abc", {"max_length": n}))
        cases.append(("\U0001f468‍\U0001f469" * 4, {"max_length": n, "preserve_emoji": True}))
    rng = random.Random(8)
    pool = list("ab \n\t\x00\x07​‍‮️\U0001f600\U0001f468é́[]")
    for _ in range(300):
        s = "".join(rng.choice(pool) for _ in range(rng.randint(0, 60)))
        cases.append((s, {"max_length": rng.choice([4096, 8, 30])}))
        cases.append((s, {"preserve_emoji": True}))
    return cases
