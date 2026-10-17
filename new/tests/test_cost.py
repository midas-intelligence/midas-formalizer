"""Configured model pricing through provider execution; no Lean or live calls."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from midas.application.providers import construct
from midas.domain import DeliveryCertainty, ReceiptBinding, ResultStatus, Usage
from midas.policy import Config, ModelRoute, Prices
from midas.prompt import Options, Section, Snapshot, build, researcher
from midas.provider import ModelOutput, ModelRequest, Route, worker_payload
from tests.test_provider import Artifacts, LIMITS, ROUTE, WIRE, context, issue


PRICES = Prices(2_000_000, 500_000, 6_000_000, 3_000_000)


class CostTests(unittest.TestCase):
    def setUp(self):
        self.artifacts = Artifacts()
        self.request = ModelRequest(ROUTE, 'prover', LIMITS, self.artifacts.save(WIRE))
        self.route = ModelRoute(**ROUTE.record(), prices=PRICES)
        self.output = ModelOutput(raw=b'proof', usage=Usage(100, 20, 10), cache_write_tokens=10)

    def execute(self, output, prices=PRICES):
        config = replace(Config(), prover=replace(self.route, prices=prices))
        provider = construct(config, transports={ROUTE.scope: lambda *_: output})
        invocation = issue(self.artifacts, self.request)
        result = provider.execute(invocation.request, context(self.artifacts, invocation))
        receipt = ReceiptBinding(invocation.original_execution_id, invocation.request.request_ref,
            self.artifacts.save(worker_payload(result)), self.artifacts.save(b'seal'))
        admitted = provider.admit_receipt(self.request, invocation, result, receipt, self.artifacts,
            SimpleNamespace(verify_receipt=lambda *_: True))
        self.assertEqual(admitted.outcome.usage, result.usage)
        self.assertEqual(self.artifacts.data[admitted.raw.id], output.raw)
        self.assertEqual(admitted.outcome.cache_write_tokens, output.cache_write_tokens)
        return result

    def test_cache_partition_cost_is_saved_in_worker_result_and_receipt(self):
        result = self.execute(self.output)
        self.assertEqual(result.usage, Usage(100, 20, 10, 240))
        self.assertEqual(result.status, ResultStatus.SUCCEEDED)
        self.assertIsNone(self.output.usage.cost_microusd)

    def test_missing_prices_and_required_telemetry_remain_unknown(self):
        for prices, usage, writes in (
                (None, Usage(100, 20, 10), 10),
                (PRICES, Usage(None, 20, 10), 10),
                (PRICES, Usage(100, None, 10), 10),
                (PRICES, Usage(100, 20, None), 10),
                (PRICES, Usage(100, 20, 10), None),
                (PRICES, Usage(100, 95, 10), 10),
                (Prices(2**63-1), Usage(2**63-1, 0, 0), None)):
            with self.subTest(prices=prices, usage=usage, writes=writes):
                result = self.execute(replace(self.output, usage=usage, cache_write_tokens=writes), prices)
                self.assertIsNone(result.usage.cost_microusd)

    def test_reported_cost_including_free_scripted_calls_is_preserved(self):
        for cost in (0, 1234):
            with self.subTest(cost=cost):
                output = replace(self.output, usage=replace(self.output.usage, cost_microusd=cost))
                self.assertEqual(self.execute(output).usage.cost_microusd, cost)
        self.assertEqual(self.execute(self.output, Prices()).usage.cost_microusd, 0)

    def test_partial_failure_and_cancellation_still_charge_consumed_tokens(self):
        for error in ('timeout', 'transient', 'cancelled', 'malformed_output'):
            with self.subTest(error=error):
                result = self.execute(replace(self.output, error=error, phase='reading',
                                             delivery=DeliveryCertainty.UNKNOWN))
                self.assertEqual(result.usage.cost_microusd, 240)
                self.assertNotEqual(result.status, ResultStatus.SUCCEEDED)

    def test_shared_transport_uses_the_request_role_prices(self):
        config = replace(Config(), prover=self.route,
                         reviewer=replace(self.route, prices=Prices(1_000_000, 1_000_000, 2_000_000)))
        calls = []
        def transport(request, *args):
            calls.append(request.role)
            return self.output
        provider = construct(config, transports={ROUTE.scope: transport})
        for role, cost in (('prover', 240), ('reviewer', 120)):
            request = replace(self.request, role=role)
            output = provider.invoke(request, context(self.artifacts, issue(self.artifacts, request)))
            self.assertIsNone(output.error)
            self.assertEqual(output.usage.cost_microusd, cost)
        self.assertEqual(calls, ['prover', 'reviewer'])

    def test_research_uses_its_own_prices(self):
        route = ModelRoute(provider='openai', model='model', prices=Prices(1_000_000, 100_000, 3_000_000))
        config = replace(Config(), research=route)
        prepared = build(Snapshot((Section('question', 'question', b'Find a reference.'),
                                   Section('capabilities', 'allowance', b'web_calls=1'))),
                         Options('codex', 'model'), renderer=researcher.render)
        request = ModelRequest(Route.from_config(route), 'research', replace(LIMITS, web_calls=1),
                              self.artifacts.save(prepared.wire), self.artifacts.save(prepared.setup))
        provider = construct(config, transports={request.route.scope: lambda *_: self.output})
        output = provider.invoke(request, context(self.artifacts, issue(self.artifacts, request)))
        self.assertIsNone(output.error)
        self.assertEqual(output.usage.cost_microusd, 112)

    def test_large_response_is_not_json_serialized_for_pricing(self):
        with patch('json.dumps', side_effect=AssertionError('JSON serialization')):
            result = self.execute(replace(self.output, raw=b'x'*100_000))
        self.assertEqual(result.usage.cost_microusd, 240)

    def test_invalid_transport_is_rejected_before_execution(self):
        with self.assertRaises(ValueError):
            construct(replace(Config(), prover=self.route), transports={ROUTE.scope: object()})


if __name__ == '__main__':
    unittest.main()
