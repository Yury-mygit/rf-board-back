"""BRD-30 Stage 0: LEXORANK primitive — unit tests.

Contract (см. `app/core/lexorank.py`):

- `ALPHABET` — Base62 sortable string: `"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"`.
  Порядок ASCII → лексикографическая сортировка строк совпадает с
  desired rank order.

- `midpoint(a: str, b: str) -> str` — возвращает rank r такой, что
  `a < r < b` (лексикографически). Требования:
  - `a < b` — обязательно (иначе `ValueError`).
  - `len(r) <= max(len(a), len(b)) + 1` (типовой случай).
  - Не пустая строка.
  - Не совпадает с `a` или `b`.

- `rank_after(x: str) -> str` — rank r такой, что `r > x`. Простое
  правило: `x + FIRST_CHAR` (append '1' — минимальный не-zero digit).
  Actually лучше: incrementing последнего символа x, или append.

- `rank_before(x: str) -> str` — rank r такой, что `r < x`.
  Decrement или взятие ниже.

- `rank_between(a: str | None, b: str | None) -> str` — универсальная
  функция:
  - `a=None, b=None` → некий default (e.g., `"V"` — примерный
    middle).
  - `a=None, b=X` → `rank_before(X)`.
  - `a=X, b=None` → `rank_after(X)`.
  - `a=X, b=Y` → `midpoint(X, Y)`.

- `rebalance(ranks: list[str]) -> list[str]` — переупорядочить N
  ranks с равномерными gap'ами. Используется когда цепочка insert
  привела к overflow длины.

Все функции чистые (no I/O, no state).
"""
from __future__ import annotations

import pytest

from app.core.lexorank import (
    ALPHABET,
    midpoint,
    rank_after,
    rank_before,
    rank_between,
    rebalance,
)


# ─── ALPHABET ────────────────────────────────────────────────────────

def test_alphabet_is_ascii_sorted():
    """ALPHABET должен быть sortable — все символы возрастают по ASCII."""
    for i in range(1, len(ALPHABET)):
        assert ALPHABET[i-1] < ALPHABET[i], (
            f"non-sorted at index {i}: {ALPHABET[i-1]!r} vs {ALPHABET[i]!r}"
        )


def test_alphabet_is_base62():
    assert len(ALPHABET) == 62
    assert ALPHABET[0] == "0"
    assert ALPHABET[-1] == "z"


# ─── midpoint(a, b) ──────────────────────────────────────────────────

def test_midpoint_between_adjacent_singletons():
    """midpoint('0', '2') — где-то в промежутке."""
    r = midpoint("0", "2")
    assert "0" < r < "2"


def test_midpoint_between_same_length_close():
    """midpoint('a', 'b') — не пустой, длина не более max+1."""
    r = midpoint("a", "b")
    assert "a" < r < "b"
    assert len(r) <= 2


def test_midpoint_between_different_lengths():
    r = midpoint("a", "az")
    assert "a" < r < "az"


def test_midpoint_far_apart():
    r = midpoint("0", "z")
    assert "0" < r < "z"
    assert len(r) == 1  # много места, hop в один char хватает


def test_midpoint_raises_when_a_ge_b():
    with pytest.raises(ValueError):
        midpoint("b", "a")
    with pytest.raises(ValueError):
        midpoint("a", "a")


def test_midpoint_deep_recursion_still_works():
    """Симулируем цепочку insert'ов между одной парой — глубина растёт."""
    a, b = "a", "b"
    for _ in range(20):
        r = midpoint(a, b)
        assert a < r < b
        b = r  # каждый раз вставляем перед прежним b
    # После 20 insert'ов rank должен быть длинным, но <64 (наш VARCHAR limit).
    assert len(b) < 64


# ─── rank_after(x) / rank_before(x) ──────────────────────────────────

def test_rank_after_produces_larger():
    assert rank_after("a") > "a"
    assert rank_after("z") > "z"
    assert rank_after("0") > "0"


def test_rank_before_produces_smaller():
    assert rank_before("z") < "z"
    assert rank_before("a") < "a"
    assert rank_before("1") < "1"


def test_rank_after_chain_grows_monotonically():
    prev = "V"
    for _ in range(10):
        nxt = rank_after(prev)
        assert nxt > prev
        prev = nxt


def test_rank_before_chain_grows_monotonically():
    prev = "V"
    for _ in range(10):
        nxt = rank_before(prev)
        assert nxt < prev
        prev = nxt


# ─── rank_between(a, b) — универсальная ─────────────────────────────

def test_rank_between_both_none_returns_middle():
    r = rank_between(None, None)
    assert r  # непусто


def test_rank_between_only_a_delegates_to_after():
    r = rank_between("m", None)
    assert r > "m"


def test_rank_between_only_b_delegates_to_before():
    r = rank_between(None, "m")
    assert r < "m"


def test_rank_between_both_delegates_to_midpoint():
    r = rank_between("a", "z")
    assert "a" < r < "z"


def test_rank_between_swapped_raises():
    with pytest.raises(ValueError):
        rank_between("z", "a")


# ─── rebalance(ranks) ────────────────────────────────────────────────

def test_rebalance_preserves_order_and_count():
    """rebalance([...]) → same length, still sorted."""
    original = ["a", "aa", "aaa", "aaaa", "b"]
    result = rebalance(original)
    assert len(result) == len(original)
    for i in range(1, len(result)):
        assert result[i-1] < result[i]


def test_rebalance_reduces_max_length():
    """После rebalance средний rank не должен быть монструозно длинным."""
    original = ["a" + "a" * i for i in range(50)]  # a, aa, aaa, ... aaaa...a
    result = rebalance(original)
    max_len = max(len(r) for r in result)
    assert max_len <= 10, f"rebalance не помог: max_len={max_len}"


def test_rebalance_single_element():
    result = rebalance(["hello"])
    assert len(result) == 1
    # Для одиночного — просто оставить или дать canonical middle.
    assert result[0]


def test_rebalance_empty():
    assert rebalance([]) == []


# ─── Property: chain of midpoint'ов остаётся сортируемым ────────────

def test_property_chain_of_inserts_stays_sorted():
    """Симулируем 100 случайных insert'ов между произвольной парой —
    после каждого insert'а результирующий список должен быть отсортирован."""
    import random
    random.seed(42)
    ranks = [rank_between(None, None)]
    for _ in range(100):
        # Вставляем между random парой (или в начало/конец).
        i = random.randint(0, len(ranks))
        if i == 0:
            new = rank_between(None, ranks[0])
        elif i == len(ranks):
            new = rank_between(ranks[-1], None)
        else:
            new = rank_between(ranks[i-1], ranks[i])
        ranks.insert(i, new)
    # Проверка: sorted.
    assert ranks == sorted(ranks), "chain of inserts broke ordering"
