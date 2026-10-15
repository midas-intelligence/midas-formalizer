"""Alternating local retrieval/paging comparisons; no network, models or Lean."""
from pathlib import Path
from statistics import median
import sys
import threading
from timeit import timeit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer.combined_retrieval import fuse as old_fuse
from midas_formalizer.discovery import content_matches as old_matches
from midas_formalizer.source_names import declaration_names as old_names
from midas_formalizer.evidence_archive import EvidenceArchive as OldArchive
from midas.tools import (Backend, HelperRequest, Match, Retrieve, Scope, SearchResult,
                        fuse, prepare_archive, recall, search_stages)
from midas.tools.search import content_matches
from midas.tools.plans import _canonical
from midas.domain import OwnerVersion
from tests.test_tools import Artifacts


def compare(label, old, new, count):
    samples = ([], [])
    for sample in range(7):
        for side in (sample % 2, 1-sample % 2):
            samples[side].append(timeit((old, new)[side], number=count)/count)
    before, after = map(median, samples)
    print(f'{label}: v2={before*1e6:.1f} us new={after*1e6:.1f} us ratio={after/before:.3f}')


def main():
    artifacts = Artifacts()
    scope = Scope(OwnerVersion('obl_'+'1'*64, 0), 'env_'+'2'*64, artifacts.save(b'context'))
    request = HelperRequest(scope, Retrieve(('one', 'two', 'three'),
                backends=(Backend('frozen-source', ('Mathlib',)),)), 'resume')
    spec = {'query': {'scope': {}, 'query': {'queries': ['one','two','three'], 'names': [], 'result_offset':0},
            'configuration': {'forbidden': [], 'backends': [{'backend':'frozen-source-v1',
                'configuration': {'prefixes':['Mathlib']}, 'error':None}]}}}
    previous, current = [], []
    for stage in search_stages(request):
        names = tuple(f'N.x{(n*7+stage.ordinal)%400}' for n in range(400))
        rows = tuple(Match(names[i:i+20], 'lib/X.lean', i+1, i+1, 'source', 1) for i in range(0,400,20))
        previous.append({'status':'success', 'records':[{'names':list(r.names)} for r in rows]})
        current.append(SearchResult(stage.sha256, rows))
    current = tuple(current)
    assert old_fuse(spec, previous)['ranked'] == list(fuse(request, current).ranked)
    compare('fusion_1200_hints', lambda: old_fuse(spec, previous), lambda: fuse(request, current), 10)
    def cold_fusion():
        _canonical.cache_clear()
        return fuse(request, current)
    compare('fusion_1200_hints_cold_names', lambda: old_fuse(spec, previous), cold_fusion, 10)

    cancel, deadline = threading.Event(), float('inf')
    for label, source, query in (
            ('source_matching', ('-- compact sets\ntheorem compact : True := by trivial\n'+'\n'*10)*250, 'compact'),
            ('source_no_matches', ('def unrelated : Nat := 0\n'+'\n'*10)*250, 'compact')):
        def previous_match():
            return old_matches(source.splitlines(keepends=True), old_names(source), query,
                               'lib/X.lean', 20, cancel, deadline)
        def current_match():
            return content_matches(source, query, 'lib/X.lean', 20, cancel, deadline)
        a, b = previous_match(), current_match()
        assert [(r['line'],r['source'],r['names'],r['score']) for r in a] == [
            (r.line,r.source,list(r.names),r.score) for r in b]
        compare(label, previous_match, current_match, 3)

    records = [{'body':'α😀 abc '*10000}]
    archive = prepare_archive(scope, 'a'*64, records, (artifacts.save(b'receipt'),), artifacts)
    old = object.__new__(OldArchive)
    old.recall = lambda *args, **kwargs: {'request': {'scope':{}}, 'records':records}
    def old_first():
        return old.page('result', obligation='owner')
    def new_first():
        return recall(archive, scope, artifacts)
    # The old archive read is deliberately free, isolating its repeated fragment
    # projection. New recall includes verification and one real artifact read.
    compare('evidence_first_page_110KB', old_first, new_first, 1)


if __name__ == '__main__':
    main()
