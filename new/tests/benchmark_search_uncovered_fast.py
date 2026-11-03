"""Sparse compound scoring; native FTS fallback, no index mutations or JSON.

Keep the frozen scorer as the reference. Compute compound replacements only for
matching documents; native SQL scores all fallback postings before the cutoff.
Temporary tables belong to the caller's SQLite connection and are dropped.
"""
from collections import defaultdict

from midas.tools import bm25
from tests.benchmark_search_identifier_budgets import coefficients, expression, groups, ranked
from tests.benchmark_search_uncovered import spans


def fast_ranked(db, query, partial_discount=.5, limit=bm25.CANDIDATES):
    units = list(groups(query))
    if not any(compounds for _,_,compounds in units):
        return ranked(db, coefficients(query, 'identifier'), limit)
    replacements = []
    for unit, (word, lexical, compounds) in enumerate(units):
        if not compounds:
            continue
        parts, locations = spans(word)
        n = sum(bool(part) for part in parts)
        chosen, covered = {}, {}
        # Locations are already longest-first, then left-to-right. Cache a
        # repeated token's posting list within this query identifier.
        postings = {}
        for token, start, end in locations:
            if token not in postings:
                postings[token] = db.execute(
                    'SELECT rowid,bm25(terms,3,2,1,3,2,1,3,2,1) FROM terms WHERE terms MATCH ?',
                    (expression([token]),)).fetchall()
            mask = ((1 << (end-start))-1) << start
            for doc, score in postings[token]:
                used = covered.get(doc, 0)
                if not used & mask:
                    covered[doc] = used | mask
                    chosen[doc] = chosen.get(doc, 0.) + (end-start)*score/n
        if partial_discount and chosen:
            # Filter the term's posting stream by document ID. Unary + prevents
            # FTS from restarting a term search for each ID in the IN list;
            # common terms otherwise pay heavily for repeated seeks. BM25
            # document frequency still comes from the full index.
            uncovered = defaultdict(dict)
            for pos, token in enumerate(parts):
                if token and token not in bm25.STOP:
                    for doc, mask in covered.items():
                        if not mask & (1 << pos):
                            uncovered[token][doc] = uncovered[token].get(doc, 0) + 1
            partials = defaultdict(float)
            for token, docs in uncovered.items():
                ids = list(docs)
                for offset in range(0, len(ids), 500):
                    batch = ids[offset:offset+500]
                    sql = ('SELECT rowid,bm25(terms,3,2,1,3,2,1,3,2,1) FROM terms '
                           'WHERE terms MATCH ? AND +rowid IN ('+','.join('?' for _ in batch)+')')
                    for doc, score in db.execute(sql, (expression([token]), *batch)):
                        partials[doc] += docs[doc]*score
            for doc, score in partials.items():
                chosen[doc] += partial_discount*score/n
        replacements.extend((doc, unit, score) for doc, score in chosen.items())
    db.execute('CREATE TEMP TABLE span_replacements(document INTEGER,unit INTEGER,score REAL,PRIMARY KEY(document,unit)) WITHOUT ROWID')
    try:
        db.executemany('INSERT INTO span_replacements VALUES(?,?,?)', replacements)
        sql_parts, params = [], []
        for unit, (_, lexical, compounds) in enumerate(units):
            if lexical:
                sql_parts.append('SELECT rowid,? AS unit,bm25(terms,3,2,1,3,2,1,3,2,1)*? AS score FROM terms WHERE terms MATCH ?')
                params.extend((unit, (.5 if compounds else 1.)/len(lexical), expression(lexical)))
        lexical_sql = ' UNION ALL '.join(sql_parts) or 'SELECT 0 AS rowid,0 AS unit,0. AS score WHERE 0'
        sql = ('WITH lexical AS MATERIALIZED ('+lexical_sql+'), '
               'scores AS (SELECT rowid,score FROM lexical l WHERE NOT EXISTS '
               '(SELECT 1 FROM span_replacements r WHERE r.document=l.rowid AND r.unit=l.unit) '
               'UNION ALL SELECT document,score FROM span_replacements) '
               'SELECT rowid,sum(score) AS total FROM scores GROUP BY rowid '
               'HAVING total<0 ORDER BY total,rowid LIMIT ?')
        return db.execute(sql, (*params, limit)).fetchall()
    finally:
        db.execute('DROP TABLE span_replacements')


def fast_retrieve(db, query, partial_discount=.5):
    rows = fast_ranked(db, query, partial_discount)
    exact = [doc for doc, in db.execute('SELECT id FROM documents WHERE name=? ORDER BY id',
                                      (query.strip().removeprefix('_root_.'),))]
    seen, found = set(), []
    for doc in dict.fromkeys([*exact, *(doc for doc, _ in rows)]):
        name, = db.execute('SELECT name FROM documents WHERE id=?', (doc,)).fetchone()
        if name and name in seen:
            continue
        seen.add(name)
        found.append((doc, name))
        if len(found) == bm25.TOTAL:
            break
    return found
