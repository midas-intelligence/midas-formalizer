"""Policy contract tests. No Lean, subprocesses, providers or database access."""

import ast
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from midas.domain import (
    ArtifactRef, DeliveryCertainty, DomainError, FrozenRequest, Invocation, Job,
    JobKind, OwnerVersion, ReceiptBinding, ResourceLimits, ResultStatus, Session,
    Termination, Usage, WorkerResult,
)
from midas.policy import (
    AdmissionRequest, BacklogState, Capacity, Config, ExportUsage, Failure,
    HelperConfig, MathematicsConfig, ModelConfig, ModelRoute, OwnerUsage, Policy, Prices, Progress,
    RecoveryConfig, RetryHistory, RunConfig, RunUsage, SchedulingConfig, Services,
    Stage, cost_microusd, load_config,
)
from midas.policy.config import MAX_CONFIG_BYTES


def uid(label):
    return str(uuid5(NAMESPACE_URL, label))


def artifact(label):
    return ArtifactRef(uid(label), sha256(label.encode()).hexdigest(), len(label))


OWNER = OwnerVersion('obl_' + '0' * 64, 0)


def request(kind=JobKind.MODEL, **kwargs):
    return AdmissionRequest(Job('work', kind, OWNER, (), (OWNER,), ResourceLimits(1000, 1, 1000, 1000)), **kwargs)


def result_for(req, *, status=ResultStatus.FAILED, delivery=DeliveryCertainty.NOT_SENT,
               usage=Usage(), termination=Termination.TERMINATED):
    frozen = FrozenRequest(req.job, 'env_' + '1' * 64, artifact('payload'), artifact('request'))
    invocation = Invocation(uid('execution'), Session(uid('run'), uid('session'), 1), frozen, artifact('reservation'))
    result = WorkerResult(invocation.original_execution_id, frozen.request_hash, status, (), None,
                          None, usage, delivery, termination)
    receipt = ReceiptBinding(invocation.original_execution_id, frozen.request_ref, artifact('result'), artifact('seal'))
    return invocation, receipt, result


