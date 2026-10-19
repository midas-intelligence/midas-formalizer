"""Run ownership, publication, authenticated receipts and structural recovery."""
from contextlib import contextmanager
import fcntl
from hashlib import sha256
import hmac
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4

from ..domain import ArtifactRef, ReceiptBinding, Session
from . import artifacts as archive, codec, schema, readers
from .ledger import Transaction
from .protocol import Conflict, RunLocked, StaleSession, StorageError, USAGE_FIELDS
from .queries import RunReader


def _path(directory, *, create=False):
    path = Path(directory).absolute()
    parent = path.parent if create else path
    if parent != parent.resolve(strict=True) or not parent.is_dir():
        raise StorageError('Run path must be a directory without symlink components')
    return path


def _connect(path, *, readonly, with_key=False):
    # Opening and closing the DB outside SQLite can release this process's
    # existing POSIX locks, including a concurrent writer's WAL locks.
    info = (path / 'run.sqlite3').lstat()
    if not stat.S_ISREG(info.st_mode):
        raise StorageError('Database must be a regular file')
    for suffix in ('-wal', '-shm'):
        sidecar = path / ('run.sqlite3'+suffix)
        if sidecar.exists() or sidecar.is_symlink():
            if not stat.S_ISREG(sidecar.lstat().st_mode):
                raise StorageError('Database sidecar must be a regular file')
    key = path, info.st_dev, info.st_ino
    if readonly:
        pooled = readers.take(key)
        if pooled is not None:
            return (pooled, key) if with_key else pooled
    db = sqlite3.connect((path / 'run.sqlite3').as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                         uri=True, isolation_level=None, timeout=0, check_same_thread=not readonly)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA temp_store=FILE')
    if readonly:
        db.execute('PRAGMA query_only=ON')
    return (db, key) if with_key else db


def _header(db):
    if (db.execute('PRAGMA application_id').fetchone()[0], db.execute('PRAGMA user_version').fetchone()[0]) != (schema.APPLICATION_ID, schema.SCHEMA_VERSION):
        raise StorageError('Unsupported or incomplete storage schema; create a new run')
    if db.execute('SELECT count(*) FROM run').fetchone() != (1,):
        raise StorageError('Missing run record')


def open_reader(directory):
    path = _path(directory)
    db, key = _connect(path, readonly=True, with_key=True)
    try:
        _header(db)
        reader = RunReader(path, db)
        info = (path/'run.sqlite3').lstat()
        if (path, info.st_dev, info.st_ino) != key:
            raise StorageError('Database changed while opening the reader')
        reader._pool_key = key
        return reader
    except BaseException:
        db.close()
        raise


