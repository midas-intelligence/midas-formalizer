"""CPU-only declaration discovery over a disposable, frozen-corpus FTS5 index.

Source extraction is lexical. It supplies hints, never checked declarations or
compiler-generated names. The cache stores postings and source coordinates, not
environment objects or copies of entire source files.
"""
from bisect import bisect_right
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory, NamedTemporaryFile
import fcntl
import os
import re
import sqlite3
import time

from ..domain.encoding import hash_field
from ..source import code_only, declaration_names, parse_identifier
from .protocol import read
from . import bm25_score
from .bm25_score import STOP, token_text

FILE_BYTES = 2 * 1024**2
CORPUS_BYTES = 512 * 1024**2
CANDIDATES = 100
DETAILS = 12
TOTAL = 50
_ATTR = re.compile(r'@\[[^\]]*\]')
_COMMENT_BOUNDARY = re.compile(r'/-|-/')
_MODIFIERS = r'(?:(?:private|protected|public|noncomputable|unsafe|partial|nonrec)\s+)*'
_DECLARATION = re.compile(r'(?m)^[ \t]*'+_MODIFIERS+
    r'(?P<kind>theorem|lemma|def|abbrev|instance|class|structure|inductive|opaque|axiom)\b')
_COMMAND = re.compile(r'(?m)^(?P<indent>[ \t]*)'+_MODIFIERS+
    r'(?:theorem|lemma|def|abbrev|instance|class|structure|inductive|opaque|axiom|example|'
    r'namespace|section|end|open|variable|universe|attribute|set_option|include|omit|'
    r'alias|export|syntax|macro|elab|notation|infix[lr]?|prefix|postfix|mutual|deriving)\b')
_HEADER_TOKEN = re.compile(r'«[^»]*»|:=|[()\[\]{}⟨⟩]|(?<![\w.])(?:where|let|match|by|do)(?![\w.])|(?m:^\s*\|)')
_OPEN = {'(': ')', '[': ']', '{': '}', '⟨': '⟩'}
WEIGHTED_RANK = bm25_score.RANK
FORMAT = b'midas-bm25-source-v3-compound-spans'


def stopped(cancel, deadline):
    if cancel.is_set() or time.monotonic() >= deadline:
        raise TimeoutError('Search cancelled or deadline exceeded')


def _statement_end(clean):
    """Header boundary in comment/attribute-blanked code, or None if uncertain."""
    declaration = _DECLARATION.match(clean)
    if declaration is None:
        return None
    stack = []
    for match in _HEADER_TOKEN.finditer(clean, declaration.end()):
        token = match[0]
        if token.startswith('«'):
            continue
        if token in _OPEN:
            stack.append(_OPEN[token])
        elif token in _OPEN.values():
            if not stack or stack.pop() != token:
                return None
        elif not stack:
            if token in ('let', 'match', 'by', 'do'):
                return None
            if token in (':=', 'where') or token.lstrip().startswith('|'):
                return match.start()
    return None if stack else len(clean)


def statement(clean):
    """Header from comment/attribute-blanked code; implementation excluded."""
    end = _statement_end(clean)
    return clean[:end].strip() if end is not None else ''


def documents(source):
    """Yield name, line, excerpt line/byte range, documentation and header.

    Theorem/lemma excerpts stop before the proof; definitions retain their bodies.
    Anonymous instances retain their source since there is no name to look up.
    Commands delimit excerpts, including documentation before the next command.
    """
    names = declaration_names(source)
    clean = _ATTR.sub(lambda m: re.sub(r'[^\n]', ' ', m[0]), code_only(source))
    lines, clean_lines = source.splitlines(keepends=True), clean.splitlines(keepends=True)
    chars, offsets = [0], [0]
    for line in lines:
        chars.append(chars[-1]+len(line))
        offsets.append(offsets[-1]+len(line.encode()))
    commands = []
    for match in _COMMAND.finditer(clean):
        line = bisect_right(chars, match.start())-1
        leading = line
        while leading and not clean_lines[leading-1].strip():
            leading -= 1
        # Do not attach a module overview or unrelated comments to a declaration.
        prefix = ''.join(lines[leading:line])
        doc = prefix.find('/--')
        start = leading + prefix[:doc].count('\n') if doc >= 0 else line
        documentation = ''
        if doc >= 0:
            depth = 1
            for boundary in _COMMENT_BOUNDARY.finditer(prefix, doc+2):
                depth += 1 if boundary[0] == '/-' else -1
                if not depth:
                    documentation = prefix[doc:boundary.end()]
                    break
        commands.append((match, line, start, documentation))
    for i, (match, line, start, doc) in enumerate(commands):
        declaration = _DECLARATION.match(clean, match.start())
        if declaration is None:
            continue
        name = names.get(line, '')
        if name and parse_identifier(name) is None:
            continue
        if re.match(r'\s*private\b', match[0]):
            continue
        end = len(lines)
        for j in range(i+1, len(commands)):
            following, _, leading, _ = commands[j]
            if len(following['indent']) <= len(match['indent']):
                end = leading
                break
        # Blank lines between commands are not part of the authored declaration.
        while end > line+1 and not lines[end-1].strip():
            end -= 1
        text = clean[chars[line]:chars[end]]
        boundary = _statement_end(text)
        header = text[:boundary].strip() if boundary is not None else ''
        excerpt_end = offsets[end]
        if declaration['kind'] in ('theorem', 'lemma'):
            # Preserve authored documentation, attributes and binder spelling.
            # If lexical syntax is uncertain, keep the name for checked lookup
            # instead of falling back to an excerpt that could include a proof.
            excerpt_end = (offsets[line] + len(source[chars[line]:chars[line]+boundary].rstrip().encode())
                           if boundary is not None else offsets[start])
        yield name, line+1, start+1, offsets[start], excerpt_end, doc, header


