"""Compare pricing and result handling with v2; no Lean or provider invocation."""

from pathlib import Path
from statistics import median
import sys
from timeit import timeit
from types import SimpleNamespace

from midas.application.providers import _priced
from midas.domain import Usage
from midas.policy import ModelConfig, Prices, cost_microusd
from midas.provider import Limits, ModelOutput


def compare(label, old, new):
    samples = [[], []]
    for iteration in range(7):
        for index in ((0, 1) if iteration % 2 else (1, 0)):
            samples[index].append(timeit((old, new)[index], number=10_000))
    old_time, new_time = map(median, samples)
    print(f'{label}: new/v2={new_time / old_time:.3f}; '
          f'v2={old_time * 100:.3f} us, new={new_time * 100:.3f} us per call')


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
    compare('Cache-aware pricing', old, new)

    # Include result validation and attaching the cost as the runtimes do.
    # The v2 fixture is small: neither side processes large JSON values.
    from midas_formalizer.model_protocol import result, validate_result
    previous = result(raw='proof', phase='terminal', certainty='response_received',
                      usage=dict(old_usage, cost_microusd=None), cache=cache)
    output = ModelOutput(raw=b'proof', usage=usage, cache_write_tokens=10)
    priced = _priced(lambda *_: output, {'prover': prices})
    request = SimpleNamespace(role='prover')
    limits = Limits.from_config(ModelConfig())
    def previous_result():
        value = validate_result(previous)
        cost = old_cost(old_prices, value['usage'], value['cache'])
        return dict(value, usage=dict(value['usage'], cost_microusd=cost),
                    diagnostics=[*value['diagnostics'], {'type': 'ConfiguredTokenPricing'}])
    def current_result():
        value = priced(request, b'', None, None, None)
        value.validate_limits(limits)
        return value
    assert previous_result()['usage']['cost_microusd'] == current_result().usage.cost_microusd == 240
    compare('Validated result with cost', previous_result, current_result)


if __name__ == '__main__':
    main()
