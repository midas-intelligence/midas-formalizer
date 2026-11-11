"""Package search preparation with synthetic compiler outcomes; no Lean processes."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

from midas.application import Application, files, preparation
from midas.environment import Bootstrap, PackageConfig
from midas.tools import bm25
from tests.test_environment import Context, fixture, write


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = fixture(self.root)
        self.app = Application(self.root/'cache')
        self.config = PackageConfig('4.35.0-rc3', self.environment.toolchain,
            (('project', self.environment.roots['project']), ('checker', self.environment.roots['checker'])),
            ('project/Main.lean',), ('project', 'toolchain/lib/lean'))
        self.context = Context(self.root)
        self.context.cancellation = Event()
        self.context.deadline = time.monotonic()+30
        # Exercise the binary search worker protocol in-process alongside the
        # synthetic compiler. The production adapter now calls run_search.
        def run_search(request):
            import io
            from types import SimpleNamespace
            from midas.executor import Outcome
            from midas.tools.search_process import _main
            output = io.BytesIO()
            with patch('sys.stdout', SimpleNamespace(buffer=output)):
                _main(request)
            return SimpleNamespace(outcome=Outcome.COMPLETED,
                stdout=SimpleNamespace(data=output.getvalue()), stderr=SimpleNamespace(data=b''))
        self.context.run_search = run_search
        runtime = self.environment.directory/'runtime/ld.so'
        bootstrap = Bootstrap(self.environment.launcher, self.environment.manifest.launcher_sha256, ())
        self.enterContext(patch('midas.environment.package._runtime', return_value=(runtime, (runtime,))))
        self.enterContext(patch.object(Bootstrap, 'installed', return_value=bootstrap))
        self.enterContext(patch.object(preparation, 'operate', side_effect=lambda callback, **kw: callback(self.context)))

    def test_package_publishes_search_index_and_warm_preparation_reuses_it(self):
        from midas.environment.package import _publish
        def publish(stage, destination):
            self.assertTrue((stage/preparation.SEARCH_INDEX).is_file())
            _publish(stage, destination)
        with patch('midas.environment.package._publish', side_effect=publish):
            environment = self.app.package(self.root/'prepared', self.config)
        path = environment.directory/preparation.SEARCH_INDEX
        sources = preparation.FrozenSources(environment)
        with patch.object(bm25, '_build', side_effect=AssertionError('unexpected rebuild')), \
             patch.object(preparation.FrozenSources, 'read_artifact', side_effect=AssertionError('unexpected read')):
            self.assertEqual(preparation.index_sources(environment, self.context), path)
            with bm25.database(sources.index, sources, self.context.cancellation, self.context.deadline,
                               prepared_path=path) as db:
                self.assertEqual(db.execute('SELECT key FROM metadata').fetchone(), (sources.index.search_key,))
        self.assertFalse(self.app.cache.exists())
        self.assertNotIn(preparation.SEARCH_INDEX, environment.manifest.inventory.by_path)
        environment.validate()

    def test_index_failure_or_budget_exhaustion_does_not_publish_package(self):
        destination = self.root/'prepared'
        with patch.object(bm25, 'documents', side_effect=ValueError('index failed')):
            with self.assertRaisesRegex(ValueError, 'index failed'):
                self.app.package(destination, self.config)
        self.assertFalse(destination.exists())
        with self.assertRaisesRegex(ValueError, 'remaining byte allowance'):
            self.app.package(destination, replace(self.config, max_bytes=16*1024))
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob('.midas-package-*')))

    def test_attached_package_indexes_new_library_before_publication(self):
        base = self.app.package(self.root/'prepared', self.config)
        write(self.root/'library/My/Library.lean', b'namespace My\ndef helper : Nat := 1\nend My\n')
        write(self.root/'library/lib/My/Library.olean', b'compiled')
        config = write(self.root/'attachment.json',
            b'{"name":"library","path":"library","module_paths":["lib"],"source_roots":["."],"prefixes":["My"]}')
        attached = files.package(self.app, self.root/'attached', config, base=base)
        sources = preparation.FrozenSources(attached)
        with patch.object(bm25, '_build', side_effect=AssertionError('unexpected rebuild')):
            with bm25.database(sources.index, sources, self.context.cancellation, self.context.deadline,
                               prepared_path=attached.directory/preparation.SEARCH_INDEX) as db:
                self.assertEqual(bm25.ranked(db, 'My.helper', ('library',))[0][0], 'My.helper')
        self.assertNotEqual(sources.index.search_key, preparation.FrozenSources(base).index.search_key)
        self.assertTrue((base.directory/preparation.SEARCH_INDEX).is_file())


if __name__ == '__main__':
    unittest.main()
