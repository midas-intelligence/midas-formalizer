"""Opt-in query/named retrieval, checked source and compact evidence with real Lean.

Requires permission under the root AGENTS.md. No network or live model calls.
Usage: python -B -m tests.check_search_lean /absolute/path/to/bin/lean
"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
from unittest.mock import patch

from midas.application import Application, helpers, records
from midas.domain import SelectionRequest
from midas.environment import PackageConfig
from midas.policy import Config, ModelRoute
from midas.tools.search import source_search
from tests.test_strategy import proof, response


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    with TemporaryDirectory(prefix='midas-search-lean-') as temporary:
        root = Path(temporary)
        project = root/'project'
        project.mkdir()
        source = ('import Lean\nstructure GeneratedFixture.Box where\n  value : Nat\nnamespace SearchFixture\n'+
                  ''.join(f'def term{i:02} : Nat := {i}\n' for i in range(70))+
                  'end SearchFixture\ntheorem target : True := by sorry\n')
        (project/'Main.lean').write_text(source)
        app = Application('/tmp/midas-new-search-runtime')
        package = app.package(root/'package', PackageConfig('4.35.0-rc3', toolchain,
            (('project', project),), ('project/Main.lean',), ('project', 'toolchain/lib/lean')))
        assert (package.directory/'search.sqlite').is_file()
        selected = app.select(package, SelectionRequest('Main.lean', all_unfinished=True))
        app = Application(root/'run-cache')
        route = ModelRoute(provider='azure', model='fixture', endpoint='https://fixture.test/responses')
        config = replace(Config(), prover=route, reviewer=route)
        backend = dict(name='frozen-source', prefixes=['project/Main.lean'])
        discovery = dict(mode='retrieve', rationale='Find and inspect local declarations.',
                         queries=['SearchFixture terms'], names=[], backends=[backend])
        generated = dict(mode='retrieve', rationale='Look up a generated recursor through local search.',
                         queries=['GeneratedFixture.Box.rec'], names=[], backends=[backend])
        tail = dict(mode='retrieve', rationale='Expand the last visible name.', names=['SearchFixture.term49'],
                    queries=[], backends=[])
        followup = dict(mode='retrieve', rationale='Inspect selected names from the search results.', queries=[],
                        names=['SearchFixture.term12', 'SearchFixture.term23'], backends=[])
        named = dict(mode='retrieve', rationale='Read an explicit definition.', queries=[],
                     names=['SearchFixture.term69'], backends=[])
        action = proof()
        action['proof_body'] = 'by trivial'
        with patch('midas.tools.bm25._build', side_effect=AssertionError('unexpected runtime indexing')), \
             patch('midas.tools.search.source_search', wraps=source_search) as searches, \
             app.start(root/'run', package=package, selection=selected, config=config, workflow='scripted',
                script=tuple(('prover', response(a)) for a in (discovery, tail, followup, named, generated, action))) as run:
            result = run.run()
            assert result.exit_code == 0, result
            owner = run.workflow.graph(run.writer).nodes[0].owner
            archives = helpers.advertised(run.writer, owner)
            assert len(archives) == 5, len(archives)
            pages = [helpers.page_text(records.read(run.writer, archive.pages[0], 16000)).decode()
                     for archive in archives]
            assert pages[0].count('Source:') == 12, pages[0]
            assert '50. SearchFixture.term49' in pages[0], pages[0]
            assert 'SearchFixture.term50' not in pages[0], pages[0]
            assert 'def term00 : Nat := 0' in pages[0], pages[0]
            assert 'def term11 : Nat := 11' in pages[0], pages[0]
            assert 'def term49 : Nat := 49' in pages[1], pages[1]
            assert 'def term12 : Nat := 12' in pages[2], pages[2]
            assert 'def term23 : Nat := 23' in pages[2], pages[2]
            assert 'def term69 : Nat := 69' in pages[3], pages[3]
            assert pages[4].startswith('GeneratedFixture.Box.rec :'), pages[4]
            assert 'declaration is not visible' not in pages[4], pages[4]
            assert run.writer.counter('owner:'+owner.obligation_id, 'retrieval') == 5
            assert run.writer.status_snapshot().accounting.turns == 6
            assert searches.call_count == 2, searches.call_count
            assert not (app.cache/'search').exists()
            print('Query/named retrieval, generated recursor priority, BM25 12/50 delivery, '
                  'selected-name followups and proof continuation passed', flush=True)
        with app.resume(root/'run') as run:
            assert run.run().exit_code == 0
        print('Prepared package index reused with a fresh cache; authenticated resume passed', flush=True)


if __name__ == '__main__':
    main()