class ConfigTests(unittest.TestCase):
    def test_recursion_depth_config_projects_and_round_trips(self):
        config = load_config(b'''{"format":"midas-config-v1",
            "checker":{"max_rec_depth":256}, "local":{"max_rec_depth":512},
            "export":{"max_rec_depth":2048}}''')
        self.assertEqual(load_config(config.encode()), config)
        self.assertNotEqual(config.id, Config().id)
        for name, count in (('checker', 256), ('local', 512), ('export', 2048)):
            section = getattr(config, name)
            self.assertEqual(section.limits().max_rec_depth, count)
            for invalid in (0, -1, True, 1.5, None, 2**63):
                with self.subTest(name=name, value=invalid), self.assertRaises(DomainError):
                    replace(section, max_rec_depth=invalid)
            self.assertEqual(getattr(config.increase({name: {'max_rec_depth': count + 1}}),
                                     name).max_rec_depth, count + 1)
        self.assertEqual(config.export.limits(comparator=True).max_rec_depth, 2048)
        self.assertEqual(Policy(config).export_limits(ExportUsage(elapsed_ms=100)).max_rec_depth, 2048)

    def test_heartbeat_config_projects_and_round_trips(self):
        config = load_config(b'''{"format":"midas-config-v1",
            "checker":{"max_heartbeat":12345}, "local":{"max_heartbeat":0},
            "export":{"max_heartbeat":54321}}''')
        self.assertEqual(load_config(config.encode()), config)
        self.assertNotEqual(config.id, Config().id)
        for name, count in (('checker', 12345), ('local', 0), ('export', 54321)):
            section = getattr(config, name)
            self.assertEqual(section.limits().max_heartbeat, count)
            for invalid in (-1, True, 1.5, None, 2**63):
                with self.subTest(name=name, value=invalid), self.assertRaises(DomainError):
                    replace(section, max_heartbeat=invalid)
            if count == 0:
                with self.assertRaises(DomainError):
                    config.increase({name: {'max_heartbeat': count + 1}})
            else:
                self.assertEqual(getattr(config.increase({name: {'max_heartbeat': 0}}),
                                         name).max_heartbeat, 0)
        self.assertEqual(config.export.limits(comparator=True).max_heartbeat, 54321)
        self.assertEqual(Policy(config).export_limits(ExportUsage(elapsed_ms=100)).max_heartbeat, 54321)

    def test_stop_request_allowance_and_config(self):
        config = Config()
        self.assertEqual(config.mathematics.stop_requests, 3)
        self.assertEqual(load_config(config.encode()), config)
        for limit in (0, 1, 3):
            policy = Policy(replace(config, mathematics=replace(config.mathematics, stop_requests=limit)))
            for used in range(5):
                decision = policy.stop_review(OwnerUsage(stop_requests=used))
                self.assertEqual(decision.allowed, used < limit)
                if not decision.allowed:
                    self.assertEqual(decision.reason, 'stop_request_limit_exceeded')
        with self.assertRaises(DomainError):
            replace(config.mathematics, stop_requests=-1)
        self.assertEqual(config.increase({'mathematics': {'stop_requests': 4}}).mathematics.stop_requests, 4)

    def test_codex_retry_limits_round_trip_and_remain_frozen(self):
        config = load_config(b'''{
            "format": "midas-config-v1",
            "model": {"request_max_retries": 3, "stream_max_retries": 1},
            "research_model": {"request_max_retries": 2, "stream_max_retries": 4}
        }''')
        self.assertEqual(load_config(config.encode()), config)
        defaults = Config()
        for section in ('model', 'research_model'):
            saved = getattr(config, section)
            self.assertEqual(getattr(config.increase({'run': {'turns': 401}}), section), saved)
            for name in ('request_max_retries', 'stream_max_retries'):
                self.assertEqual(getattr(getattr(defaults, section), name), 0)
                changed = replace(config, **{section: replace(saved, **{name: 0})})
                self.assertNotEqual(changed.id, config.id)
                with self.assertRaises(FrozenInstanceError):
                    setattr(saved, name, 5)
            with self.assertRaises(DomainError):
                config.increase({section: {'request_max_retries': 5}})
        self.assertEqual(config.recovery, defaults.recovery)

    def test_invalid_codex_retry_limits_rejected(self):
        for name in ('request_max_retries', 'stream_max_retries'):
            for value in (-1, True, 1.5, '2', None, 2**63):
                with self.subTest(name=name, value=value), self.assertRaises(DomainError):
                    ModelConfig(**{name: value})
            for section in ('model', 'research_model'):
                payload = ('{"format":"midas-config-v1","' + section + '":{"' + name + '":-1}}').encode()
                with self.subTest(section=section, name=name), self.assertRaises(DomainError):
                    load_config(payload)

    def test_model_routes_round_trip_and_affect_config_identity(self):
        config = load_config(b'''{
            "format": "midas-config-v1",
            "prover": {"provider": "azure", "model": "proof-deployment",
                       "transport": "azure_responses", "account": "proof-account",
                       "endpoint": "https://models.example/openai/v1/responses",
                       "credential_env": "MODEL_API_KEY",
                       "prices": {"input": 2000000, "cached_input": 500000, "output": 6000000}},
            "reviewer": {"provider": "amazon-bedrock-runtime", "model": "review-model",
                         "transport": "bedrock_responses", "prompt_cache": "explicit",
                         "prices": {"input": 1000000}},
            "research": {"provider": "custom-provider", "model": "research-model",
                         "endpoint": "http://localhost:8000/v1/responses"}
        }''')
        self.assertEqual(config.prover.provider, 'azure')
        self.assertEqual(config.prover.model, 'proof-deployment')
        self.assertEqual(config.prover.credential_env, 'MODEL_API_KEY')
        self.assertEqual(config.reviewer.transport, 'bedrock_responses')
        self.assertTrue(config.research.configured)
        self.assertEqual(config.prover.prices.input, 2000000)
        self.assertEqual(config.reviewer.prices.input, 1000000)
        self.assertEqual(config.reviewer.prompt_cache, 'explicit')
        self.assertIsNone(config.research.prices)
        self.assertEqual(load_config(config.encode()), config)
        self.assertNotEqual(replace(config, prover=replace(config.prover, model='other-model')).id, config.id)
        self.assertEqual(config.increase({'run': {'turns': 401}}).prover, config.prover)
        with self.assertRaises(DomainError):
            config.increase({'prover': {'model': 'other-model'}})
        with self.assertRaises(FrozenInstanceError):
            config.prover.model = 'other-model'

    def test_config_example_groups_models_and_prices(self):
        path = Path(__file__).resolve().parents[1] / 'config.json'
        with path.open('rb') as source:
            config = load_config(source.read(MAX_CONFIG_BYTES + 1))
        self.assertEqual(config, load_config(config.encode()))
        for name in ('prover', 'reviewer'):
            route = getattr(config, name)
            self.assertEqual((route.provider, route.model), ('azure', 'gpt-6.1-sol'))
            self.assertEqual(route.prices, Prices(2_000_000, 100_000, 10_000_000, 2_500_000))
        self.assertEqual(config.reviewer.prompt_cache, 'explicit')
        self.assertEqual(config.research, ModelRoute())
        defaults = Config()
        for name in ('prover', 'reviewer', 'research'):
            self.assertEqual(getattr(defaults, name), ModelRoute())
            self.assertFalse(getattr(defaults, name).configured)
            self.assertEqual(set(config.record()[name]),
                             {'provider', 'model', 'transport', 'account', 'endpoint', 'credential_env',
                              'prompt_cache', 'prices', 'input_token_budget', 'reasoning_effort'})

    def test_reasoning_effort_is_saved_and_validated(self):
        config = Config(prover=ModelRoute(provider='azure', model='fixture', reasoning_effort='high'))
        self.assertEqual(load_config(config.encode()), config)
        self.assertNotEqual(config.id, replace(config, prover=replace(config.prover, reasoning_effort=None)).id)
        for value in ('invalid', '', True, 5):
            with self.subTest(value=value), self.assertRaises(DomainError):
                ModelRoute(provider='azure', model='fixture', reasoning_effort=value)
        with self.assertRaises(DomainError):
            ModelRoute(reasoning_effort='high')
        with self.assertRaises(DomainError):
            ModelRoute(provider='openai', model='fixture', reasoning_effort='high')

    def test_nested_prices_preserve_unknown_free_and_frozen_rates(self):
        config = Config(prover=ModelRoute(provider='provider', model='model'))
        self.assertIsNone(config.prover.prices)
        self.assertIsNone(cost_microusd(config.prover.prices, Usage(10, 0, 10)))
        free = replace(config, prover=replace(config.prover, prices=Prices()))
        self.assertEqual(cost_microusd(free.prover.prices, Usage(10, 0, 10)), 0)
        self.assertNotEqual(config.id, free.id)
        self.assertEqual(load_config(free.encode()), free)
        changed = replace(free, prover=replace(free.prover, prices=Prices(input=1)))
        self.assertNotEqual(free.id, changed.id)
        self.assertEqual(free.prover.prices.input, 0)
        with self.assertRaises(FrozenInstanceError):
            free.prover.prices.input = 1
        for prices in (None, Prices(), Prices(2, 1, 3, 4)):
            configured = replace(config, prover=replace(config.prover, prices=prices))
            self.assertEqual(load_config(configured.encode()), configured)

    def test_malformed_nested_prices_and_detached_prices_are_rejected(self):
        for value in (b'1', b'[]', b'{"input":true}', b'{"input":-1}', b'{"input":1.5}',
                      b'{"input":9223372036854775808}', b'{"input":NaN}', b'{"unknown":1}',
                      b'{"input":1,"input":2}', b'{"input":{"rate":1}}'):
            payload = (b'{"format":"midas-config-v1","prover":{"provider":"p","model":"m","prices":'
                       + value + b'}}')
            with self.subTest(value=value), self.assertRaises(DomainError):
                load_config(payload)
        for name in ('prover_prices', 'reviewer_prices', 'research_prices'):
            payload = b'{"format":"midas-config-v1","' + name.encode() + b'":null}'
            with self.subTest(name=name), self.assertRaises(DomainError):
                load_config(payload)
        with self.assertRaises(DomainError):
            ModelRoute(provider='provider', model='model', prices={'input': 1})

    def test_invalid_or_incomplete_model_routes_rejected(self):
        for changes in ({'provider': None}, {'model': None}, {'provider': ''}, {'model': True},
                        {'model': ' '}, {'model': 'model\n'}, {'model': '\ud800'}, {'model': 'x' * 257},
                        {'model': '😀' * 65}, {'transport': 1}, {'account': []},
                        {'prompt_cache': 'automatic'},
                        {'credential_env': 'not-an-env-name'}, {'credential_env': '$API_KEY'},
                        {'endpoint': 'file:///tmp/model'}, {'endpoint': 'https:///responses'},
                        {'endpoint': 'https://user:password@example.com'},
                        {'endpoint': 'https://example.com?api_key=secret'},
                        {'endpoint': 'https://example.com#fragment'},
                        {'endpoint': 'https://example.com:99999'},
                        {'endpoint': 'https://example.com/bad path'}):
            with self.subTest(changes=changes), self.assertRaises(DomainError):
                ModelRoute(**({'provider': 'provider', 'model': 'model'} | changes))
        with self.assertRaises(DomainError):
            ModelRoute(credential_env='API_KEY')
        for payload in (
            b'{"format":"midas-config-v1","prover":null}',
            b'{"format":"midas-config-v1","prover":{"provider":"provider"}}',
            b'{"format":"midas-config-v1","prover":{"api_key":"secret"}}',
            b'{"format":"midas-config-v1","prover":{"provider":"a","provider":"b","model":"m"}}',
            b'{"format":"midas-config-v1","prover":{"provider":"p","model":{"name":"m"}}}',
        ):
            with self.subTest(payload=payload), self.assertRaises(DomainError):
                load_config(payload)

    def test_single_shallow_config_defaults_round_trip_and_dynamic_windows(self):
        config = load_config(b'{"format":"midas-config-v1", "scheduling":{"model_concurrency":4}}')
        self.assertEqual(config.scheduling.backlog_new_stop, 84)
        self.assertEqual(config.scheduling.backlog_all_stop, 104)
        self.assertEqual(config.research_model.invocation_ms, 300_000)
        self.assertEqual(load_config(config.encode()), config)
        self.assertLess(len(config.encode()), MAX_CONFIG_BYTES)
        with self.assertRaises(FrozenInstanceError):
            config.run.turns = 1
        policy = Policy(config)
        with self.assertRaises(FrozenInstanceError):
            policy.config = Config()
        self.assertNotEqual(config.id, Config().id)

    def test_unknown_duplicate_invalid_and_nested_settings_rejected(self):
        cases = (
            b'{"format":"midas-config-v1","run":{"turns":true}}',
            b'{"format":"midas-config-v1","run":{"turns":-1}}',
            b'{"format":"midas-config-v1","run":{"runtime_ms":NaN}}',
            b'{"format":"midas-config-v1","run":{"runtime_ms":1e999}}',
            b'{"format":"midas-config-v1","run":{"runtime_ms":1.5}}',
            b'{"format":"midas-config-v1","unknown":{}}',
            b'{"format":"midas-config-v1","run":{"unknown":1}}',
            b'{"format":"midas-config-v1","run":{},"run":{}}',
            b'{"format":"midas-config-v1","run":{"turns":1,"turns":2}}',
            b'{"format":"midas-config-v1","run":{"turns":{}}}',
            b'{"format":"midas-config-v1","environment":{"toolchain":"x"}}',
            b'{"format":"midas-config-v1","run":null}',
            b'{"format":"midas-config-v1","checker":{"memory_mib":8192}}',
            b'{"format":"midas-config-v1","recovery":{"unknown_retries":3}}',
            b'{"format":"midas-config-v1","model":{"output_bytes":17000000}}',
            b'{"format":"midas-config-v1","scheduling":{"backlog_low":99}}',
        )
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                load_config(payload)

    def test_large_or_deep_input_rejected_before_json(self):
        with patch('midas.policy.config.json.loads', side_effect=AssertionError('JSON called')):
            for payload in (b'x' * (MAX_CONFIG_BYTES + 1), b'[[[[0]]]]',
                            b'{"prover":{"prices":{"input":{"rate":1}}}}'):
                with self.assertRaises(DomainError):
                    load_config(payload)

    def test_increases_are_prospective_and_do_not_reset_accounting(self):
        old = Config()
        new = old.increase({'run': {'turns': 401}, 'mathematics': {'direct': 9, 'protocol': 3}})
        self.assertEqual(old.run.turns, 400)
        self.assertEqual(new.run.turns, 401)
        self.assertTrue(Policy(new)._budget_reason(Usage(), RunUsage(turns=400)) is None)
        for change in ({}, {'run': {'turns': 399}}, {'run': {'turns': 400}},
                       {'helpers': {'research': 100}}, {'run': {'max_input_tokens': 5}}):
            with self.subTest(change=change), self.assertRaises(DomainError):
                old.increase(change)

    def test_resource_increases_validate_pool_profiles_and_decreases(self):
        old = Config()
        changes = {'checker': {'memory_mib': 8192, 'wall_ms': 480000},
                   'export': {'memory_mib': 8192, 'timeout_ms': 480000},
                   'scheduling': {'compiler_memory_mib': 16384, 'compiler_concurrency': 2}}
        new = old.increase(changes)
        self.assertEqual(load_config(new.encode()), new)
        self.assertEqual(new.checker.limits().memory_mib, 8192)
        self.assertEqual(new.export.limits().wall_ms, 480000)
        self.assertEqual(old.checker.memory_mib, 4096)
        for change in ({'checker': {'memory_mib': 8192}},
                       {'export': {'timeout_ms': 1}},
                       {'checker': {'wall_ms': 86400001}},
                       {'scheduling': {'compiler_concurrency': 1025}},
                       {'scheduling': {'backlog_low': 5}},
                       {'local': {'memory_mib': True}}):
            with self.subTest(change=change), self.assertRaises(DomainError):
                old.increase(change)

    def test_policy_import_boundary(self):
        package = Path(__file__).resolve().parents[1] / 'midas' / 'policy'
        for path in package.glob('*.py'):
            for statement in ast.walk(ast.parse(path.read_text())):
                if isinstance(statement, ast.Import):
                    for alias in statement.names:
                        self.assertIn(alias.name.split('.')[0], sys.stdlib_module_names)
                        self.assertNotIn(alias.name, ('sqlite3', 'subprocess'))
                elif isinstance(statement, ast.ImportFrom):
                    if statement.level == 2:
                        self.assertEqual(statement.module.split('.')[0], 'domain')
                    elif statement.level == 0:
                        self.assertIn(statement.module.split('.')[0], sys.stdlib_module_names)
                    else:
                        self.assertEqual(statement.level, 1)


