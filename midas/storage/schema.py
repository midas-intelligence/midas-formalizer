"""The complete SQLite schema; no component outside storage owns SQL."""
APPLICATION_ID = 0x4D494453
SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE run (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL UNIQUE,
    epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch>=0), revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
    graph_revision INTEGER NOT NULL DEFAULT 0 CHECK(graph_revision>=0),
    environment_id TEXT, inputs TEXT REFERENCES artifacts(id), configuration TEXT REFERENCES artifacts(id),
    parent_id TEXT, parent_revision INTEGER,
    CHECK((environment_id IS NULL)=(inputs IS NULL)), CHECK((inputs IS NULL)=(configuration IS NULL)),
    CHECK((parent_id IS NULL)=(parent_revision IS NULL))
) STRICT;
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, epoch INTEGER NOT NULL UNIQUE CHECK(epoch>0),
    state TEXT NOT NULL CHECK(state IN ('ACTIVE','CLOSED','INTERRUPTED'))
) STRICT;
CREATE UNIQUE INDEX active_session ON sessions(state) WHERE state='ACTIVE';
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY, digest TEXT NOT NULL CHECK(length(digest)=64), size INTEGER NOT NULL CHECK(size>=0),
    inline BLOB, segment TEXT REFERENCES segments(id), offset INTEGER, stored_size INTEGER,
    CHECK((size<=16384 AND inline IS NOT NULL AND length(inline)=size AND segment IS NULL AND offset IS NULL AND stored_size IS NULL)
       OR (size>16384 AND inline IS NULL AND segment IS NOT NULL AND offset>=0 AND stored_size>8))
) STRICT;
CREATE TABLE segments (id TEXT PRIMARY KEY, size INTEGER NOT NULL CHECK(size>8), stamp BLOB NOT NULL) STRICT;
CREATE INDEX artifact_placement ON artifacts(segment,offset);
CREATE TABLE nodes (
    id TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>=0),
    revision INTEGER NOT NULL CHECK(revision>=0), state TEXT NOT NULL CHECK(state IN ('UNRESOLVED','CONDITIONAL','VERIFIED')),
    interface TEXT NOT NULL REFERENCES artifacts(id), selected TEXT UNIQUE,
    FOREIGN KEY(id,revision) REFERENCES node_history(owner,revision) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE INDEX nodes_state ON nodes(state,id);
