"""Offline LeanEval boundaries; all compiler/comparator processes are substituted."""
from dataclasses import replace
from hashlib import sha256
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from midas.domain import encoding as codec
from midas.environment import Archive, ArchiveFile, describe
from midas.executor import Diagnostic, Outcome, ProcessResult
from midas.export.layout import _Reader, publish_files, reference
from midas.leaneval import attachments, cli, comparator, service, workspace
from midas.policy import Config
from tests.test_environment import benchmark_archive, benchmark_config


def changed(archive, path, data):
    return Archive(archive.config, tuple(replace(f, data=data) if f.path == path else f for f in archive.files))


def publish(files, destination, **options):
    return publish_files({p: reference(data) for p, data in files.items()},
                         _Reader(None, {reference(data): data for data in files.values()}), destination, **options)


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = benchmark_config(self.root)
        self.archive = benchmark_archive(self.config)
        lock = codec.load_json(next(f.data for f in self.archive.files if f.path == 'lake-manifest.json'))
        self.archive = changed(self.archive, 'lake-manifest.json', codec.canonical(dict(lock, version='1.1.0')))

    def test_submission_cannot_replace_trusted_inputs_or_helpers(self):
        starter = {'Submission.lean': b'sorry', 'Challenge.lean': b'trusted', 'Submission/H.lean': b'helper'}
        self.assertEqual(comparator.overlay(starter, {'Submission.lean': b'proof'})['Challenge.lean'], b'trusted')
        for supplied in ({}, {'Submission.lean': b'p', 'Challenge.lean': b'x'},
                         {'Submission.lean': b'p', 'Submission/H.lean': b'x'},
                         {'Submission.lean': b'p', '../escape.lean': b'x'},
                         {'Submission.lean': b'p', 'Submission/x.so': b'x'}):
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                comparator.overlay(starter, supplied)

    def test_attached_source_overlay_is_exact_and_preserves_benchmark_configuration(self):
        trusted = {'Submission.lean': b'sorry', 'Challenge.lean': b'trusted'}
        attached = {'Extra.lean': b'theorem Extra.x : True := by trivial'}
        submission = dict(attached, **{'Submission.lean': b'proof'})
        self.assertEqual(comparator.overlay(trusted, submission, attached=attached)['Extra.lean'], attached['Extra.lean'])
        for invalid in ({'Submission.lean': b'proof'}, dict(submission, **{'Extra.lean': b'axiom Extra.x : False'}),
                        dict(submission, **{'Challenge.lean': b'changed'})):
            with self.assertRaises(ValueError):
                comparator.overlay(trusted, invalid, attached=attached)
        lake = b'name="sample"\n[leanOptions]\nautoImplicit=false\n[[lean_lib]]\nname="Submission"\n'
        generated = attachments.lakefile(lake, attached)
        self.assertTrue(generated.startswith(lake))
        self.assertIn(b'name = "Extra"', generated)
        with self.assertRaises(ValueError):
            attachments.lakefile(lake, {'Submission/Injected.lean': b'bad'})

    def test_external_json_formatting_preserves_archive_bytes_and_comparator_inputs(self):
        files = tuple(replace(f, data=(json.dumps(codec.load_json(f.data), indent=2) + '\n').encode())
                      if f.path.endswith('.json') else f for f in self.archive.files)
        archive = Archive(self.archive.config, files)
        self.assertEqual(describe(archive), describe(self.archive))
        self.assertEqual(archive.files, files)
        self.assertNotEqual(archive.id, self.archive.id)
        self.assertEqual(comparator.lake_metadata(archive, ()), comparator.lake_metadata(self.archive, ()))
        # Internal persisted identities still require canonical bytes.
        with self.assertRaisesRegex(ValueError, 'Noncanonical'):
            Archive(b' ' + archive.config, files)

    def test_external_json_rejects_duplicate_keys_and_oversized_metadata(self):
        original = json.loads
        def bounded(payload, **kwargs):
            self.assertLessEqual(len(payload), 16384)
            return original(payload, **kwargs)
        for name in ('generated/sample/holes.json', 'generated/sample/config.json', 'lake-manifest.json'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Duplicate'):
                changed(self.archive, name, b'{"id":"sample", "id":"sample"}')
            with self.subTest(name=name), patch('json.loads', side_effect=bounded):
                with self.assertRaisesRegex(ValueError, 'byte limit'):
                    changed(self.archive, name, b' ' * 16385)

    def test_markers_require_success_and_complete_capture(self):
        success = Diagnostic(b'\n'.join(comparator.MARKERS), 128, False)
        empty = Diagnostic(b'', 0, False)
        result = ProcessResult(Outcome.COMPLETED, 0, 1, 0, 0, None, success, empty)
        comparator.acceptance(result)
        for rejected in (replace(result, returncode=1), replace(result, outcome=Outcome.CANCELLED),
                         replace(result, stdout=empty), replace(result, stdout=replace(success, truncated=True))):
            with self.subTest(rejected=rejected), self.assertRaises(ValueError):
                comparator.acceptance(rejected)

    def test_archived_fixtures_never_produce_judge_files(self):
        source = {'Submission.lean': b'proof'}
        self.assertIn('judge-submission/sample/Submission.lean', service.judge_files(self.archive, source))
        config = replace(self.config, allow_archived_fixture=True)
        archive = Archive(config.identity_bytes(), self.archive.files)
        archive = changed(archive, 'manifests/problems/sample.toml',
                          b'id="sample"\nstatus="archived"\nmodule="Sample"\nholes=["goal"]')
        self.assertTrue(describe(archive).fixture_only)
        self.assertEqual(service.judge_files(archive, source), {})
        archive = changed(self.archive, 'generated/sample/lakefile.toml', b'name="wrong"')
        with self.assertRaises(ValueError):
            service.judge_files(archive, source)

    def test_workspace_metadata_keeps_authoritative_pins_and_rejects_unknown_dependencies(self):
        lock, overrides = comparator.lake_metadata(self.archive, ())
        self.assertEqual(codec.load_json(lock)['name'], 'sample')
        self.assertEqual(codec.load_json(overrides)['packages'], [])
        with self.assertRaises(ValueError):
            comparator.lake_metadata(self.archive, ('missing',))
        files = (*self.archive.files, ArchiveFile('generated/sample/lake-manifest.json', b'{}'))
        archive = Archive(self.archive.config, tuple(sorted(files, key=lambda f: f.path)))
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            comparator.lake_metadata(archive, ())

    def test_workspace_overrides_use_root_labels_for_quoted_lake_names(self):
        package = dict(name='«lean-pool»', type='git', rev='1' * 40,
                       url='https://example.test/pool', configFile='lakefile.toml')
        lock = dict(version='1.3.0', packagesDir='.lake/packages', packages=[package])
        archive = changed(self.archive, 'lake-manifest.json', (json.dumps(lock, indent=2) + '\n').encode())
        archive = changed(archive, 'generated/sample/lakefile.toml',
            ('name="sample"\n[[require]]\nname="lean-pool"\ngit="https://example.test/pool"\nrev="' + '1' * 40 + '"\n').encode())
        self.assertEqual(describe(archive).required_dependencies, ('lean-pool',))
        encoded, overrides = comparator.lake_metadata(archive, ('lean-pool',))
        self.assertEqual(codec.load_json(encoded)['packages'][0]['name'], '«lean-pool»')
        entry = codec.load_json(overrides)['packages'][0]
        self.assertEqual(entry['dir'], '/deps/lean-pool')
        self.assertFalse(entry['inherited'])

    def test_large_publication_is_raw_atomic_and_never_overwrites(self):
        files = {'Submission.lean': b'--'+b'x'*2_000_000,
                 'verification.bin': workspace.evidence({'format': 'fixture'}, ((b'O', b'log'*100000),), 1024**2)}
        output = self.root/'output'
        with patch('json.dumps', side_effect=AssertionError('large JSON')), patch('json.loads', side_effect=AssertionError('large JSON')):
            publish(files, output)
            publish(files, output)
            self.assertEqual((output/'Submission.lean').read_bytes(), files['Submission.lean'])
            with self.assertRaises(ValueError):
                publish(dict(files, **{'Submission.lean': b'changed'}), output)
        self.assertEqual((output/'Submission.lean').read_bytes(), files['Submission.lean'])

    def test_publication_rejects_links_extra_directories_and_cancellation(self):
        output = self.root/'output'
        publish({'a': b'x'}, output)
        (output/'extra').mkdir()
        with self.assertRaises(ValueError):
            publish({'a': b'x'}, output)
        (self.root/'link').symlink_to(output, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            publish({'a': b'x'}, self.root/'link')
        def cancelled():
            raise RuntimeError('cancelled')
        with self.assertRaises(RuntimeError):
            publish({'a': b'x'}, self.root/'cancelled', check=cancelled)
        self.assertFalse((self.root/'cancelled').exists())
        self.assertFalse(list(self.root.glob('.midas-export-*')))

    def test_raw_non_utf8_evidence_roundtrips_without_json(self):
        from midas.environment.inventory import read_rows
        data = b'\xff\x00'*100000
        with patch('json.dumps', side_effect=AssertionError('JSON evidence')), patch('json.loads', side_effect=AssertionError('JSON evidence')):
            encoded = workspace.evidence({'format': 'fixture'}, ((b'O', data),), 1024**2)
            rows = list(read_rows(io.BytesIO(encoded), b'MIDASEVL\x01'))
            self.assertEqual(rows[1], (b'O', data))
            with self.assertRaises(ValueError):
                workspace.evidence({}, ((b'O', data),), 1024)

    def test_selection_requires_all_trusted_outputs(self):
        selected = NS(source=b'', selected=NS(request=NS(source='Submission.lean'), targets=(NS(selector='Submission.goal', kind='theorem'),)))
        workspace.check_selection(selected, self.archive)
        for source, targets in (('Main.lean', selected.selected.targets), ('Submission.lean', ()),
                                ('Submission.lean', (NS(selector='Submission.other', kind='theorem'),)),
                                ('Submission.lean', (NS(selector='Submission.goal', kind='def'),))):
            with self.assertRaises(ValueError):
                workspace.check_selection(NS(source=b'', selected=NS(request=NS(source=source), targets=targets)), self.archive)
        with self.assertRaises(ValueError):
            workspace.check_selection(NS(source=b'changed', selected=selected.selected), self.archive)

    def test_bundle_rejects_partial_exports_or_a_different_benchmark(self):
        bound = NS(environment_id='e', selection=NS(id='s'), library=None)
        values = dict(environment_id='e', selection_id='s', allow_sorry=False,
                      decode=lambda: {'packet': {'logical': 'Submission.lean',
                          'inventory': service.reference(codec.encode([])).to_dict()}})
        service.check_bundle(NS(**values), bound)
        for diff in (dict(environment_id='other'), dict(selection_id='other'), dict(allow_sorry=True),
                     dict(decode=lambda: {'packet': {'logical': 'Main.lean'}})):
            with self.assertRaises(ValueError):
                service.check_bundle(NS(**dict(values, **diff)), bound)
        bound.library = NS(payload=codec.encode([{'name': 'Trusted'}]))
        with self.assertRaisesRegex(ValueError, 'dependency trust'):
            service.check_bundle(NS(**values), bound)

    def test_dependency_replay_is_explicit_and_optional(self):
        args = ['prepare', '/workspace', '--package', '/package', '--verification-config', '/verification.json']
        self.assertFalse(cli.parser().parse_args(args).replay_dependencies)
        self.assertTrue(cli.parser().parse_args([*args, '--replay-dependencies']).replay_dependencies)

    def test_ordinary_commands_delegate_without_constructing_an_application(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, 'midas_main', return_value=17) as ordinary:
            self.assertEqual(cli.main(['status', '/run', '--json'], stdout=out, stderr=err), 17)
            self.assertEqual(ordinary.call_args.args[0], ['status', '/run', '--json'])

    def test_workspace_start_uses_standard_cli_and_injects_library(self):
        bound = NS(package=self.root/'package', selection='selected', library='library', library_reader='reader', imports=('Extra',))
        work = self.root/'work'
        work.mkdir()
        (work/'workspace.bin').write_bytes(codec.encode(dict(package=str(bound.package), environment='env')))
        received = []
        environment = NS(directory=bound.package, id='env')
        app = NS(start=lambda *a, **kw: received.append(kw), environment=lambda *a, **kw: environment)
        def ordinary(argv, **kwargs):
            adapter = kwargs['application_factory'](self.root/'cache')
            adapter.start('run', package='placeholder', selection='placeholder', workflow='supplied')
            self.assertIn(str(bound.package), argv)
            self.assertIn(str(work/'selection.bin'), argv)
            return 0
        with patch.object(cli.workspace, 'load', return_value=bound) as load, patch.object(cli, 'midas_main', side_effect=ordinary):
            self.assertEqual(cli.main(['start', 'run', '--workspace', str(work)], application_factory=lambda *a, **kw: app), 0)
            self.assertIs(load.call_args.kwargs['package'], environment)
        self.assertIs(received[0]['package'], environment)
        self.assertEqual(received[0]['library'], 'library')
        self.assertEqual(received[0]['workflow'], 'supplied')
        for flag in ('--package', '--source', '--selection', '--target'):
            err = io.StringIO()
            self.assertEqual(cli.main(['start', 'run', '--workspace', str(work), flag, 'x'], stderr=err), 2)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_application import Contracts as Fixture
        from midas.domain import (ByteSpan, SelectionInputs, SelectionRequest, SourceBinding,
                                  encode_submission, freeze_selection)
        from midas.source import parse_identifier
        self.fixture = f = Fixture()
        f.setUp()
        self.addCleanup(f.tearDown)
        self.root = f.root
        source = b'theorem Submission.goal : True := by sorry\n'
        name = codec.canonical(parse_identifier('Submission.goal'))
        declaration = replace(f.selected.declarations[0], selector='Submission.goal', constant=name,
            declaration=ByteSpan(0, len(source)-1), replacement=ByteSpan(source.index(b':='), len(source)-1))
        inputs = SelectionInputs(f.environment.id, (SourceBinding('Submission.lean', sha256(source).hexdigest(), len(source)),))
        f.selected = freeze_selection(SelectionRequest('Submission.lean', ('Submission.goal',)), source,
            (declaration,), frozenset(f.selected.imported_constants), {name: f.selected.signatures[0]}, inputs)
        f.environment.selection_inputs = lambda: inputs
        f.environment.toolchain = self.root/'toolchain'
        with f.start() as run:
            run.submit(encode_submission(dict(format='midas-scoped-candidates', submission_id=str(uuid4()),
                selection_id=f.selected.id, definitions=[], entries=[dict(key='parent',
                    subject=dict(kind='target', name='Submission.goal'), proof='by trivial', dependencies=[],
                    review=None, revision_of=None, definition_scope=dict(statement=0, proof=0))])))
            self.assertEqual(f.finish(run).exit_code, 0)
            run.materialize(self.root/'bundle')
        self.verification = benchmark_config(self.root)
        archive = changed(benchmark_archive(self.verification), 'generated/sample/Submission.lean', source)
        self.bound = NS(directory=self.root/'workspace', package=self.root/'package', environment_id=f.environment.id,
                        selection=f.selected, archive=archive, library=None, attachments=())
        self.options = dict(workspace=self.bound.directory, verification=self.verification,
                            runtime=self.root/'runtime', config=Config())
        self.comparison = self.enterContext(patch.object(service.comparator, 'run', return_value=b'fresh comparator'))
        self.enterContext(patch.object(service, 'load', return_value=self.bound))

    def publish(self):
        return service.finalize(self.fixture.app, self.root/'bundle', self.root/'published', **self.options)

    def replace_recorded_file(self, path, data):
        from midas.export.layout import reference
        published = self.root/'published'
        (published/path).write_bytes(data)
        manifest = codec.decode((published/'manifest.bin').read_bytes())
        manifest['files'] = [[p, reference(data).to_dict() if p == path else ref] for p, ref in manifest['files']]
        (published/'manifest.bin').write_bytes(codec.encode(manifest))

    def test_fresh_audit_and_comparison_ignore_forged_historical_success(self):
        self.assertTrue(self.publish()['submission_ready'])
        self.replace_recorded_file('verification.bin', b'forged historical success')
        result, report = service.verify_submission(self.fixture.app, self.root/'published', **self.options)
        self.assertTrue(result['verified'])
        self.assertEqual(self.comparison.call_count, 2)
        self.assertNotEqual(report, b'forged historical success')

    def test_copy_tampering_with_recomputed_inventory_is_rejected_before_comparison(self):
        self.publish()
        self.replace_recorded_file('submission/Submission.lean', b'theorem Submission.goal : False := by sorry')
        with self.assertRaisesRegex(ValueError, 'copies'):
            service.verify_submission(self.fixture.app, self.root/'published', **self.options)
        self.assertEqual(self.comparison.call_count, 1)

    def test_failed_comparator_never_publishes_a_judge_package(self):
        self.comparison.side_effect = ValueError('kernel rejected')
        with self.assertRaisesRegex(ValueError, 'kernel rejected'):
            self.publish()
        self.assertFalse((self.root/'published').exists())


if __name__ == '__main__':
    unittest.main()