def corpus_key(index):
    digest = sha256(FORMAT)
    hash_field(digest, index.environment_id.encode())
    for entry in index.entries:
        for field in (entry.path, entry.artifact.sha256, str(entry.artifact.byte_count)):
            hash_field(digest, field.encode())
    return digest.hexdigest()


def _connect(path, cancel, deadline, *, readonly=False):
    db = sqlite3.connect(path.as_uri()+'?mode=ro' if readonly else path, uri=readonly)
    db.set_progress_handler(lambda: int(cancel.is_set() or time.monotonic() >= deadline), 1000)
    db.execute('PRAGMA temp_store=FILE')
    return db


def _build(path, key, index, reader, cancel, deadline):
    size = sum(e.artifact.byte_count for e in index.entries)
    if size > CORPUS_BYTES or any(e.artifact.byte_count > FILE_BYTES for e in index.entries):
        raise ValueError('Source index byte allowance exceeded')
    db = _connect(path, cancel, deadline)
    try:
        db.executescript('''
            CREATE TABLE metadata(key TEXT PRIMARY KEY);
            CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE);
            CREATE TABLE documents(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                file INTEGER NOT NULL, line INTEGER NOT NULL, start_line INTEGER NOT NULL,
                start INTEGER NOT NULL, end INTEGER NOT NULL);
            CREATE INDEX names ON documents(name);
            CREATE INDEX source_files ON documents(file);
            CREATE VIRTUAL TABLE terms USING fts5(name, documentation, statement,
                pair_name, pair_documentation, pair_statement,
                triple_name, triple_documentation, triple_statement,
                content='', tokenize='unicode61 remove_diacritics 0');
        ''')
        db.execute("INSERT INTO terms(terms,rank) VALUES('rank',?)", (WEIGHTED_RANK,))
        for file, entry in enumerate(index.entries, 1):
            stopped(cancel, deadline)
            source = read(reader, entry.artifact, FILE_BYTES).decode()
            db.execute('INSERT INTO files VALUES(?,?)', (file, entry.path))
            for n, (name, line, start_line, start, end, doc, header) in enumerate(documents(source)):
                if n % 128 == 0:
                    stopped(cancel, deadline)
                row = db.execute('INSERT INTO documents(name,file,line,start_line,start,end) VALUES(?,?,?,?,?,?)',
                    (name, file, line, start_line, start, end))
                db.execute('INSERT INTO terms(rowid,name,documentation,statement,'
                    'pair_name,pair_documentation,pair_statement,'
                    'triple_name,triple_documentation,triple_statement) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (row.lastrowid, *bm25_score.fields(name, doc, header)))
        db.execute("INSERT INTO terms(terms) VALUES('optimize')")
        db.execute('INSERT INTO metadata VALUES(?)', (key,))
        db.commit()
    finally:
        db.close()


def _valid(path, key, cancel, deadline):
    if not path.is_file():
        return False
    try:
        db = _connect(path, cancel, deadline, readonly=True)
        try:
            return db.execute('SELECT key FROM metadata').fetchone() == (key,)
        finally:
            db.close()
    except sqlite3.DatabaseError:
        stopped(cancel, deadline)
        return False


def prepare(index, reader, cancel, deadline, path, *, temporary_path=None):
    """Publish reusable postings atomically under a cancellable builder lock."""
    stopped(cancel, deadline)
    path = Path(path).absolute()
    key = index.search_key
    if not _valid(path, key, cancel, deadline):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix('.lock').open('ab') as lock:
            while True:
                stopped(cancel, deadline)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    cancel.wait(.02)
            if not _valid(path, key, cancel, deadline):
                if temporary_path is None:
                    with NamedTemporaryFile(prefix=path.stem+'-', suffix='.sqlite', dir=path.parent, delete=False) as temporary:
                        temp = Path(temporary.name)
                else:
                    temp = Path(temporary_path)
                    if temp.parent != path.parent or temp == path:
                        raise ValueError('Index temporary must be a separate file beside its destination')
                try:
                    _build(temp, key, index, reader, cancel, deadline)
                    stopped(cancel, deadline)
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)
    return path


