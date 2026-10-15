"""Compare pure cache-aware pricing with v2; no Lean or provider invocation."""

from pathlib import Path
from statistics import median
import sys
from timeit import timeit

from midas.domain import Usage
from midas.policy import Prices, cost_microusd


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from midas_formalizer.model_pricing import cost_microusd as old_cost

    old_prices = dict(input_usd_per_million_tokens=2,
                      cached_input_usd_per_million_tokens=0.5,
                      output_usd_per_million_tokens=6,
                      cache_write_input_usd_per_million_tokens=3)
    old_usage = dict(input_tokens=100, output_tokens=10)
    cache = dict(read_tokens=20, write_tokens=10)
    prices, usage = Prices(2_000_000, 500_000, 6_000_000, 3_000_000), Usage(100, 20, 10)
    old = lambda: old_cost(old_prices, old_usage, cache)
    new = lambda: cost_microusd(prices, usage, cache_write_tokens=10)
    assert old() == new() == 240
    samples = [[], []]
    for iteration in range(7):
        for index in ((0, 1) if iteration % 2 else (1, 0)):
            samples[index].append(timeit((old, new)[index], number=10_000))
    old_time, new_time = map(median, samples)
    print(f'Cache-aware pricing: new/v2={new_time / old_time:.3f}; '
          f'v2={old_time * 100:.3f} us, new={new_time * 100:.3f} us per call')


if __name__ == '__main__':
    main()