CREATE TABLE node_history (
    owner TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0), payload BLOB NOT NULL,
    PRIMARY KEY(owner,revision)
) STRICT;
CREATE TABLE node_artifacts (
    owner TEXT NOT NULL, revision INTEGER NOT NULL, artifact TEXT NOT NULL REFERENCES artifacts(id),
    PRIMARY KEY(owner,revision,artifact), FOREIGN KEY(owner,revision) REFERENCES node_history(owner,revision)
) STRICT;
CREATE TABLE edges (
    parent TEXT NOT NULL REFERENCES nodes(id), child TEXT NOT NULL REFERENCES nodes(id),
    PRIMARY KEY(parent,child), CHECK(parent!=child)
) STRICT;
CREATE INDEX consumers ON edges(child,parent);
CREATE TABLE roots (ordinal INTEGER PRIMARY KEY CHECK(ordinal>=0), owner TEXT NOT NULL UNIQUE REFERENCES nodes(id)) STRICT;
CREATE TABLE records (
    scope TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0),
    PRIMARY KEY(scope,key), FOREIGN KEY(scope,key,revision) REFERENCES record_history(scope,key,revision) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE TABLE record_history (
    scope TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0),
    artifact TEXT REFERENCES artifacts(id), PRIMARY KEY(scope,key,revision)
) STRICT;
CREATE TABLE counters (
    scope TEXT NOT NULL, key TEXT NOT NULL, value INTEGER NOT NULL CHECK(value>=0), PRIMARY KEY(scope,key)
) STRICT;
CREATE TABLE intents (
    key TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES nodes(id), generation INTEGER NOT NULL CHECK(generation>=0),
    kind TEXT NOT NULL, payload BLOB NOT NULL, priority INTEGER NOT NULL CHECK(priority>=0),
    not_before INTEGER NOT NULL CHECK(not_before>=0),
    state TEXT NOT NULL CHECK(state IN ('PENDING','RUNNING','DONE','CANCELLED','BLOCKED')),
    current_execution TEXT REFERENCES executions(id) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE INDEX ready_intents ON intents(state,priority,not_before,key);
CREATE INDEX owner_intents ON intents(owner,generation,state);
CREATE TABLE intent_artifacts (
    intent TEXT NOT NULL REFERENCES intents(key), artifact TEXT NOT NULL REFERENCES artifacts(id), PRIMARY KEY(intent,artifact)
) STRICT;
CREATE TABLE intent_owners (
    intent TEXT NOT NULL REFERENCES intents(key), owner TEXT NOT NULL REFERENCES nodes(id), generation INTEGER NOT NULL,
    PRIMARY KEY(intent,owner)
) STRICT;
CREATE INDEX fenced_intents ON intent_owners(owner,intent);
CREATE TABLE intent_dependencies (
    intent TEXT NOT NULL REFERENCES intents(key), dependency TEXT NOT NULL REFERENCES intents(key) DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY(intent,dependency), CHECK(intent!=dependency)
) STRICT;
CREATE INDEX downstream_intents ON intent_dependencies(dependency,intent);
CREATE TABLE executions (
    id TEXT PRIMARY KEY, intent TEXT NOT NULL REFERENCES intents(key),
    session TEXT NOT NULL REFERENCES sessions(id), adopted_session TEXT REFERENCES sessions(id),
    reservation TEXT NOT NULL REFERENCES artifacts(id), bounds BLOB NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('PREPARED','DISPATCHED','UNKNOWN','NOT_SENT','RECEIVED')),
    charged INTEGER NOT NULL CHECK(charged IN (0,1)), held INTEGER NOT NULL CHECK(held IN (0,1)),
    resource TEXT NOT NULL CHECK(resource IN ('MODEL','COMPILER','LOCAL','NETWORK')), memory INTEGER NOT NULL CHECK(memory>=0),
    provider TEXT, provider_request TEXT,
    CHECK(provider_request IS NULL OR provider IS NOT NULL)
) STRICT;
CREATE UNIQUE INDEX provider_request_identity ON executions(provider,provider_request) WHERE provider_request IS NOT NULL;
CREATE INDEX execution_recovery ON executions(phase,id);
CREATE INDEX executions_intent ON executions(intent,id);
CREATE TABLE receipts (
    execution TEXT PRIMARY KEY REFERENCES executions(id), request TEXT NOT NULL REFERENCES artifacts(id),
    result TEXT NOT NULL REFERENCES artifacts(id), seal TEXT NOT NULL REFERENCES artifacts(id),
    status TEXT NOT NULL CHECK(status IN ('SUCCEEDED','FAILED','CANCELLED')),
    delivery TEXT NOT NULL CHECK(delivery IN ('NOT_SENT','DELIVERED','UNKNOWN')),
    termination TEXT NOT NULL CHECK(termination IN ('TERMINATED','UNKNOWN'))
) STRICT;
CREATE TABLE receipt_artifacts (
    execution TEXT NOT NULL REFERENCES receipts(execution), artifact TEXT NOT NULL REFERENCES artifacts(id),
    PRIMARY KEY(execution,artifact)
) STRICT;
CREATE TABLE imported_receipts (
    execution TEXT PRIMARY KEY, payload BLOB NOT NULL, seal TEXT NOT NULL REFERENCES artifacts(id)
) STRICT;
CREATE TABLE usage_reports (
    execution TEXT NOT NULL REFERENCES executions(id), key TEXT NOT NULL, payload BLOB NOT NULL,
    PRIMARY KEY(execution,key)
) STRICT;
CREATE TABLE execution_usage (
    execution TEXT PRIMARY KEY REFERENCES executions(id), input_tokens INTEGER CHECK(input_tokens>=0),
    cache_tokens INTEGER CHECK(cache_tokens>=0), output_tokens INTEGER CHECK(output_tokens>=0),
    cost_microusd INTEGER CHECK(cost_microusd>=0)
) STRICT;
CREATE TABLE progress (
    execution TEXT NOT NULL REFERENCES executions(id), sequence INTEGER NOT NULL CHECK(sequence>=0),
    kind TEXT NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(execution,sequence)
) STRICT;
CREATE TABLE progress_artifacts (
    execution TEXT NOT NULL, sequence INTEGER NOT NULL, artifact TEXT NOT NULL REFERENCES artifacts(id),
    PRIMARY KEY(execution,sequence,artifact), FOREIGN KEY(execution,sequence) REFERENCES progress(execution,sequence)
) STRICT;
CREATE TABLE applications (
    key TEXT PRIMARY KEY, payload BLOB NOT NULL, execution TEXT UNIQUE REFERENCES receipts(execution)
) STRICT;
CREATE TABLE dispositions (
    execution TEXT PRIMARY KEY REFERENCES receipts(execution), application TEXT NOT NULL UNIQUE REFERENCES applications(key),
    value TEXT NOT NULL
) STRICT;
CREATE TABLE counts (category TEXT NOT NULL, state TEXT NOT NULL, value INTEGER NOT NULL CHECK(value>=0), PRIMARY KEY(category,state)) STRICT;
CREATE TABLE accounting (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), turns INTEGER NOT NULL DEFAULT 0 CHECK(turns>=0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens>=0), cache_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cache_tokens>=0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens>=0), cost_microusd INTEGER NOT NULL DEFAULT 0 CHECK(cost_microusd>=0),
    input_tokens_unknown INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens_unknown>=0), cache_tokens_unknown INTEGER NOT NULL DEFAULT 0 CHECK(cache_tokens_unknown>=0),
    output_tokens_unknown INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens_unknown>=0), cost_microusd_unknown INTEGER NOT NULL DEFAULT 0 CHECK(cost_microusd_unknown>=0),
    input_tokens_reserved INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens_reserved>=0), cache_tokens_reserved INTEGER NOT NULL DEFAULT 0 CHECK(cache_tokens_reserved>=0),
    output_tokens_reserved INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens_reserved>=0), cost_microusd_reserved INTEGER NOT NULL DEFAULT 0 CHECK(cost_microusd_reserved>=0),
    models INTEGER NOT NULL DEFAULT 0 CHECK(models>=0), compilers INTEGER NOT NULL DEFAULT 0 CHECK(compilers>=0),
    locals INTEGER NOT NULL DEFAULT 0 CHECK(locals>=0), compiler_memory INTEGER NOT NULL DEFAULT 0 CHECK(compiler_memory>=0),
    networks INTEGER NOT NULL DEFAULT 0 CHECK(networks>=0)
) STRICT;
INSERT INTO accounting(singleton) VALUES (1);
"""


def create(db):
    db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA)
    for table, column in (('nodes', 'state'), ('intents', 'state'), ('executions', 'phase')):
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            statements = []
            if event != 'INSERT':
                statements.append(f"UPDATE counts SET value=value-1 WHERE category='{table}' AND state=OLD.{column};")
            if event != 'DELETE':
                statements.append(f"INSERT INTO counts VALUES ('{table}',NEW.{column},1) ON CONFLICT(category,state) DO UPDATE SET value=value+1;")
            db.execute(f'CREATE TRIGGER count_{table}_{event} AFTER {event} ON {table} BEGIN {" ".join(statements)} END')
    for table in ('artifacts', 'node_history', 'node_artifacts', 'record_history', 'intent_artifacts', 'intent_owners',
                  'intent_dependencies', 'receipts', 'receipt_artifacts', 'usage_reports', 'progress',
                  'progress_artifacts', 'applications', 'dispositions'):
        for event in ('UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER immutable_{table}_{event} BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT,'immutable storage record'); END")
    db.execute(f'PRAGMA application_id={APPLICATION_ID}')
    db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
