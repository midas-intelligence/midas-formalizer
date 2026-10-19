"""Opt-in BM25 -> compact evidence -> checked tail lookup with real Lean.

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
from tests.test_strategy import proof, response


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    with TemporaryDirectory(prefix='midas-search-lean-') as temporary:
        root = Path(temporary)
        project = root/'project'
        project.mkdir()
        source = ('import Lean\nnamespace SearchFixture\n'+
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
        search = dict(mode='search', rationale='Find local declarations.', query='SearchFixture', backend=backend)
        lookup = dict(mode='lookup', rationale='Expand the last visible name.', names=['SearchFixture.term49'], inspect=True)
        retrieve = dict(mode='retrieve', rationale='Check the next group.', queries=['SearchFixture'],
                        names=[], backends=[backend], offset=12, inspect=False)
        action = proof()
        action['proof_body'] = 'by trivial'
        with patch('midas.tools.bm25._build', side_effect=AssertionError('unexpected runtime indexing')), \
             app.start(root/'run', package=package, selection=selected, config=config, workflow='scripted',
                script=tuple(('prover', response(a)) for a in (search, lookup, retrieve, action))) as run:
            result = run.run()
            assert result.exit_code == 0, result
            owner = run.workflow.graph(run.writer).nodes[0].owner
            archives = helpers.advertised(run.writer, owner)
            assert len(archives) == 3, len(archives)
            pages = [helpers.page_text(records.read(run.writer, archive.pages[0], 16000)).decode()
                     for archive in archives]
            assert pages[0].count('Source:') == 12, pages[0]
            assert '50. SearchFixture.term49' in pages[0], pages[0]
            assert 'SearchFixture.term50' not in pages[0], pages[0]
            assert 'def term49 : Nat := 49' in pages[1], pages[1]
            assert 'SearchFixture.term12 : Nat' in pages[2], pages[2]
            assert 'SearchFixture.term23 : Nat' in pages[2], pages[2]
            assert run.writer.counter('owner:'+owner.obligation_id, 'retrieval') == 3
            assert not (app.cache/'search').exists()
            print('BM25 12/50 delivery, checked tail source, retrieval offset and proof continuation passed', flush=True)
        with app.resume(root/'run') as run:
            assert run.run().exit_code == 0
        print('Prepared package index reused with a fresh cache; authenticated resume passed', flush=True)


if __name__ == '__main__':
    main()