class AdmissionTests(unittest.TestCase):
    def test_recursion_depth_cannot_exceed_the_configured_profile(self):
        config = Config()
        for profile, kind in (('checker', JobKind.CHECK_PROOF), ('checker', JobKind.CHECK_BINDING),
                              ('checker', JobKind.PROBE), ('export', JobKind.EXPORT_VERIFY)):
            policy = Policy(replace(config, **{profile: replace(getattr(config, profile), max_rec_depth=256)}))
            for count in (1, 256, 257):
                req = request(kind)
                req = replace(req, job=replace(req.job, limits=replace(req.job.limits, max_rec_depth=count)))
                decision = policy.admit(req, RunUsage(), OwnerUsage(), Capacity(), Services())
                with self.subTest(kind=kind, count=count):
                    self.assertEqual(decision.allowed, count <= 256)
                    if not decision.allowed:
                        self.assertEqual(decision.reason, 'process_resource_limit')

    def test_heartbeat_admission_respects_finite_and_unlimited_profiles(self):
        config = Config()
        for profile, kind in (('checker', JobKind.CHECK_PROOF), ('checker', JobKind.PROBE),
                              ('export', JobKind.EXPORT_VERIFY)):
            for maximum in (0, 100):
                policy = Policy(replace(config, **{profile: replace(getattr(config, profile), max_heartbeat=maximum)}))
                for count in (0, 100, 101):
                    req = request(kind)
                    req = replace(req, job=replace(req.job, limits=replace(req.job.limits, max_heartbeat=count)))
                    decision = policy.admit(req, run=RunUsage(), owner=OwnerUsage(), capacity=Capacity(), services=Services())
                    allowed = maximum == 0 or 0 < count <= maximum
                    with self.subTest(kind=kind, maximum=maximum, count=count):
                        self.assertEqual(decision.allowed, allowed)
                        if not allowed:
                            self.assertEqual(decision.reason, 'process_resource_limit')

    def setUp(self):
        self.policy = Policy()

    def admit(self, req=None, **kwargs):
        args = dict(run=RunUsage(), owner=OwnerUsage(), capacity=Capacity(), services=Services())
        args.update(kwargs)
        return self.policy.admit(req or request(), **args)

    def test_admission_reserves_turns_and_rechecks_pending_capacity(self):
        first = self.admit()
        self.assertTrue(first.allowed)
        self.assertEqual(first.reservation.turns, 1)
        self.assertEqual(self.admit(capacity=Capacity(models=2)).reason, 'model_capacity')
        self.assertEqual(self.admit(run=RunUsage(turns=400)).reason, 'model_turn_budget')
        self.assertEqual(self.admit(run=RunUsage(intents=10_000)).reason, 'lifetime_intent_allowance_exhausted')
        self.assertTrue(self.admit(request(new_intent=False), run=RunUsage(intents=10_000)).allowed)
        self.assertEqual(self.admit(request(dependencies_ready=False)).reason, 'waiting_dependency')
        self.assertEqual(self.admit(run=RunUsage(elapsed_ms=7_200_000)).reason, 'run_deadline')

    def test_all_compiler_operations_share_capacity_and_retained_memory(self):
        for kind in JobKind:
            if kind in (JobKind.MODEL, JobKind.SEARCH, JobKind.NETWORK_SEARCH):
                continue
            with self.subTest(kind=kind):
                req = request(kind)
                self.assertTrue(self.admit(req).allowed)
                self.assertEqual(self.admit(req, capacity=Capacity(compilers=1)).reason, 'compiler_capacity')
                self.assertEqual(self.admit(req, capacity=Capacity(retained_memory_mib=4096)).reason, 'compiler_capacity')
                self.assertEqual(self.admit(req, services=Services(compiler=False)).reason, 'compiler_not_configured')
        req = request(JobKind.CHECK_PROOF)
        req = replace(req, job=replace(req.job, limits=replace(req.job.limits, memory_mib=4097)))
        self.assertEqual(self.admit(req).reason, 'impossible_memory')
        self.assertEqual(self.admit(request(JobKind.SEARCH), capacity=Capacity(locals=1)).reason, 'compiler_capacity')

    def test_source_search_shares_cpu_but_remote_search_has_separate_capacity(self):
        for kind in (JobKind.SEARCH, JobKind.CHECK_PROOF):
            for capacity in (Capacity(compilers=1), Capacity(locals=1)):
                self.assertEqual(self.admit(request(kind), capacity=capacity).reason, 'compiler_capacity')
            self.assertTrue(self.admit(request(kind), capacity=Capacity(networks=2)).allowed)
        remote = request(JobKind.NETWORK_SEARCH)
        self.assertTrue(self.admit(remote, capacity=Capacity(compilers=1, locals=1)).allowed)
        self.assertEqual(self.admit(remote, capacity=Capacity(networks=2)).reason, 'network_capacity')
        self.assertTrue(self.admit(request(JobKind.SEARCH), capacity=Capacity(retained_memory_mib=4096)).allowed)

    def test_backlog_hysteresis_and_gradual_reopening(self):
        state = self.policy.tick(72)
        self.assertTrue(state.repair_closed and state.new_closed)
        self.assertEqual(self.admit(backlog_state=state).reason, 'compiler_backlog')
        state = self.policy.tick(10, state)
        self.assertTrue(state.new_closed)
        state = self.policy.tick(0, state)
        self.assertEqual(state.model_allowance, 1)
        self.assertTrue(state.ramping)
        self.assertEqual(self.admit(capacity=Capacity(models=1), backlog_state=state).reason, 'model_capacity')
        self.assertEqual(self.policy.tick(0, state).model_allowance, 2)
        state = self.policy.tick(52)
        self.assertFalse(state.repair_closed)
        self.assertTrue(self.admit(request(repair_model=True), backlog_state=state).allowed)
        self.assertEqual(self.admit(request(downstream=52)).reason, 'impossible_downstream')
        self.assertEqual(self.admit(request(downstream=10), capacity=Capacity(backlog=42)).reason,
                         'projected_compiler_backlog')

    def test_usage_bounds_allow_exact_cap_and_preserve_unknown(self):
        self.policy = Policy(replace(Config(), run=RunConfig(max_input_tokens=100, max_cost_microusd=50)))
        req = request(bounds=Usage(20, None, 10, 10))
        self.assertTrue(self.admit(req, run=RunUsage(budget=Usage(80, None, 0, 40))).allowed)
        self.assertEqual(self.admit(req, run=RunUsage(budget=Usage(81, None, 0, 40))).reason,
                         'usage_budget_exhausted:input_tokens')
        self.assertEqual(self.admit(req, run=RunUsage(budget=Usage())).reason, 'unknown_usage_bound:input_tokens')
        self.assertEqual(self.admit(request()).reason, 'unknown_usage_bound:input_tokens')
        self.assertEqual(self.admit(req, run=RunUsage(bound_exceeded=True)).reason, 'usage_bound_exceeded')

    def test_helper_display_and_admission_use_same_rules(self):
        services = Services(research=True, research_web_calls=8)
        for owner in (OwnerUsage(), OwnerUsage(research=7, retrieval=10, probe=6)):
            modes = self.policy.capabilities(owner, services).available_modes
            for mode in ('research', 'retrieve', 'probe', 'page'):
                self.assertEqual(mode in modes, self.policy.helper(mode, owner, services).allowed)
        self.assertEqual(self.policy.capabilities(OwnerUsage(), services).web_calls, 8)
        self.assertEqual(self.admit(request(helper='research'), services=services, owner=OwnerUsage(research=7)).reason,
                         'research_allowance_exhausted')
        self.assertEqual(self.admit(request(JobKind.PROBE, helper='probe', helper_units=3),
                                   owner=OwnerUsage(probe=4)).reason, 'probe_allowance_exhausted')
        self.assertNotIn('research', self.policy.capabilities(OwnerUsage(), Services()).available_modes)
        self.assertEqual(self.policy.helper('retrieve', OwnerUsage(), Services(compiler=False)).reason,
                         'compiler_not_configured')

    def test_retrieval_is_the_only_search_and_lookup_helper(self):
        modes = self.policy.capabilities(OwnerUsage(), Services()).available_modes
        for mode, kind in (('search', JobKind.SEARCH), ('lookup', JobKind.LOOKUP), ('recall', JobKind.LOOKUP)):
            self.assertNotIn(mode, modes)
            with self.assertRaisesRegex(DomainError, 'Unknown helper mode'):
                self.policy.helper(mode, OwnerUsage(), Services())
            with self.assertRaisesRegex(DomainError, 'Unknown helper mode'):
                request(kind, helper=mode)
        for kind in (JobKind.SEARCH, JobKind.NETWORK_SEARCH, JobKind.LOOKUP):
            self.assertTrue(self.admit(request(kind, helper='retrieve')).allowed)

    def test_configured_allowances_drive_capabilities_and_resource_admission(self):
        config = replace(Config(), helpers=HelperConfig(research=1, retrieval=0, probe=2, web_calls=3),
                         mathematics=MathematicsConfig(direct=0))
        self.policy = Policy(config)
        services = Services(research=True, research_web_calls=8)
        caps = self.policy.capabilities(OwnerUsage(), services)
        self.assertEqual(caps.available_modes, ('research', 'probe', 'page'))
        self.assertEqual((caps.direct_remaining, caps.web_calls), (0, 3))
        self.assertFalse(self.admit(request(candidate_mode='direct')).allowed)
        req = request()
        req = replace(req, job=replace(req.job, limits=replace(req.job.limits, output_bytes=config.model.output_bytes+1)))
        self.assertEqual(self.admit(req).reason, 'model_resource_limit')
        req = request(JobKind.CHECK_PROOF)
        req = replace(req, job=replace(req.job, limits=replace(req.job.limits, wall_ms=90_001)))
        self.assertEqual(self.admit(req).reason, 'process_resource_limit')

    def test_candidate_repair_and_backtrack_rules(self):
        exhausted = OwnerUsage(direct=7, decomposition=7)
        self.assertEqual(self.admit(request(candidate_mode='direct'), owner=exhausted).reason, 'direct_allowance_exhausted')
        grant = replace(exhausted, repair_open=True, repair_remaining=2)
        self.assertTrue(self.admit(request(candidate_mode='direct'), owner=grant).reservation.uses_repair)
        self.assertFalse(self.admit(request(candidate_mode='direct'), owner=replace(grant, repair_suspended=True)).allowed)
        self.assertEqual(self.admit(request(candidate_mode='direct'), owner=OwnerUsage(pending_candidate=True)).reason,
                         'mathematical_candidate_pending')
        for owner, run, reason in ((grant, RunUsage(), 'repair_grant_open'),
                                  (OwnerUsage(backtracks=3), RunUsage(), 'owner_backtrack_limit'),
                                  (OwnerUsage(), RunUsage(backtracks=12), 'run_backtrack_limit')):
            self.assertEqual(self.policy.backtrack(owner, run).reason, reason)
        self.assertFalse(self.policy.revision('memory', OwnerUsage()).allowed)
        self.assertFalse(self.policy.revision('timeout', OwnerUsage(timeout=2)).allowed)
        for outcome in ('stale', 'cancelled', 'abandoned', 'infrastructure', 'exception'):
            self.assertEqual(self.policy.mathematical_charge(outcome), 0)
        self.assertEqual(self.policy.mathematical_charge('rejection'), 1)

    def test_provider_gate_and_export_residual_limits(self):
        gate = self.policy.provider_gate(now_ms=1000, recent_failures=3, saved_recovery=RecoveryConfig())
        self.assertEqual(gate.not_before_ms, 61_000)
        self.assertEqual(self.admit(capacity=Capacity(provider_not_before_ms=gate.not_before_ms)).reason, 'provider_cooldown')
        unavailable = self.policy.provider_gate(now_ms=1000, recent_failures=1, probe_failed=True, saved_recovery=RecoveryConfig())
        self.assertTrue(unavailable.unavailable)
        residual = self.policy.export_limits(ExportUsage(elapsed_ms=7_199_900, captured_bytes=1000))
        self.assertEqual(residual.wall_ms, 100)
        self.assertEqual(residual.output_bytes, 8 * 1024**2 - 1000)
        self.assertEqual(self.policy.export_limits(ExportUsage(
            artifact_bytes=self.policy.config.export.max_bytes-100)).evidence_bytes, 100)
        for used in (ExportUsage(cancelled=True), ExportUsage(elapsed_ms=7_200_000),
                     ExportUsage(captured_bytes=8 * 1024**2), ExportUsage(artifact_bytes=16 * 1024**3 + 1)):
            with self.assertRaises(DomainError):
                self.policy.export_limits(used)


