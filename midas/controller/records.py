"""Bounded binary controller metadata and indexed policy budget projections."""
from dataclasses import fields

from ..domain import Usage
from ..domain.encoding import load_record, record_parts
from ..policy import AdmissionRequest, RecoveryConfig, Reservation, RetryHistory, RunUsage
from ..storage import CounterChange, RecordChange

META = 16384
SCOPE = 'controller'
FIELDS = ('input_tokens', 'cache_tokens', 'output_tokens', 'cost_microusd')


def pack(value):
    return b''.join(record_parts(value, limit=META))


def read(reader, ref):
    return load_record(reader.read_artifact(ref, max_bytes=META), limit=META)


def record(reader, scope, key, default=None):
    saved = reader.record(scope, key)
    return default if saved is None or saved.artifact is None else read(reader, saved.artifact)


def pointer(reader, scope, key, ref):
    old = reader.record(scope, key)
    return RecordChange(scope, key, old.revision if old else None, ref)


def delta(reader, key, amount):
    before = reader.counter(SCOPE, key)
    return CounterChange(SCOPE, key, before, before+amount)


def admission(value):
    return [[getattr(value.bounds, k) for k in FIELDS], value.downstream, value.repair_model,
            value.helper, value.helper_units]


def load_admission(job, data):
    return AdmissionRequest(job, Usage(*data[0]), data[1], data[2], new_intent=False,
                            helper=data[3], helper_units=data[4])


def reservation(value, recovery, first_ms):
    return pack(['controller-reservation-v1', admission(value.request), value.turns, value.uses_repair,
                 [getattr(recovery, f.name) for f in fields(recovery)], first_ms])


def load_reservation(reader, invocation):
    data = read(reader, invocation.reservation_ref)
    if type(data) is not list or len(data) != 6 or data[0] != 'controller-reservation-v1':
        raise ValueError('Invalid controller reservation')
    return (Reservation(load_admission(invocation.request.job, data[1]), data[2], data[3]),
            RecoveryConfig(*data[4]), data[5])


def budget_changes(reader, execution_id, budget, *, exceeded=False):
    """One invocation's delta; unknown bounds never become free budget."""
    old = record(reader, 'controller.budget', execution_id, [[0]*4, False])
    values = [getattr(budget, k) for k in FIELDS]
    counters = []
    for field, before, after in zip(FIELDS, old[0], values):
        difference = (after or 0)-(before or 0)
        if difference:
            counters.append(delta(reader, 'budget.'+field, difference))
        difference = int(after is None)-int(before is None)
        if difference:
            counters.append(delta(reader, 'unknown.'+field, difference))
    exceeded = exceeded or old[1]
    if exceeded != old[1]:
        counters.append(delta(reader, 'bound_exceeded', 1))
    return pack([values, exceeded]), tuple(counters)


def run_usage(reader, elapsed_ms):
    status = reader.status_snapshot()
    budget = Usage(*(None if reader.counter(SCOPE, 'unknown.'+k) else
                     reader.counter(SCOPE, 'budget.'+k) for k in FIELDS))
    return RunUsage(status.accounting.turns, sum(n for _, n in status.intents), elapsed_ms,
                    reader.counter(SCOPE, 'backtracks'), budget, bool(reader.counter(SCOPE, 'bound_exceeded')))


def history(reader, key, first_ms, retry_after_ms=0):
    value = record(reader, 'controller.retry', key, [first_ms, 0, 0, 0])
    return RetryHistory(*value, retry_after_ms)


def retried(previous, decision):
    return pack([previous.first_ms, previous.transport_retries+decision.transport_charge,
                 previous.unknown_retries+decision.unknown_charge,
                 previous.local_retries+decision.local_charge])


def projection(state, usage, *, terminated=False):
    values, exceeded = [], False
    for key in FIELDS:
        actual, bound = getattr(usage, key), getattr(state.bounds, key)
        exceeded |= actual is not None and bound is not None and actual > bound
        values.append((actual if actual is not None else bound) if terminated or not state.held else
                      (None if bound is None else max(actual or 0, bound)))
    return Usage(*values), exceeded