@contextmanager
def database(index, reader, cancel, deadline, cache_directory=None, *, prepared_path=None):
    """Prefer package postings without creating a cache directory or taking a lock."""
    stopped(cancel, deadline)
    path = Path(prepared_path).absolute() if prepared_path is not None else None
    if path is None or not _valid(path, index.search_key, cancel, deadline):
        if cache_directory is None:
            with TemporaryDirectory(prefix='midas-search-') as temporary:
                with database(index, reader, cancel, deadline, temporary) as db:
                    yield db
            return
        path = prepare(index, reader, cancel, deadline,
                       Path(cache_directory)/(index.search_key+'.sqlite'))
    db = _connect(path, cancel, deadline, readonly=True)
    try:
        yield db
    finally:
        db.close()


def _path_weights(prefixes):
    """Relative-depth bonuses, ignoring the source root and top-level namespace."""
    hints = tuple(dict.fromkeys(p.removesuffix('.lean') for p in prefixes))
    weights = {}
    for hint in hints:
        # All text matches are eligible already. A broad parent hint must not
        # give its full bonus to every sibling of a more specific requested path.
        if any(other.startswith(hint+'/') for other in hints):
            continue
        parts = hint.split('/')
        # Short root/namespace hints still prefer their entire subtree. Longer
        # hints give no bonus merely for sharing e.g. mathlib/Mathlib.
        base = min(2, len(parts)-1)
        for depth in range(base+1, len(parts)+1):
            prefix = '/'.join(parts[:depth])
            weight = 1+.5*(depth-base)/(len(parts)-base)
            weights[prefix] = max(weights.get(prefix, 1.), weight)
    return weights


def _path_preferences(db, prefixes, check):
    """Apply the best hint's bonus once per file, using indexed path ranges."""
    db.execute('CREATE TEMP TABLE path_weights(id INTEGER PRIMARY KEY, weight REAL NOT NULL)')
    weights = _path_weights(prefixes)
    if not weights:
        return False
    # Highest weight first: INSERT OR IGNORE keeps the best hint. Different-depth
    # hints can each give the full 50%, so depth alone does not order the bonuses.
    for prefix, weight in sorted(weights.items(), key=lambda item: -item[1]):
        check()
        db.execute('''INSERT OR IGNORE INTO path_weights
            SELECT id,? FROM files WHERE path=? OR (path>=? AND path<?)''',
            (weight, prefix+'.lean', prefix+'/', prefix+'0'))
    count, low, high = db.execute('SELECT count(*),min(weight),max(weight) FROM path_weights').fetchone()
    # Keep native FTS rank selection if every file receives the same multiplier.
    return bool(count) and (low != high or count != db.execute('SELECT count(*) FROM files').fetchone()[0])


def _ranked(db, query, prefixes, limit, check):
    """Rank all text matches with a soft path boost before candidate selection."""
    preferred = _path_preferences(db, prefixes, check)
    candidates = bm25_score.candidates(db, query.strip(), CANDIDATES, check, preferred=preferred)
    seen, result = set(), []
    for id, score in candidates:
        row = db.execute('''SELECT d.name,f.path,d.line,d.start_line,d.start,d.end
            FROM documents d JOIN files f ON f.id=d.file WHERE d.id=?''', (id,)).fetchone()
        name = row[0]
        if name and name in seen:
            continue
        seen.add(name)
        result.append((*row, -score))
        if len(result) == limit:
            break
    return result


def ranked(db, query, prefixes, *, limit=TOTAL, check=lambda: None):
    """Keep temporary scoring state inside one request, including on failure."""
    check()
    db.execute('SAVEPOINT search_query')
    try:
        return _ranked(db, query, prefixes, limit, check)
    finally:
        # Interrupting a SQLite write can already roll back the transaction,
        # including this savepoint. Preserve the original interruption error.
        if db.in_transaction:
            db.execute('ROLLBACK TO search_query')
            db.execute('RELEASE search_query')