class RunWriter(RunReader):
    def __init__(self, directory, db, lock):
        super().__init__(directory, db)
        self._lock, self._tx, self._busy = lock, None, False
        self._authority = object()
        latest = db.execute('SELECT id FROM segments ORDER BY rowid DESC LIMIT 1').fetchone()
        self._archive = archive.Segments(directory / 'artifacts', latest[0] if latest else None)
        self.session = None
        with os.fdopen(archive.regular(directory / 'receipt.key', os.O_RDONLY), 'rb') as source:
            self._secret = source.read(33)
        if len(self._secret) != 32:
            raise StorageError('Invalid receipt key')

    def _start_session(self):
        self._db.execute('BEGIN IMMEDIATE')
        try:
            self._db.execute("UPDATE sessions SET state='INTERRUPTED' WHERE state='ACTIVE'")
            self._db.execute('UPDATE run SET epoch=epoch+1,revision=revision+1')
            epoch = self._db.execute('SELECT epoch FROM run').fetchone()[0]
            self.session = Session(self.run_id, str(uuid4()), epoch)
            self._db.execute("INSERT INTO sessions VALUES (?,?,'ACTIVE')", (self.session.session_id, epoch))
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    @contextmanager
    def transaction(self, session):
        self._check()
        if self._tx is not None or self._db.in_transaction or self._busy:
            raise StorageError('Nested transactions or publication inside a transaction are forbidden')
        self._db.execute('BEGIN IMMEDIATE')
        try:
            if session != self.session or self._db.execute("SELECT id FROM sessions WHERE state='ACTIVE' AND epoch=(SELECT epoch FROM run)").fetchone() != (session.session_id,):
                raise StaleSession('Obsolete or foreign writer session')
            tx = self._tx = Transaction(self)
            yield tx
            if tx._state_changed:
                self._db.execute('UPDATE run SET revision=revision+1')
            archive.checkpoint('before_commit')
            self._db.commit()
            archive.checkpoint('after_commit')
        except BaseException:
            self._db.rollback()
            raise
        finally:
            self._tx = None

    def publish(self, source, *, max_bytes, reference=None):
        """Finalize bytes outside SQL; register the returned value in a transaction."""
        self._check()
        if self._db.in_transaction or self._busy:
            raise StorageError('Artifact I/O must occur outside write transactions')
        self._busy = True
        try:
            known_digest = None
            if self._archive.active is not None:
                saved = self._db.execute('SELECT size FROM segments WHERE id=?', (self._archive.active,)).fetchone()
                if saved and (self.directory/'artifacts'/(self._archive.active+'.seg')).stat().st_size < saved[0]:
                    raise archive.ArtifactError('Committed segment was truncated; recovery is required')
            # Anonymous immutable bytes have a stable identity. Repeated prompt
            # prefixes and evidence pages do not append duplicate archive data.
            if type(source) is bytes:
                from ..domain.records import natural
                natural(max_bytes, 'Artifact limit')
                if len(source) > max_bytes:
                    raise archive.ArtifactError('Artifact exceeds publication limit')
                digest = sha256(source).hexdigest()
                known_digest = digest
                reference = reference or archive.reference(digest, len(source))
                if (reference.sha256, reference.byte_count) != (digest, len(source)):
                    raise ValueError('Published bytes differ from reference')
                old = self._db.execute('SELECT digest,size,inline,segment,offset,stored_size FROM artifacts WHERE id=?', (reference.id,)).fetchone()
                if old is not None:
                    if old[:2] != (reference.sha256, reference.byte_count):
                        raise Conflict('Artifact ID conflicts with committed bytes')
                    for _ in self.stream_artifact(reference, max_bytes=max_bytes):
                        pass
                    stamp = self._db.execute('SELECT stamp FROM segments WHERE id=?', (old[3],)).fetchone()[0] if old[3] else None
                    return archive.Durable(reference, old[2], stamp, self._authority, old[3:] if old[3] else None)
            durable = self._archive.append(source, max_bytes=max_bytes, expected=reference,
                                           authority=self._authority, known_digest=known_digest)
            prior = self._db.execute('SELECT digest,size FROM artifacts WHERE id=?', (durable.reference.id,)).fetchone()
            if prior is not None and prior != (durable.reference.sha256, durable.reference.byte_count):
                raise Conflict('Artifact ID conflicts with committed bytes')
            return durable
        finally:
            self._busy = False

    def put(self, source, *, max_bytes, reference=None):
        durable = self.publish(source, max_bytes=max_bytes, reference=reference)
        with self.transaction(self.session) as tx:
            return tx.register(durable)

    def staging(self, *, max_bytes, max_artifacts=1024):
        self._check()
        return archive.Staging(self.directory / 'staging' / str(uuid4()), max_bytes=max_bytes, max_artifacts=max_artifacts)

    def publish_staged(self, staging, reference):
        # Copy into storage-owned files: staging remains worker-owned and cannot
        # mutate a registered archive through a hard link or open descriptor.
        chunks = staging.stream(reference)
        try:
            return self.publish(archive.ChunkReader(chunks), max_bytes=reference.byte_count, reference=reference)
        finally:
            chunks.close()

    def copy_artifact(self, reader, reference):
        chunks = reader.stream_artifact(reference, max_bytes=reference.byte_count)
        try:
            return self.put(archive.ChunkReader(chunks), max_bytes=reference.byte_count, reference=reference)
        finally:
            chunks.close()

    def _seal_bytes(self, invocation, result):
        data = codec.pack([self.run_id, invocation.original_execution_id, invocation.session.session_id,
            invocation.session.epoch, codec.request(invocation.request), codec.ref(invocation.reservation_ref)])
        return hmac.digest(self._secret, data + codec.worker_payload(result), 'sha256')

    def seal_result(self, invocation, result):
        """Controller-only authentication of executor-delivered, published bytes."""
        for ref in result.staged_artifacts:
            if self.artifact(ref.id) != ref:
                raise Conflict('Result artifact differs from published metadata')
        receipt, artifacts = self.prepare_receipt(invocation, result)
        with self.transaction(self.session) as tx:
            for artifact in artifacts:
                tx.register(artifact)
        return receipt

    def prepare_receipt(self, invocation, result):
        """Publish receipt bytes for registration with staged artifacts and receive.

        Publication is not receipt admission. Transaction.receive rechecks all
        artifact bindings and the seal after the caller registers the batch.
        """
        self._check()
        result.validate_for(invocation)
        if self.invocation(invocation.original_execution_id) != invocation:
            raise Conflict('Cannot seal a foreign invocation')
        result_artifact = self.publish(codec.worker_payload(result), max_bytes=65536)
        seal_artifact = self.publish(self._seal_bytes(invocation, result), max_bytes=32)
        receipt = ReceiptBinding(invocation.original_execution_id, invocation.request.request_ref,
                                 result_artifact.reference, seal_artifact.reference)
        return receipt, (result_artifact, seal_artifact)

    def _verify_seal(self, receipt, invocation, result):
        if receipt.original_execution_id != invocation.original_execution_id or receipt.request_ref != invocation.request.request_ref:
            raise Conflict('Receipt binding differs from invocation')
        payload = codec.worker_payload(result)
        if receipt.result_ref.byte_count != len(payload) or receipt.result_ref.sha256 != sha256(payload).hexdigest():
            raise Conflict('Receipt result differs from exact worker result')
        # Receive rechecks durable metadata and the small inline seal. Full result
        # bytes are checked by the public verifier outside the write transaction.
        seal = self.read_artifact(receipt.seal_ref, max_bytes=32)
        if not hmac.compare_digest(seal, self._seal_bytes(invocation, result)):
            raise Conflict('Receipt seal is invalid')

    def verify_receipt(self, receipt, invocation, result):
        return super().verify_receipt(receipt, invocation, result)

    def import_receipt(self, parent, execution_id):
        from .receipts import import_receipt
        return import_receipt(self, parent, execution_id)

    def validate(self, *, force_artifacts=False):
        """Structural validation only. Owning services validate saved semantics."""
        self._check()
        if self._db.in_transaction:
            raise StorageError('Integrity validation must run outside write transactions')
        for folder in ('artifacts', 'staging'):
            path = self.directory / folder
            if path.is_symlink() or not path.is_dir():
                raise StorageError('Invalid storage directory')
        _header(self._db)
        if self._db.execute('PRAGMA quick_check').fetchone() != ('ok',) or self._db.execute('PRAGMA foreign_key_check').fetchone():
            raise StorageError('Storage integrity check failed')
        epoch = self._db.execute('SELECT epoch FROM run').fetchone()[0]
        if self._db.execute('SELECT coalesce(max(epoch),0),count(*) FROM sessions').fetchone() != (epoch, epoch):
            raise StorageError('Session history differs from run epoch')
        for table, column in (('nodes', 'state'), ('intents', 'state'), ('executions', 'phase')):
            actual = dict(self._db.execute(f'SELECT {column},count(*) FROM {table} GROUP BY {column}'))
            saved = dict(self._db.execute('SELECT state,value FROM counts WHERE category=? AND value>0', (table,)))
            if actual != saved:
                raise StorageError('Indexed counts differ from committed state')
        a = self.status_snapshot().accounting
        known = self._db.execute('''SELECT coalesce(sum(u.input_tokens),0),coalesce(sum(u.cache_tokens),0),
            coalesce(sum(u.output_tokens),0),coalesce(sum(u.cost_microusd),0) FROM execution_usage u JOIN executions e ON e.id=u.execution WHERE e.charged=1''').fetchone()
        unknown = self._db.execute('''SELECT coalesce(sum(u.input_tokens IS NULL),0),coalesce(sum(u.cache_tokens IS NULL),0),
            coalesce(sum(u.output_tokens IS NULL),0),coalesce(sum(u.cost_microusd IS NULL),0) FROM executions e
            LEFT JOIN execution_usage u ON u.execution=e.id WHERE e.charged=1''').fetchone()
        if (a.turns != self._db.execute('SELECT coalesce(sum(charged),0) FROM executions').fetchone()[0]
                or known != tuple(getattr(a.known, k) for k in USAGE_FIELDS)
                or unknown != tuple(getattr(a.unknown, k) for k in USAGE_FIELDS)):
            raise StorageError('Accounting differs from original invocation ledger')
        capacity = {'MODEL': 0, 'COMPILER': 0, 'LOCAL': 0, 'NETWORK': 0}
        memory, reserved = 0, [0]*4
        for kind, footprint, bounds in self._db.execute('SELECT resource,memory,bounds FROM executions WHERE held=1'):
            capacity[kind] += 1
            memory += footprint if kind == 'COMPILER' else 0
            for i, v in enumerate(codec.unpack(bounds)):
                reserved[i] += v or 0
        if ((*capacity.values(), memory) != (a.models, a.compilers, a.locals, a.networks, a.compiler_memory_mib)
                or tuple(reserved) != tuple(getattr(a.reserved, k) for k in USAGE_FIELDS)):
            raise StorageError('Capacity differs from held reservations')
        if self._db.execute("""SELECT 1 FROM executions e LEFT JOIN receipts r ON r.execution=e.id
            WHERE (e.phase='RECEIVED')!=(r.execution IS NOT NULL) OR (e.phase='NOT_SENT' AND (e.charged OR e.held))
            OR (e.charged=1 AND e.resource!='MODEL') LIMIT 1""").fetchone():
            raise StorageError('Inconsistent execution lifecycle')
        from .integrity import validate_views
        validate_views(self)
        for (key,) in self._db.execute('SELECT execution FROM imported_receipts'):
            if not self.verify_receipt(self.receipt(key), self.invocation(key), self.result(key)):
                raise StorageError('Invalid imported receipt')
        # Changed file stamps force a full bounded stream/hash check. Unchanged
        # stamps only cache storage integrity, never semantic receipt admission.
        for key, digest, size, inline in self._db.execute('SELECT id,digest,size,inline FROM artifacts WHERE inline IS NOT NULL'):
            ref = ArtifactRef(key, digest, size)
            ref.verify(inline, max_bytes=16384)
        for segment, highwater, stamp in self._db.execute('SELECT * FROM segments'):
            from ..domain.records import uuid
            uuid(segment)
            path = self.directory / 'artifacts' / (segment+'.seg')
            current = archive.stamp(path)
            if path.stat().st_size < highwater:
                raise StorageError('Committed segment was truncated')
            previous_end = 0
            for key, digest, size, offset, stored_size in self._db.execute(
                    'SELECT id,digest,size,offset,stored_size FROM artifacts WHERE segment=? ORDER BY offset', (segment,)):
                if offset < previous_end or offset+stored_size > highwater:
                    raise StorageError('Artifact placement overlaps or exceeds committed segment')
                previous_end = offset+stored_size
                if force_artifacts or current != stamp:
                    for _ in archive.stream_file(path, ArtifactRef(key, digest, size), offset=offset, stored_size=stored_size):
                        pass

    def _recover_artifacts(self):
        """Call only after validating all committed files; collect unpublished bytes."""
        for folder in ('artifacts', 'staging'):
            path = self.directory / folder
            if path.is_symlink() or not path.is_dir():
                raise StorageError('Invalid storage directory')
        for entry in (self.directory / 'artifacts').iterdir():
            if not re.fullmatch(r'[0-9a-f-]{36}\.seg', entry.name) or entry.is_symlink() or not entry.is_file():
                raise StorageError('Unexpected archive entry')
            committed = self._db.execute('SELECT size FROM segments WHERE id=?', (entry.stem,)).fetchone()
            if committed is None:
                entry.unlink()
            elif entry.stat().st_size > committed[0]:
                fd = archive.regular(entry, os.O_RDWR)
                try:
                    os.ftruncate(fd, committed[0])
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self._db.execute('UPDATE segments SET stamp=? WHERE id=?', (archive.stamp(entry), entry.stem))
        archive.sync_directory(self.directory / 'artifacts')
        for scope in (self.directory / 'staging').iterdir():
            from ..domain.records import uuid
            uuid(scope.name)
            if scope.is_symlink() or not scope.is_dir():
                raise StorageError('Unexpected staging scope')
            for entry in scope.iterdir():
                if not re.fullmatch(r'[0-9a-f-]{36}\.(raw|tmp)', entry.name) or entry.is_symlink() or not entry.is_file():
                    raise StorageError('Unexpected staged entry')
                entry.unlink()
            scope.rmdir()
        archive.sync_directory(self.directory / 'staging')

    def close(self):
        if self._db is None:
            return
        self._check()
        if self._db.in_transaction or self._busy:
            raise StorageError('Cannot close during transaction/publication')
        try:
            if self.session is not None:
                with self.transaction(self.session):
                    self._db.execute("UPDATE sessions SET state='CLOSED' WHERE id=?", (self.session.session_id,))
        finally:
            try:
                super().close()
            finally:
                os.close(self._lock)


