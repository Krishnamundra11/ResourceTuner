import os

SCALE = float(os.environ.get("TEST_SCALE", "1"))


def sieve(n):
    is_p = bytearray([1]) * (n + 1)
    is_p[0:2] = b"\x00\x00"
    for i in range(2, int(n ** 0.5) + 1):
        if is_p[i]:
            is_p[i * i::i] = bytearray(len(is_p[i * i::i]))
    return [i for i, v in enumerate(is_p) if v]


def matmul(a, b):
    bt = list(zip(*b))
    return [[sum(x * y for x, y in zip(row, col)) for col in bt] for row in a]


def test_primes(case):
    n = int(200_000 * SCALE) + case
    primes = sieve(n)
    assert primes[0] == 2 and all(p % 2 for p in primes[1:])


def test_matrix_identity(case):
    size = int(60 * SCALE ** 0.5)
    a = [[(i * size + j + case) % 7 for j in range(size)] for i in range(size)]
    ident = [[int(i == j) for j in range(size)] for i in range(size)]
    assert matmul(a, ident) == a
