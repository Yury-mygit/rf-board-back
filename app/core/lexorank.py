"""BRD-30 Stage 0: LEXORANK primitive для z_rank в board_elements.

Строковые ranks в base62 alphabet (`0-9A-Za-z`), ordered лексикографически.
Ключевая операция — `midpoint(a, b)` — вставка нового элемента между двумя
существующими без rebalance'а всей цепочки.

Reference: Trello / JIRA / Linear LEXORANK style.

Canonical form: trailing MIN_CHAR ('0') убирается (`'a0' → 'a'`), поскольку
семантически 'a' и 'a0' обозначают одну и ту же позицию. Все возвращаемые
из функций ranks — в canonical form.
"""
from __future__ import annotations

import math

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)  # 62
MIN_CHAR = ALPHABET[0]  # '0'
MAX_CHAR = ALPHABET[-1]  # 'z'
MID_CHAR = ALPHABET[BASE // 2]  # 'V' (index 31)
IDX = {c: i for i, c in enumerate(ALPHABET)}


def _decode(s: str) -> int:
    """Строка → integer в base62."""
    v = 0
    for c in s:
        v = v * BASE + IDX[c]
    return v


def _encode(v: int, width: int) -> str:
    """Integer → строка ширины `width` в base62 (leading zeros)."""
    chars = []
    for _ in range(width):
        chars.append(ALPHABET[v % BASE])
        v //= BASE
    return "".join(reversed(chars))


def midpoint(a: str, b: str) -> str:
    """Rank r такой, что `a < r < b` (lex).

    Требования:
    - `a < b` строго (иначе `ValueError`).
    - Non-empty, canonical (no trailing MIN_CHAR).
    """
    if a >= b:
        raise ValueError(f"midpoint requires a < b, got a={a!r} b={b!r}")

    width = max(len(a), len(b))
    while True:
        ai = _decode(a.ljust(width, MIN_CHAR))
        bi = _decode(b.ljust(width, MIN_CHAR))
        if bi - ai >= 2:
            break
        width += 1

    mid_v = (ai + bi) // 2
    result = _encode(mid_v, width)

    # Canonicalize: trim trailing MIN_CHAR while result > a strictly.
    while len(result) > 1 and result[-1] == MIN_CHAR:
        candidate = result[:-1]
        if a < candidate < b:
            result = candidate
        else:
            break
    return result


def rank_after(x: str) -> str:
    """Rank > x. Append MID_CHAR — гарантирует `x < x + 'V'` (x prefix)."""
    return x + MID_CHAR


def rank_before(x: str) -> str:
    """Rank < x через midpoint('0', x). Raises если x <= MIN_CHAR."""
    if x <= MIN_CHAR:
        raise ValueError(f"cannot rank_before {x!r}: no rank exists below")
    return midpoint(MIN_CHAR, x)


def rank_between(a: str | None, b: str | None) -> str:
    """Универсальный:
    - `a=None, b=None` → MID_CHAR ('V').
    - `a=None, b=X` → `rank_before(X)`.
    - `a=X, b=None` → `rank_after(X)`.
    - `a=X, b=Y` → `midpoint(X, Y)`.
    """
    if a is None and b is None:
        return MID_CHAR
    if a is None:
        return rank_before(b)
    if b is None:
        return rank_after(a)
    return midpoint(a, b)


def rebalance(ranks: list[str]) -> list[str]:
    """Переупорядочить N ranks с равномерными gap'ами. Порядок и count
    сохраняются, но max_length сжимается.

    Используется когда цепочка insert'ов вырастила длину до > threshold
    (BRD-30 D1: VARCHAR(64) — reference).
    """
    n = len(ranks)
    if n == 0:
        return []
    if n == 1:
        return [MID_CHAR]

    # Width k: минимально достаточная для n + 1 равномерных слотов.
    k = max(1, math.ceil(math.log(n + 2, BASE)))
    step = (BASE ** k) // (n + 1)
    if step < 1:
        # Fallback (не должен срабатывать при разумных n).
        step = 1
        k = max(k, math.ceil(math.log(n + 2, BASE)))

    result = []
    for i in range(1, n + 1):
        val = i * step
        result.append(_encode(val, k))
    return result