class AccountingTests(unittest.TestCase):
    def test_cache_pricing_partition_rounding_and_unknown_usage(self):
        prices = Prices(2_000_000, 500_000, 6_000_000, 3_000_000)
        self.assertEqual(cost_microusd(prices, Usage(100, 20, 10), cache_write_tokens=10), 240)
        self.assertEqual(cost_microusd(Prices(1, 1, 1), Usage(1, None, 1)), 1)
        self.assertIsNone(cost_microusd(prices, Usage(100, None, 10), cache_write_tokens=0))
        self.assertIsNone(cost_microusd(prices, Usage(100, 20, 10)))
        self.assertIsNone(cost_microusd(prices, Usage(100, 95, 10), cache_write_tokens=10))
        self.assertIsNone(cost_microusd(None, Usage(1, 0, 1)))
        self.assertIsNone(cost_microusd(Prices(2**63 - 1), Usage(2**63 - 1, 0, 0)))

    def test_not_sent_refunds_unknown_retains_bounds_and_duplicate_is_idempotent(self):
        policy = Policy()
        req = request(bounds=Usage(100, None, 200, 300))
        reservation = policy.admit(req, RunUsage(), OwnerUsage(), Capacity(), Services()).reservation
        for delivery, expected_turns in ((DeliveryCertainty.NOT_SENT, 0), (DeliveryCertainty.UNKNOWN, 1)):
            invocation, receipt, result = result_for(req, delivery=delivery)
            settled = policy.settle(reservation, invocation, receipt, result)
            self.assertEqual(settled.turns, expected_turns)
            self.assertEqual(settled.usage, Usage() if expected_turns else Usage(0, 0, 0, 0))
            self.assertEqual(settled.budget.input_tokens, 100 if expected_turns else 0)
            self.assertIsNone(policy.settle(reservation, invocation, receipt, result, previous=settled))
            with self.assertRaises(DomainError):
                policy.settle(reservation, invocation, replace(receipt, result_ref=artifact('other')), result, previous=settled)

    def test_exact_binding_termination_and_overrun_accounting(self):
        policy = Policy()
        req = request(bounds=Usage(100, None, 100, 100))
        reservation = policy.admit(req, RunUsage(), OwnerUsage(), Capacity(), Services()).reservation
        invocation, receipt, result = result_for(req, delivery=DeliveryCertainty.UNKNOWN,
            usage=Usage(101, None, 50, None), termination=Termination.UNKNOWN)
        settled = policy.settle(reservation, invocation, receipt, result)
        self.assertFalse(settled.release_capacity)
        self.assertEqual(settled.exceeded_bounds, ('input_tokens',))
        self.assertEqual(settled.budget.input_tokens, 101)
        self.assertEqual(settled.budget.output_tokens, 100)
        self.assertIsNone(settled.usage.cost_microusd)
        with self.assertRaises(DomainError):
            policy.settle(reservation, invocation, receipt, replace(result, request_hash='0' * 64))
        with self.assertRaises(DomainError):
            policy.settle(reservation, invocation, replace(receipt, request_ref=artifact('wrong')), result)
        with self.assertRaises(DomainError):
            policy.settle(reservation, invocation, receipt, replace(result, delivery_certainty=DeliveryCertainty.NOT_SENT))

    def test_unterminated_not_sent_work_keeps_its_reservation(self):
        policy = Policy()
        req = request(bounds=Usage(100, None, 100, 100))
        reservation = policy.admit(req, RunUsage(), OwnerUsage(), Capacity(), Services()).reservation
        invocation, receipt, result = result_for(req, termination=Termination.UNKNOWN)
        settled = policy.settle(reservation, invocation, receipt, result)
        self.assertEqual(settled.turns, 1)
        self.assertEqual(settled.budget.input_tokens, 100)
        self.assertFalse(settled.release_capacity)


