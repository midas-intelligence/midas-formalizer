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

FILE_BYTES = 2 * 1024**2
CORPUS_BYTES = 512 * 1024**2
CANDIDATES = 100
DETAILS = 12
TOTAL = 50
STOP = frozenset(('the', 'and', 'that', 'with', 'from', 'have', 'exists', 'such', 'this', 'are', 'two', 'for'))
_WORDS = re.compile(r'[^\W_]+')
_CAMEL = re.compile(r'([a-z])([A-Z])')
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
WEIGHTED_RANK = 'bm25(3.0, 2.0, 1.0)'
FORMAT = b'midas-bm25-source-v2'


def stopped(cancel, deadline):
    if cancel.is_set() or time.monotonic() >= deadline:
        raise TimeoutError('Search cancelled or deadline exceeded')


def token_text(value):
    """Original experiment tokenizer: retain words and camel components.

    Multiplicity is term frequency. No stemming, singularization, identifier
    encoding, or query-score normalization is applied.
    """
    out = []
    for word in _WORDS.findall(value):
        parts = _CAMEL.sub(r'\1 \2', word).split()
        for term in ([word] if len(parts) == 1 else [word, *parts]):
            folded = term.casefold()
            if folded not in STOP:
                out.append(folded)
    return ' '.join(out)


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
                db.execute('INSERT INTO terms(rowid,name,documentation,statement) VALUES(?,?,?,?)',
                    (row.lastrowid, token_text(name), token_text(doc), token_text(header)))
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


def ranked(db, query, prefixes, *, limit=TOTAL):
    """Filter before top-100, preserving corpus IDF; then apply file preference.

    Scores returned to evidence are raw BM25.
    The 1.5 multiplier affects ordering only. Stable ties preserve FTS order.
    """
    db.execute('CREATE TEMP TABLE scope_docs(id INTEGER PRIMARY KEY, preferred INTEGER NOT NULL)')
    for prefix in prefixes:
        db.execute('''INSERT INTO scope_docs
            SELECT d.id, f.path=? FROM files f JOIN documents d ON d.file=f.id
            WHERE f.path=? OR (f.path>=? AND f.path<?)
            ON CONFLICT(id) DO UPDATE SET preferred=max(preferred,excluded.preferred)''',
            (prefix, prefix, prefix+'/', prefix+'0'))
    query = query.strip()
    candidates = []
    terms = dict.fromkeys(token_text(query).split())
    if terms:
        expression = ' OR '.join('"'+term.replace('"', '""')+'"' for term in terms)
        for id, score in db.execute('''SELECT rowid,rank FROM terms WHERE terms MATCH ?
                AND EXISTS (SELECT 1 FROM scope_docs s WHERE s.id=terms.rowid)
                ORDER BY rank LIMIT ?''', (expression, CANDIDATES)):
            candidates.append((id, -score))
    hits = []
    for id, score in candidates:
        row = db.execute('''SELECT d.name,f.path,d.line,d.start_line,d.start,d.end,s.preferred
            FROM documents d JOIN files f ON f.id=d.file JOIN scope_docs s ON s.id=d.id
            WHERE d.id=?''', (id,)).fetchone()
        hits.append((score, row))
    hits.sort(key=lambda h: -h[0]*(1.5 if h[1][-1] else 1))
    seen, result = set(), []
    for score, row in hits:
        name = row[0]
        if name and name in seen:
            continue
        seen.add(name)
        result.append((*row[:-1], score))
        if len(result) == limit:
            break
    return result
