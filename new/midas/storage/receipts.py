"""Receipt verification and authenticated checkpoint imports, without accounting."""
import hmac
import os

from ..domain import Invocation, Session
from . import artifacts, codec
from .protocol import Conflict, StorageError


def secret(reader):
    if hasattr(reader, '_secret'):
        return reader._secret
    with os.fdopen(artifacts.regular(reader.directory/'receipt.key', os.O_RDONLY), 'rb') as source:
        value = source.read(33)
    if len(value) != 32:
        raise StorageError('Invalid receipt key')
    return value


def imported(reader, execution_id):
    reader._check()
    row = reader._db.execute('SELECT payload,seal FROM imported_receipts WHERE execution=?', (execution_id,)).fetchone()
    if row is None:
        return None
    value = codec.unpack(row[0])
    invocation = Invocation(execution_id, Session(*value[2]), codec.unrequest(value[3]), codec.unref(value[4]))
    return invocation, codec.unreceipt(value[5]), row


def signature(reader, payload):
    return hmac.digest(secret(reader), b'midas-receipt-import-v1\0'+reader.run_id.encode()+payload, 'sha256')


def verify(reader, receipt, invocation, result):
    reader._check()
    try:
        result.validate_for(invocation)
        if reader.invocation(invocation.original_execution_id) != invocation or reader.receipt(invocation.original_execution_id) != receipt:
            return False
        if reader.read_artifact(receipt.result_ref, max_bytes=65536) != codec.worker_payload(result):
            return False
        saved = imported(reader, invocation.original_execution_id)
        if saved:
            payload, seal_id = saved[2]
            expected = signature(reader, payload)
            actual = reader.read_artifact(reader.artifact(seal_id), max_bytes=32)
        else:
            payload = codec.pack([reader.run_id, invocation.original_execution_id, invocation.session.session_id,
                invocation.session.epoch, codec.request(invocation.request), codec.ref(invocation.reservation_ref)])
            expected = hmac.digest(secret(reader), payload+codec.worker_payload(result), 'sha256')
            actual = reader.read_artifact(receipt.seal_ref, max_bytes=32)
        return hmac.compare_digest(actual, expected)
    except (ValueError, KeyError, StorageError):
        return False


def import_receipt(writer, parent, execution_id):
    """Authenticate a pinned parent receipt and bind it to this child checkpoint.

    The caller must copy its referenced artifacts first and perform semantic
    admission separately. Imports never become executions or usage in the child.
    """
    invocation, receipt, result = parent.invocation(execution_id), parent.receipt(execution_id), parent.result(execution_id)
    if not parent.verify_receipt(receipt, invocation, result):
        raise Conflict('Cannot import an unauthenticated receipt')
    if writer._db.execute('SELECT 1 FROM executions WHERE id=?', (execution_id,)).fetchone():
        raise Conflict('Imported receipt collides with a local execution')
    refs = (*invocation.request.job.input_refs, invocation.request.operation_payload,
        invocation.request.request_ref, invocation.reservation_ref, receipt.result_ref, receipt.seal_ref,
        *result.staged_artifacts)
    for ref in refs:
        if writer.artifact(ref.id) != ref:
            raise Conflict('Imported receipt artifact differs')
    payload = codec.pack([parent.run_id, parent.status_snapshot().revision,
        [invocation.session.run_id, invocation.session.session_id, invocation.session.epoch],
        codec.request(invocation.request), codec.ref(invocation.reservation_ref), codec.receipt(receipt)])
    old = writer._db.execute('SELECT payload FROM imported_receipts WHERE execution=?', (execution_id,)).fetchone()
    if old:
        if old[0] != payload:
            raise Conflict('Conflicting receipt import')
        return
    seal = writer.put(signature(writer, payload), max_bytes=32)
    with writer.transaction(writer.session) as tx:
        writer._db.execute('INSERT INTO imported_receipts VALUES (?,?,?)', (execution_id, payload, seal.id))
        tx._state_changed = True