class RetryAndCompletionTests(unittest.TestCase):
    def retry(self, *, kind=JobKind.MODEL, delivery=DeliveryCertainty.NOT_SENT,
              termination=Termination.TERMINATED, failure=Failure.TRANSPORT, **kwargs):
        req = request(kind)
        invocation, _, result = result_for(req, delivery=delivery, termination=termination,
            status=ResultStatus.CANCELLED if failure == Failure.CANCELLED else ResultStatus.FAILED)
        policy = Policy()
        reservation = policy.admit(req, RunUsage(), OwnerUsage(), Capacity(), Services()).reservation
        args = dict(history=RetryHistory(), run=RunUsage(), now_ms=1000, saved_recovery=RecoveryConfig())
        args.update(kwargs)
        return policy.retry(reservation, invocation, result, failure, **args)

    def test_unknown_delivery_requires_saved_explicit_allowance(self):
        self.assertTrue(self.retry().eligible)
        self.assertEqual(self.retry(delivery=DeliveryCertainty.UNKNOWN).reason, 'unknown_outcome_policy')
        allowed = self.retry(delivery=DeliveryCertainty.UNKNOWN, saved_recovery=RecoveryConfig(unknown_retries=1))
        self.assertTrue(allowed.eligible)
        self.assertEqual((allowed.turn_charge, allowed.transport_charge, allowed.unknown_charge), (1, 1, 1))
        self.assertTrue(self.retry(delivery=DeliveryCertainty.DELIVERED, failure=Failure.RATE_LIMIT).eligible)
        self.assertEqual(self.retry(history=RetryHistory(transport_retries=2)).reason, 'provider_retry_exhausted')

    def test_retry_rejects_wrong_request_and_false_cancellation(self):
        policy = Policy()
        req = request()
        reservation = policy.admit(req, RunUsage(), OwnerUsage(), Capacity(), Services()).reservation
        invocation, _, result = result_for(req)
        for report, failure in ((replace(result, request_hash='0' * 64), Failure.TRANSPORT),
                                (result, Failure.CANCELLED)):
            with self.assertRaises(DomainError):
                policy.retry(reservation, invocation, report, failure, RetryHistory(), RunUsage(),
                             now_ms=1000, saved_recovery=RecoveryConfig(), resume=True)

    def test_cancel_resume_resource_replay_and_mathematical_revision_are_distinct(self):
        self.assertEqual(self.retry(failure=Failure.CANCELLED).reason, 'cancelled')
        resumed = self.retry(failure=Failure.CANCELLED, resume=True, delivery=DeliveryCertainty.UNKNOWN)
        self.assertTrue(resumed.eligible)
        self.assertEqual((resumed.turn_charge, resumed.transport_charge, resumed.unknown_charge), (1, 0, 0))
        self.assertEqual(self.retry(failure=Failure.REJECTION).reason, 'revision_required')
        depth = self.retry(kind=JobKind.CHECK_PROOF, failure=Failure.RECURSION_DEPTH)
        self.assertEqual(depth, self.retry(kind=JobKind.CHECK_PROOF, failure=Failure.TIMEOUT))
        self.assertEqual(depth, self.retry(kind=JobKind.CHECK_PROOF, failure=Failure.MEMORY))
        self.assertEqual(self.retry(kind=JobKind.CHECK_PROOF, failure=Failure.RECURSION_DEPTH,
            history=RetryHistory(local_retries=2)).reason, 'local_retry_exhausted')
        self.assertEqual(self.retry(failure=Failure.PROTOCOL).reason, 'revision_required')
        self.assertEqual(self.retry(termination=Termination.UNKNOWN).reason, 'termination_unconfirmed')
        local = self.retry(kind=JobKind.CHECK_PROOF, failure=Failure.MEMORY)
        self.assertTrue(local.eligible)
        self.assertEqual((local.turn_charge, local.local_charge), (0, 1))
        self.assertEqual(self.retry(kind=JobKind.CHECK_PROOF, history=RetryHistory(local_retries=2)).reason, 'local_retry_exhausted')

    def test_retry_deadlines_backoff_budget_and_clock(self):
        delayed = self.retry(history=RetryHistory(transport_retries=1, retry_after_ms=5000), jitter=0.5)
        self.assertEqual(delayed.earliest_ms, 6000)
        self.assertEqual(self.retry(history=RetryHistory(retry_after_ms=300_001)).reason, 'provider_retry_after_limit')
        self.assertEqual(self.retry(now_ms=1_919_999).reason, 'retry_deadline')
        self.assertEqual(self.retry(run=RunUsage(elapsed_ms=7_199_999)).reason, 'run_deadline')
        self.assertEqual(self.retry(run=RunUsage(turns=400)).reason, 'model_turn_budget')
        self.assertEqual(self.retry(history=RetryHistory(first_ms=1001)).reason, 'clock_reconciliation_required')

    def test_completion_requires_all_stages_and_terminated_work(self):
        policy = Policy()
        proof = Progress(roots=1, verified_roots=1)
        self.assertEqual(policy.completion(proof).outcome, 'finalization_pending')
        integrated = replace(proof, integration=Stage.VERIFIED)
        self.assertNotEqual(policy.completion(integrated).exit_code, 0)
        exported = replace(integrated, export_verification=Stage.VERIFIED)
        self.assertEqual(policy.completion(exported).exit_code, 0)
        self.assertIsNone(policy.completion(replace(exported, active=1)).exit_code)
        self.assertEqual(policy.completion(replace(integrated, export_verification=Stage.FAILED)).outcome, 'finalization_failed')
        with self.assertRaises(DomainError):
            Progress(roots=1, export_verification=Stage.VERIFIED)
        self.assertEqual(policy.completion(Progress(roots=1, reasons=('direct_allowance_exhausted',))).reasons,
                         ('direct_allowance_exhausted',))
        self.assertEqual(policy.completion(Progress(roots=1, stopped=True)).exit_code, 130)
        self.assertEqual(policy.completion(Progress(roots=1, recovery_required=True)).outcome, 'recovery_required')
        self.assertEqual(policy.completion(Progress(roots=1, ready=1)).outcome, 'running')


if __name__ == '__main__':
    unittest.main()