def create(directory):
    """Create a new run. Publish and freeze its immutable inputs before work."""
    path = _path(directory, create=True)
    path.mkdir(mode=0o700)
    db = lock = None
    try:
        lock = archive.regular(path / 'run.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name in ('artifacts', 'staging'):
            (path / name).mkdir(mode=0o700)
        with os.fdopen(archive.regular(path / 'receipt.key', os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as output:
            output.write(os.urandom(32))
            output.flush()
            os.fsync(output.fileno())
        fd = archive.regular(path / 'run.sqlite3', os.O_RDWR | os.O_CREAT | os.O_EXCL)
        os.close(fd)
        db = _connect(path, readonly=False)
        if db.execute('PRAGMA journal_mode=WAL').fetchone() != ('wal',):
            raise StorageError('SQLite WAL is required')
        db.execute('PRAGMA synchronous=FULL')
        schema.create(db)
        db.execute('INSERT INTO run(singleton,id) VALUES (1,?)', (str(uuid4()),))
        db.commit()
        for folder in (path / 'artifacts', path / 'staging', path, path.parent):
            archive.sync_directory(folder)
        writer = RunWriter(path, db, lock)
        writer._start_session()
        return writer
    except BaseException:
        if db is not None:
            db.close()
        if lock is not None:
            os.close(lock)
        raise


def claim_writer(directory, *, force_artifacts=False):
    path = _path(directory)
    lock = archive.regular(path / 'run.lock', os.O_RDWR)
    db = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunLocked('Run already has a writer') from exc
        probe = _connect(path, readonly=True)
        try:
            _header(probe)
        finally:
            probe.close()
        db = _connect(path, readonly=False)
        db.execute('PRAGMA synchronous=FULL')
        if db.execute('PRAGMA journal_mode').fetchone() != ('wal',):
            raise StorageError('SQLite WAL is required')
        writer = RunWriter(path, db, lock)
        writer.validate(force_artifacts=force_artifacts)
        writer._recover_artifacts()
        writer._start_session()
        return writer
    except BaseException:
        if db is not None:
            db.close()
        os.close(lock)
        raise
