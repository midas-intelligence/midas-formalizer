"""Identifier budgets and non-overlapping compound BM25 over scoped postings."""
from collections import defaultdict
from fractions import Fraction
import re

STOP = frozenset(('the', 'and', 'that', 'with', 'from', 'have', 'exists', 'such', 'this', 'are', 'two', 'for'))
_WORDS = re.compile(r'[^\W_]+')
_IDENTIFIER = re.compile(r'[^\W]+')
_CAMEL = re.compile(r'([a-z])([A-Z])')
RANK = 'bm25(3,2,1,3,2,1,3,2,1)'
_SCORE = 'bm25(terms,3,2,1,3,2,1,3,2,1)'
_SCOPE = ' AND EXISTS (SELECT 1 FROM scope_docs s WHERE s.id=terms.rowid)'


def token_text(value):
    """Retain whole words and camel components, including term frequency."""
    out = []
    for word in _WORDS.findall(value):
        parts = _CAMEL.sub(r'\1 \2', word).split()
        for term in ([word] if len(parts) == 1 else [word, *parts]):
            folded = term.casefold()
            if folded not in STOP:
                out.append(folded)
    return ' '.join(out)


def spans(word):
    parts = _CAMEL.sub(r'\1_\2', word).casefold().split('_')
    compounds = []
    for length in (3, 2):
        for start in range(len(parts)-length+1):
            group = parts[start:start+length]
            if all(group):
                compounds.append(('longx'+'_'.join(group).encode().hex(), start, start+length))
    return parts, compounds


def fields(name, documentation, statement):
    """Nine fields: lexical, pairs, triples; each in name/doc/header order."""
    values = (name, documentation, statement)
    pairs, triples = [], []
    for value in values:
        pair, triple = [], []
        for word in _IDENTIFIER.findall(value):
            for token, start, end in spans(word)[1]:
                (pair if end-start == 2 else triple).append(token)
        pairs.append(' '.join(pair))
        triples.append(' '.join(triple))
    return (*map(token_text, values), *pairs, *triples)


def _groups(query):
    seen = set()
    for word in _IDENTIFIER.findall(query):
        lexical = tuple(dict.fromkeys(token_text(word).split()))
        parts, locations = spans(word)
        # Match the query-unit identity of the frozen scorer: pairs then triples.
        compounds = tuple(dict.fromkeys(token for token, start, end in
                          sorted(locations, key=lambda span: (span[2]-span[1], span[1]))))
        signature = lexical, compounds
        if (lexical or compounds) and signature not in seen:
            seen.add(signature)
            yield lexical, parts, locations


def _expression(tokens):
    return ' OR '.join('"'+token.replace('"', '""')+'"' for token in tokens)


def _ordinary(db, units, limit):
    weights = defaultdict(Fraction)
    for lexical, _, _ in units:
        for token in lexical:
            weights[token] += Fraction(1, len(lexical))
    buckets = defaultdict(list)
    for token, weight in weights.items():
        buckets[weight].append(token)
    if not buckets:
        return []
    if len(buckets) == 1:
        weight, tokens = next(iter(buckets.items()))
        return [(doc, score*float(weight)) for doc, score in db.execute(
            'SELECT rowid,rank FROM terms WHERE terms MATCH ? AND rank MATCH ?'+_SCOPE+
            ' ORDER BY rank LIMIT ?', (_expression(tokens), RANK, limit))]
    parts, params = [], []
    for weight, tokens in buckets.items():
        parts.append('SELECT rowid,'+_SCORE+'*? AS score FROM terms WHERE terms MATCH ?'+_SCOPE)
        params.extend((float(weight), _expression(tokens)))
    return db.execute('WITH scores AS MATERIALIZED ('+' UNION ALL '.join(parts)+') '
        'SELECT rowid,sum(score) AS total FROM scores GROUP BY rowid ORDER BY total,rowid LIMIT ?',
        (*params, limit)).fetchall()


def candidates(db, query, limit, check):
    """Score every in-scope posting before top-N, with full-corpus BM25 IDF.

    The caller owns a savepoint enclosing scope_docs and span_replacements.
    Checked exact-name lookup and file preference belong to the surrounding flow.
    """
    check()
    units = list(_groups(query))
    if not any(locations for _, _, locations in units):
        return _ordinary(db, units, limit)
    replacements = []
    for unit, (lexical, parts, locations) in enumerate(units):
        check()
        if not locations:
            continue
        n = sum(bool(part) for part in parts)
        chosen, covered, postings = {}, {}, {}
        for token, start, end in locations:
            check()
            if token not in postings:
                postings[token] = db.execute('SELECT rowid,'+_SCORE+
                    ' FROM terms WHERE terms MATCH ?'+_SCOPE, (_expression([token]),)).fetchall()
            mask = ((1 << (end-start))-1) << start
            for count, (doc, score) in enumerate(postings[token]):
                if count % 500 == 0:
                    check()
                used = covered.get(doc, 0)
                if not used & mask:
                    covered[doc] = used | mask
                    chosen[doc] = chosen.get(doc, 0.) + (end-start)*score/n
        uncovered = defaultdict(dict)
        for pos, token in enumerate(parts):
            if token and token not in STOP:
                for count, (doc, mask) in enumerate(covered.items()):
                    if count % 500 == 0:
                        check()
                    if not mask & (1 << pos):
                        uncovered[token][doc] = uncovered[token].get(doc, 0) + 1
        partials = defaultdict(float)
        for token, docs in uncovered.items():
            ids = list(docs)
            for offset in range(0, len(ids), 500):
                check()
                batch = ids[offset:offset+500]
                # Unary + keeps a posting scan instead of repeated FTS seeks.
                sql = ('SELECT rowid,'+_SCORE+' FROM terms WHERE terms MATCH ? '
                       'AND +rowid IN ('+','.join('?' for _ in batch)+')')
                for doc, score in db.execute(sql, (_expression([token]), *batch)):
                    partials[doc] += docs[doc]*score
        for doc, score in partials.items():
            chosen[doc] += .5*score/n
        replacements.extend((doc, unit, score) for doc, score in chosen.items())
    check()
    db.execute('CREATE TEMP TABLE span_replacements(document INTEGER,unit INTEGER,score REAL,'
               'PRIMARY KEY(document,unit)) WITHOUT ROWID')
    db.executemany('INSERT INTO span_replacements VALUES(?,?,?)', replacements)
    sql_parts, params = [], []
    for unit, (lexical, _, locations) in enumerate(units):
        if lexical:
            sql_parts.append('SELECT rowid,? AS unit,'+_SCORE+
                             '*? AS score FROM terms WHERE terms MATCH ?'+_SCOPE)
            params.extend((unit, (.5 if locations else 1.)/len(lexical), _expression(lexical)))
    lexical_sql = ' UNION ALL '.join(sql_parts) or 'SELECT 0 AS rowid,0 AS unit,0. AS score WHERE 0'
    sql = ('WITH lexical AS MATERIALIZED ('+lexical_sql+'), '
           'scores AS (SELECT rowid,score FROM lexical l WHERE NOT EXISTS '
           '(SELECT 1 FROM span_replacements r WHERE r.document=l.rowid AND r.unit=l.unit) '
           'UNION ALL SELECT document,score FROM span_replacements) '
           'SELECT rowid,sum(score) AS total FROM scores GROUP BY rowid '
           'HAVING total<0 ORDER BY total,rowid LIMIT ?')
    return db.execute(sql, (*params, limit)).fetchall()
