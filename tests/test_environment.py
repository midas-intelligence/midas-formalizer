"""Synthetic filesystem/contract checks only: no Lean, bubblewrap or subprocesses."""
from dataclasses import replace
from hashlib import sha1, sha256
import io
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from midas.domain.encoding import canonical
from midas.domain import ResourceLimits
from midas.environment import (Archive, ArchiveFile, AuthorizedInput, Bootstrap, Dependency,
    Environment, EnvironmentError, File, Inventory, Manifest, PackageConfig, Pin, Settings,
    Tool, VerificationConfig, archive_benchmark, attach, check_version, describe,
    inventory_modules, lake_pins, load_archive, load_config, load_manifest,
    match_relocation, resolve_closure, safe_read, scan, version_number)
from midas.environment.archive import FORMAT, ROOT_FILES, STARTER, TOOLS
from midas.environment.dependencies import GitReader
from midas.environment.inventory import MAX_INVENTORY, U64, chunks, read_rows, reset_cache
from midas.environment.manifest import MAGIC, combine
from midas.environment.package import reset_admissions
from midas.executor import Mount, Outcome


def write(path, data=b'x', *, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755 if executable else 0o644)
    return path


class Context:
    def __init__(self, workspace, *, fail=False):
        self.workspace, self.fail, self.launches = workspace, fail, []
        self.limits = ResourceLimits(120000, 4096, 1024**2, 64*1024**2)

    def check(self):
        pass

    def run_process(self, launch):
        self.launches.append(launch)
        report = b'Lean (version 4.35.0-rc3, x86_64-unknown-linux-gnu, commit test, Release)\n' if '--version' in launch.argv else b''
        return SimpleNamespace(outcome=Outcome.COMPILER_ERROR if self.fail else Outcome.COMPLETED,
                               returncode=1 if self.fail else 0, stdout=SimpleNamespace(data=report),
                               stderr=SimpleNamespace(data=b'failed' if self.fail else b''))


def fixture(base):
    toolchain, package = base / 'installed', base / 'package'
    write(toolchain / 'bin/lean', b'compiler', executable=True)
    write(toolchain / 'bin/leanc', b'native', executable=True)
    write(toolchain / 'lib/lean/Init.olean', b'init')
    write(package / 'project/Main.lean', b'example : True := by trivial\n')
    write(package / 'checker/Resource.lean', b'-- resource\n')
    write(package / 'runtime/ld.so', b'loader', executable=True)
    launcher = write(base / 'bwrap', b'launcher', executable=True)
    roots = {'toolchain': toolchain, 'project': package / 'project',
             'runtime': package / 'runtime', 'checker': package / 'checker'}
    spec = Settings('4.35.0-rc3', 'linux-test', 'ELF-test', ('project/Main.lean',),
                    ('project', 'toolchain/lib/lean'), 'runtime/ld.so')
    inventory = combine({name: scan(path)[0] for name, path in roots.items()})
    manifest = Manifest(spec, inventory, sha256(b'launcher').hexdigest())
    with (package / 'environment.bin').open('wb') as output:
        manifest.save(output)
    (package / 'location.json').write_bytes(canonical({'toolchain': str(toolchain), 'roots': {}}))
    return Environment(package, manifest, toolchain, str(launcher))


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        reset_cache()

    def test_streaming_scan_cache_and_changed_same_size_bytes(self):
        write(self.root / 'a', b'a' * 2000000)
        with patch('json.dumps', side_effect=AssertionError('large JSON')), patch('json.loads', side_effect=AssertionError('large JSON')):
            first, _ = scan(self.root)
            with patch('midas.environment.inventory.os.read', side_effect=AssertionError('cache reread')):
                self.assertEqual(scan(self.root)[0], first)
            write(self.root / 'a', b'b' * 2000000)
            self.assertNotEqual(scan(self.root)[0], first)

    def test_digest_eviction_reset_and_replacement_with_preserved_mtime(self):
        from midas.environment import inventory as cache
        paths = [write(self.root / name, name.encode()) for name in ('a', 'b', 'c')]
        with patch.object(cache, 'DIGEST_ENTRIES', 2):
            for path in paths[:2]:
                cache.file_hash(path)
            with patch.object(cache.os, 'read', side_effect=AssertionError('cache reread')):
                self.assertEqual(cache.file_hash(paths[0]), sha256(b'a').hexdigest())
            cache.file_hash(paths[2])
            self.assertEqual(len(cache._digests), 2)
            with patch.object(cache.os, 'read', wraps=os.read) as reads:
                self.assertEqual(cache.file_hash(paths[1]), sha256(b'b').hexdigest())
                self.assertTrue(reads.called)  # The least recently used file was evicted.
        before = paths[1].stat()
        replacement = write(self.root / 'replacement', b'x')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(paths[1])
        self.assertEqual(cache.file_hash(paths[1]), sha256(b'x').hexdigest())
        lock = cache._lock
        reset_cache()
        self.assertFalse(cache._digests)
        self.assertIsNot(cache._lock, lock)

    def test_root_validation_detects_earlier_file_changed_during_later_read(self):
        from midas.environment import inventory as cache
        from midas.environment.package import _check_root
        first = write(self.root/'a', b'first')
        second = write(self.root/'b', b'second')
        expected, _ = scan(self.root)
        second_inode = second.stat().st_ino
        original = cache._digest
        def change_earlier_file(fd, before, check):
            if before[1] == second_inode:
                # The first descriptor has closed. Its own before/after checks
                # passed, and an in-place write does not change directory mtime.
                first.write_bytes(b'other')
            return original(fd, before, check)
        with patch.object(cache, '_digest', side_effect=change_earlier_file):
            with self.assertRaisesRegex(EnvironmentError, 'Frozen input differs'):
                _check_root(self.root, expected)

    def test_links_special_files_and_symlink_parents(self):
        write(self.root / 'a')
        (self.root / 'alias').symlink_to('a')
        with self.assertRaises(ValueError):
            scan(self.root)
        inventory, _ = scan(self.root, links=True)
        self.assertEqual(inventory.by_path['a'].sha256, inventory.by_path['alias'].sha256)
        (self.root / 'alias').unlink()
        (self.root / 'alias').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            scan(self.root, links=True)
        (self.root / 'alias').unlink()
        os.mkfifo(self.root / 'fifo')
        with self.assertRaises(ValueError):
            scan(self.root)
        with self.assertRaises(ValueError):
            safe_read(self.root / 'fifo', 10)
        (self.root / 'fifo').unlink()
        (self.root / 'dir').mkdir()
        (self.root / 'alias').symlink_to('dir', target_is_directory=True)
        with self.assertRaises(ValueError):
            scan(self.root, links=True)
        with self.assertRaises(OSError):
            scan(self.root / 'alias')

    def test_limits_and_mutation_during_inventory(self):
        write(self.root / 'a', b'abc')
        for kwargs in ({'maximum': 2}, {'max_entries': 1}):
            write(self.root / 'b')
            with self.assertRaises(ValueError):
                scan(self.root, **kwargs)
        calls = 0
        def mutate():
            nonlocal calls
            calls += 1
            if calls == 5:
                write(self.root / 'c')
        with self.assertRaisesRegex(ValueError, 'changed'):
            scan(self.root, check=mutate)

    def test_inventory_shape_and_flat_decoder_bounds(self):
        for path in ('../x', '/x', 'a//b', 'a/./b', 'a\\b', 'a\0b'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                File(path, 0, False, '0' * 64)
        with self.assertRaises(ValueError):
            Inventory(('.',), (File('a/b', 0, False, '0' * 64),))
        with self.assertRaises(ValueError):
            list(read_rows(io.BytesIO(b'TEST\x01' + U64.pack(MAX_INVENTORY + 1)), b'TEST'))
        with self.assertRaises(ValueError):
            list(read_rows(io.BytesIO(b'TEST\x21'), b'TEST'))
        with self.assertRaises(ValueError):
            list(read_rows(io.BytesIO(b'TEST\0extra'), b'TEST'))


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        reset_admissions()
        self.env = fixture(self.root)

    def restore(self, **kwargs):
        return Environment.restore(self.env.directory, launcher=self.env.launcher, **kwargs)

    def test_manifest_binary_roundtrip_no_json_and_hash_only_bindings(self):
        with patch('json.dumps', side_effect=AssertionError('manifest JSON')), patch('json.loads', side_effect=AssertionError('manifest JSON')):
            encoded = io.BytesIO()
            self.env.manifest.save(encoded)
            decoded = load_manifest(encoded.getvalue(), expected_id=self.env.id)
        self.assertEqual(decoded, self.env.manifest)
        inputs = self.env.selection_inputs()
        self.assertEqual(inputs.environment_id, self.env.id)
        self.assertEqual(inputs.sources[0].source, 'Main.lean')
        self.assertFalse(hasattr(inputs, 'manifest'))
        with self.assertRaises(ValueError):
            load_manifest(encoded.getvalue(), expected_id='env_' + 'f' * 64)

    def test_manifest_rejects_reordering_duplicates_and_bad_references(self):
        rows = list(read_rows(io.BytesIO(b''.join(self.env.manifest.chunks())), MAGIC))
        directory = next(i for i, r in enumerate(rows) if r[0] == b'D')
        reordered = rows[:]
        reordered[directory:directory+2] = reversed(reordered[directory:directory+2])
        for altered in (rows + [rows[-1]], reordered, rows[:-1]):
            with self.assertRaises(ValueError):
                load_manifest(b''.join(chunks(altered, MAGIC)), expected_id=self.env.id)
        with self.assertRaises(ValueError):
            Manifest(replace(self.env.manifest.settings, project_inputs=('project/Missing.lean',)),
                     self.env.manifest.inventory, self.env.manifest.launcher_sha256)

    def test_large_manifest_cache_preserves_exact_admission_and_resets(self):
        from midas.environment.manifest import reset_manifest_cache
        inventory = self.env.manifest.inventory
        extra = tuple(File(f'project/File{n:04}.lean', 1000000, False, '1' * 64) for n in range(1000))
        inventory = Inventory(inventory.directories, tuple(sorted((*inventory.files, *extra), key=lambda f: f.path)))
        reset_manifest_cache()
        with patch('json.dumps', side_effect=AssertionError('large JSON')), patch('json.loads', side_effect=AssertionError('large JSON')):
            manifest = Manifest(self.env.manifest.settings, inventory, self.env.manifest.launcher_sha256)
            encoded = b''.join(manifest.chunks())
            self.assertGreater(len(encoded), 16384)
            loaded = load_manifest(encoded)
            with patch('midas.environment.manifest._decode_manifest', side_effect=AssertionError('unneeded decode')):
                self.assertIs(load_manifest(encoded), loaded)
                with self.assertRaises(ValueError):
                    load_manifest(encoded, expected_id='env_' + 'f' * 64)
            reset_manifest_cache()
            self.assertEqual(load_manifest(encoded), loaded)

    def test_settings_versions_and_profile_overrides(self):
        self.assertEqual(version_number('leanprover/lean4:v4.35.0-rc3'), '4.35.0-rc3')
        with self.assertRaises(ValueError):
            version_number('nightly')
        with self.assertRaises(ValueError):
            check_version('Lean (version 4.35.0-rc2, x)', '4.35.0-rc3')
        for values in ({'variables': (('LEAN_PATH', '/host'),)}, {'profile': 'host'},
                       {'options': (('x', 'a\0b'),)}, {'runtime_loader': 'toolchain/ld.so'}):
            with self.assertRaises(ValueError):
                replace(self.env.manifest.settings, **values)

    def test_restore_relocation_uses_saved_manifest(self):
        relocated = self.root / 'relocated'
        shutil.copytree(self.env.directory, relocated)
        (relocated / 'environment.bin').write_bytes(b'untrusted loose manifest')
        restored = Environment.restore(relocated, manifest=self.env.manifest,
            expected_id=self.env.id, toolchain=self.env.toolchain, launcher=self.env.launcher)
        self.assertEqual(restored.id, self.env.id)
        self.assertEqual(restored.roots['project'], relocated / 'project')
        with self.assertRaises(ValueError):
            Environment.restore(relocated, launcher=self.env.launcher)

    def test_package_exact_hashes_additions_modes_and_launcher(self):
        self.restore()
        write(self.env.directory / 'project/Main.lean', b'changed')
        with self.assertRaises(ValueError):
            self.restore()
        self.env = fixture(self.root)
        write(self.env.directory / 'project/Extra.lean')
        with self.assertRaises(ValueError):
            self.restore()
        (self.env.directory / 'project/Extra.lean').unlink()
        (self.env.directory / 'project/Main.lean').chmod(0o755)
        with self.assertRaises(ValueError):
            self.restore()
        self.env = fixture(self.root)
        write(Path(self.env.launcher), b'changed', executable=True)
        with self.assertRaises(ValueError):
            self.restore()

    def test_toolchain_metadata_policy_and_mandatory_exact_hashes(self):
        write(self.env.toolchain / 'bin/lean', b'COMPILER', executable=True)
        # Same size/mode satisfies default installation admission, as in v2.
        self.restore(hash_toolchain=False)
        with self.assertRaises(ValueError):
            self.restore(hash_toolchain=True)
        with patch.dict(os.environ, {'MIDAS_VERIFY_TOOLCHAIN': 'invalid'}), self.assertRaises(ValueError):
            self.restore()
        write(self.env.toolchain / 'new')
        reset_admissions()
        with self.assertRaises(ValueError):
            self.restore(hash_toolchain=False)

    def test_toolchain_admission_cache_is_bounded_to_identity_and_policy(self):
        self.restore()
        with patch('midas.environment.package._check_root', wraps=__import__('midas.environment.package', fromlist=['_check_root'])._check_root) as verify:
            self.restore()
            self.assertNotIn(self.env.toolchain, [call.args[0] for call in verify.call_args_list])
        changed = write(self.env.toolchain / 'lib/lean/New.olean')
        with self.assertRaises(ValueError):
            self.env.validate(fresh=True)
        changed.unlink()
        self.env.validate(hash_toolchain=True)

    def test_application_reuses_admitted_handles_but_preserves_validation(self):
        from midas.application import Application
        app = Application(self.root/'cache')
        with patch.dict(os.environ, {'MIDAS_VERIFY_TOOLCHAIN': '0'}):
            with patch('midas.environment.package.scan', wraps=scan) as scanning:
                self.assertIs(app.environment(self.env), self.env)
                self.assertTrue(scanning.called)
                scanning.reset_mock()
                self.assertIs(app.environment(self.env, expected_id=self.env.id), self.env)
                scanning.assert_not_called()
                with self.assertRaisesRegex(ValueError, 'hash changed'):
                    app.environment(self.env, expected_id='env_'+'f'*64)
            with patch.dict(os.environ, {'MIDAS_VERIFY_TOOLCHAIN': '1'}):
                with patch('midas.environment.package.scan', wraps=scan) as scanning:
                    app.environment(self.env)
                    self.assertTrue(any(call.args[0] == self.env.toolchain and call.kwargs['hash_content']
                                        for call in scanning.call_args_list))
        write(self.env.directory/'project/Main.lean', b'changed')
        with self.assertRaises(EnvironmentError):
            self.env.validate()
        with self.assertRaises(EnvironmentError):
            self.restore()
        with patch('midas.environment.package.os.getpid', return_value=-1), self.assertRaises(EnvironmentError):
            app.environment(self.env)

    def test_compiled_module_shadowing_and_duplicate_search(self):
        write(self.env.directory / 'project/Init.olean', b'init')
        roots = {name: scan(path, links=name == 'toolchain')[0] for name, path in self.env.roots.items()}
        with self.assertRaisesRegex(ValueError, 'Shadowed'):
            Manifest(self.env.manifest.settings, combine(roots), self.env.manifest.launcher_sha256)

    def test_materialize_exact_inputs_and_readonly_recipe(self):
        source = write(self.root / 'proof', b'example : True := by trivial\n')
        authorized = AuthorizedInput('Proof.lean', source, sha256(source.read_bytes()).hexdigest(), source.stat().st_size)
        inputs = self.env.materialize((authorized,), self.root / 'inputs')
        recipe = self.env.launch(('/inputs/Proof.lean',), inputs=inputs)
        self.assertEqual(recipe.launcher_sha256, self.env.manifest.launcher_sha256)
        self.assertFalse(any(m.writable for m in recipe.mounts))
        self.assertEqual(dict(recipe.variables)['LEAN_PATH'], '/inputs:/project:/toolchain/lib/lean')
        self.assertFalse(hasattr(recipe, 'manifest'))
        (inputs.directory / 'Proof.lean').chmod(0o644)
        write(inputs.directory / 'Proof.lean', b'changed')
        with self.assertRaises(ValueError):
            self.env.launch(('/inputs/Proof.lean',), inputs=inputs)

    def test_heartbeat_limit_is_a_launch_option(self):
        original = self.env.id
        for limit in (0, 12345):
            recipe = self.env.launch(('--run', '/checker/Resource.lean'), max_heartbeat=limit)
            self.assertIn('-DmaxHeartbeats=' + str(limit), recipe.argv)
            self.assertLess(recipe.argv.index('-DmaxHeartbeats=' + str(limit)), recipe.argv.index('--run'))
        self.assertEqual(self.env.id, original)
        for invalid in (-1, True, 1.5):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.env.launch((), max_heartbeat=invalid)
        with self.assertRaises(ValueError):
            self.env.launch((), executable='toolchain/bin/leanc', max_heartbeat=12345)

    def test_recursion_depth_limit_is_a_launch_option(self):
        original = self.env.id
        recipe = self.env.launch(('--run', '/checker/Resource.lean'), max_rec_depth=321)
        self.assertIn('-DmaxRecDepth=321', recipe.argv)
        self.assertLess(recipe.argv.index('-DmaxRecDepth=321'), recipe.argv.index('--run'))
        self.assertEqual(self.env.id, original)
        for invalid in (0, -1, True, 1.5):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.env.launch((), max_rec_depth=invalid)
        with self.assertRaises(ValueError):
            self.env.launch((), executable='toolchain/bin/leanc', max_rec_depth=321)

    def test_materialize_rejects_shadowing_overlaps_hashes_and_extras(self):
        source = write(self.root / 'source', b'x')
        item = AuthorizedInput('Proof.lean', source, sha256(b'x').hexdigest(), 1)
        for name in ('Init.lean', 'Init.olean', 'Init.olean.private'):
            with self.assertRaisesRegex(ValueError, 'shadows'):
                self.env.materialize((replace(item, path=name),), self.root / 'inputs')
        with self.assertRaises(ValueError):
            self.env.materialize((replace(item, sha256='f' * 64),), self.root / 'inputs')
        self.assertFalse((self.root / 'inputs').exists())
        with self.assertRaises(ValueError):
            self.env.materialize((item,), self.env.roots['project'] / 'inputs')
        with self.assertRaises(ValueError):
            self.env.materialize((replace(item, path='x/y'), replace(item, path='x')), self.root / 'inputs')

    def test_writable_build_only_and_nonadmitted_executable(self):
        build = self.root / 'build'
        build.mkdir()
        recipe = self.env.launch(('-o', '/build/out'), build=build, executable='toolchain/bin/leanc')
        self.assertEqual([m.target for m in recipe.mounts if m.writable], ['/build'])
        self.assertTrue(any(m.target.endswith('ld-linux-x86-64.so.2') for m in recipe.mounts))
        with self.assertRaises(ValueError):
            self.env.launch((), executable='toolchain/bin/missing')
        with self.assertRaises(ValueError):
            self.env.launch((), build=self.env.roots['project'])

    def config(self):
        return PackageConfig('4.35.0-rc3', self.env.toolchain,
            (('project', self.env.roots['project']), ('checker', self.env.roots['checker'])),
            ('project/Main.lean',), ('project', 'toolchain/lib/lean'))

    def prepare(self, destination, *, fail=False, config=None):
        bootstrap = Bootstrap(self.env.launcher, self.env.manifest.launcher_sha256, ())
        runtime = self.env.directory / 'runtime/ld.so'
        with patch('midas.environment.package._runtime', return_value=(runtime, (runtime,))):
            return Environment.prepare(destination, config or self.config(), Context(self.root, fail=fail), bootstrap=bootstrap)

    def test_prepare_publishes_only_checked_frozen_bytes(self):
        prepared = self.prepare(self.root / 'new-package')
        self.assertTrue((prepared.directory / 'environment.bin').exists())
        self.assertFalse((prepared.directory / 'toolchain').exists())
        self.assertEqual(prepared.selection_inputs().sources, self.env.selection_inputs().sources)
        write(self.env.roots['project'] / 'Main.lean', b'changed after freezing')
        prepared.validate()
        with self.assertRaises(ValueError):
            self.prepare(prepared.directory)

    def test_dependencies_are_referenced_without_copying_and_changes_are_rejected(self):
        dependency = self.env.roots['checker']
        write(dependency / 'large.bin', b'x' * (2 * 1024**2))
        from midas.environment.package import copy_file
        def copy(source, *args, **kwargs):
            self.assertFalse(Path(source).is_relative_to(dependency), 'dependency was copied')
            return copy_file(source, *args, **kwargs)
        with patch('midas.environment.package.copy_file', side_effect=copy):
            prepared = self.prepare(self.root / 'referenced')
        self.assertEqual(prepared.roots['checker'], dependency)
        self.assertFalse((prepared.directory / 'checker').exists())
        self.assertLess(sum(p.stat().st_size for p in prepared.directory.rglob('*') if p.is_file()), 16384)
        recipe = prepared.launch(('--version',))
        mount = next(m for m in recipe.mounts if m.target == '/checker')
        self.assertEqual(mount.source, str(dependency))
        self.assertFalse(mount.writable)
        restored = Environment.restore(prepared.directory, launcher=prepared.launcher)
        self.assertEqual(restored.id, prepared.id)
        self.assertEqual(restored.roots['checker'], dependency)
        write(dependency / 'large.bin', b'y' * (2 * 1024**2))
        with self.assertRaises(ValueError):
            restored.validate()
        with self.assertRaises(ValueError):
            Environment.restore(prepared.directory, launcher=prepared.launcher)

    def test_referenced_dependencies_relocate_only_with_matching_content(self):
        prepared = self.prepare(self.root / 'referenced')
        dependency = self.root / 'moved-checker'
        shutil.copytree(prepared.roots['checker'], dependency)
        moved = Environment.restore(prepared.directory, launcher=prepared.launcher,
                                    references={'checker': dependency})
        self.assertEqual(moved.id, prepared.id)
        self.assertEqual(moved.roots['checker'], dependency)
        write(dependency / 'extra')
        with self.assertRaises(ValueError):
            Environment.restore(prepared.directory, launcher=prepared.launcher,
                                references={'checker': dependency})
        with self.assertRaises(ValueError):
            Environment.restore(prepared.directory, launcher=prepared.launcher,
                                references={'project': prepared.roots['project']})

    def test_referenced_internal_file_aliases_need_no_materialized_copy(self):
        dependency = self.env.roots['checker']
        (dependency / 'alias.txt').symlink_to('Resource.lean')
        prepared = self.prepare(self.root / 'referenced')
        prepared.validate()
        self.assertTrue((dependency / 'alias.txt').is_symlink())
        self.assertEqual(prepared.manifest.inventory.by_path['checker/alias.txt'].sha256,
                         prepared.manifest.inventory.by_path['checker/Resource.lean'].sha256)
        (dependency / 'alias.txt').unlink()
        (dependency / 'alias.txt').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            prepared.validate()

    def test_prepare_failure_budget_and_changing_inputs_do_not_publish(self):
        destination = self.root / 'new-package'
        with self.assertRaises(ValueError):
            self.prepare(destination, fail=True)
        self.assertFalse(destination.exists())
        with self.assertRaises(ValueError):
            self.prepare(destination, config=replace(self.config(), max_bytes=1))
        self.assertFalse(destination.exists())
        with patch('midas.environment.package.output', side_effect=ValueError('preflight')):
            with self.assertRaises(ValueError):
                self.prepare(destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('.midas-package-*')), [])

    def test_missing_package_references_fail_before_large_inventory_or_copy(self):
        for config in (replace(self.config(), search_paths=('checker/missing',)),
                       replace(self.config(), project_inputs=('project/Missing.lean',))):
            with self.subTest(config=config), patch('midas.environment.package.scan',
                    side_effect=AssertionError('unnecessary inventory')):
                with self.assertRaisesRegex(ValueError, 'Missing referenced'):
                    self.prepare(self.root / 'missing', config=config)
            self.assertFalse((self.root / 'missing').exists())

    def test_publication_race_preserves_existing_destination(self):
        destination = self.root / 'race'
        runtime = self.env.directory / 'runtime/ld.so'
        bootstrap = Bootstrap(self.env.launcher, self.env.manifest.launcher_sha256, ())
        def race(_):
            write(destination / 'existing', b'preserve')
        with patch('midas.environment.package._runtime', return_value=(runtime, (runtime,))):
            with self.assertRaises(EnvironmentError):
                Environment.prepare(destination, self.config(), Context(self.root), bootstrap=bootstrap, _before_publish=race)
        self.assertEqual((destination / 'existing').read_bytes(), b'preserve')
        self.assertEqual(list(destination.iterdir()), [destination / 'existing'])

    def test_attachment_preserves_original_pins_and_preflights_imports(self):
        library = self.root / 'library'
        write(library / 'My/Library.lean', b'example : True := by trivial\n')
        write(library / 'lib/My/Library.olean', b'compiled')
        dependency = Dependency('library', library, ('lib',))
        runtime = self.env.directory / 'runtime/ld.so'
        bootstrap = Bootstrap(self.env.launcher, self.env.manifest.launcher_sha256, ())
        context = Context(self.root)
        with patch('midas.environment.package._runtime', return_value=(runtime, (runtime,))):
            prepared, result = attach(self.env, self.root / 'attached', dependency, ('My',), context, bootstrap=bootstrap)
        self.assertEqual(result.modules, ('My.Library',))
        self.assertNotEqual(result.environment_id, self.env.id)
        self.assertEqual(prepared._inventories['project'], self.env._inventories['project'])
        self.assertTrue(any('/inputs/Imports.lean' in launch.argv for launch in context.launches))
        with self.assertRaises(ValueError):
            attach(self.env, self.root / 'bad', dependency, ('Other',), context)


def lock(packages=()):
    return canonical({'packagesDir': '.lake/packages', 'packages': [dict(name=p.name, rev=p.revision,
        url=p.url, configFile=p.config, type='git') for p in packages]})


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = Context(self.root)
        self.pin = Pin('dep', '1' * 40, 'https://example.test/dep', 'lakefile.toml')
        self.dep = Dependency('dep', self.root / 'dep', ('lib',))
        self.metadata = {'lake-manifest.json': lock(), 'lakefile.toml': b'name = "dep"', 'lean-toolchain': b'leanprover/lean4:v4.35.0-rc3'}
        for path, data in self.metadata.items():
            write(self.dep.path / path, data)

    def closure(self, *, required=('dep',), mappings=None):
        with patch.object(GitReader, 'head', return_value=self.pin.revision), patch.object(GitReader, 'committed', side_effect=lambda _, path, **kwargs: self.metadata[path]):
            return resolve_closure(lock((self.pin,)), required, mappings or (self.dep,), self.context)

    def test_lake_metadata_cap_pins_and_root_lock_overrides(self):
        self.assertEqual(lake_pins(lock((self.pin,)))['dep'], self.pin)
        with patch('json.loads', side_effect=AssertionError('large JSON')), self.assertRaises(ValueError):
            lake_pins(b' ' * 16385)
        with self.assertRaises(ValueError):
            lake_pins(lock((self.pin, self.pin)))
        self.metadata['lake-manifest.json'] = lock((replace(self.pin, revision='2' * 40),))
        write(self.dep.path / 'lake-manifest.json', self.metadata['lake-manifest.json'])
        self.assertEqual(self.closure().pins, (self.pin,))
        self.metadata['lake-manifest.json'] = lock((replace(self.pin, name='missing'),))
        with self.assertRaises(ValueError):
            self.closure()

    def test_lake_quoted_names_are_normalized_without_accepting_unsafe_roots(self):
        quoted = lock((self.pin,)).replace(b'"dep"', '"«lean-pool»"'.encode())
        expected = replace(self.pin, name='lean-pool')
        self.assertEqual(lake_pins(quoted), {'lean-pool': expected})
        for name in ('«../dep»', '«dep/sub»', '«dep.name»', '««dep»»', '«dep'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                lake_pins(lock((self.pin,)).replace(b'"dep"', ('"' + name + '"').encode()))
        duplicate = lock((self.pin, self.pin)).replace(b'"dep"', '"«dep»"'.encode(), 1)
        with self.assertRaisesRegex(ValueError, 'Duplicate Lake dependency'):
            lake_pins(duplicate)

    def test_source_listing_ignores_unrelated_links_but_rejects_lean_links(self):
        tree = (b'100644 blob ' + b'1' * 40 + b'\tRoot.lean\0' +
                b'100644 blob ' + b'2' * 40 + b'\tDir/Child.lean\0' +
                b'120000 blob ' + b'3' * 40 + b'\tdocs/README.md\0')
        reader = GitReader(self.dep.path, self.context)
        with patch.object(GitReader, 'run', return_value=tree):
            self.assertEqual(set(reader.listing(self.pin.revision, suffixes=('.lean',))),
                             {'Root.lean', 'Dir/Child.lean'})
            with self.assertRaisesRegex(ValueError, 'Unsafe Git'):
                reader.listing(self.pin.revision)
        with patch.object(GitReader, 'run', return_value=tree.replace(b'docs/README.md', b'Alias.lean')):
            with self.assertRaisesRegex(ValueError, 'Unsafe Git'):
                reader.listing(self.pin.revision, suffixes=('.lean',))

    def test_closure_exact_mapping_and_development_toolchain_provenance(self):
        self.metadata['lean-toolchain'] = b'leanprover/lean4:v4.20.0'
        write(self.dep.path / 'lean-toolchain', self.metadata['lean-toolchain'])
        closure = self.closure()
        self.assertEqual(closure.pins, (self.pin,))
        self.assertEqual(len(closure.metadata), 3)
        with self.assertRaises(ValueError):
            self.closure(required=())
        with patch.object(GitReader, 'head', return_value='2' * 40), self.assertRaises(ValueError):
            resolve_closure(lock((self.pin,)), ('dep',), (self.dep,), self.context)

    def modules(self, *, standard=frozenset(), source_only=False):
        data = b'def value := 1\n'
        source = write(self.dep.path / 'Dep.lean', data)
        write(self.dep.path / 'lib/Dep.olean', b'compiled')
        oid = sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        closure = self.closure()
        with patch.object(GitReader, 'head', return_value=self.pin.revision), patch.object(GitReader, 'listing', return_value={'Dep.lean': (False, oid)}):
            return inventory_modules(closure, (self.dep,), self.context, required=('Dep',), standard=standard, source_only=source_only)

    def test_modules_pin_source_sidecars_and_reject_shadowing(self):
        write(self.dep.path / 'lib/Dep.ilean', b'interface')
        modules = self.modules()
        self.assertEqual(len(modules.modules[0].compiled), 2)
        with patch('json.dumps', side_effect=AssertionError('module JSON')):
            self.assertEqual(len(modules.id), 64)
        with self.assertRaises(ValueError):
            self.modules(standard=frozenset(('Dep',)))

    def test_modules_reject_untracked_sources_native_plugins_and_missing_required(self):
        write(self.dep.path / 'Extra.lean')
        with self.assertRaises(ValueError):
            self.modules()
        (self.dep.path / 'Extra.lean').unlink()
        write(self.dep.path / 'lib/native.so')
        with self.assertRaises(ValueError):
            self.modules(source_only=True)

    def test_module_admission_rechecks_metadata_and_mapping_paths(self):
        modules = self.modules()
        closure = self.closure()
        data = (self.dep.path / 'Dep.lean').read_bytes()
        oid = sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        def changed(*args, **kwargs):
            write(self.dep.path / 'lakefile.toml', b'changed during admission')
            return {'Dep.lean': (False, oid)}
        with patch.object(GitReader, 'head', return_value=self.pin.revision), patch.object(GitReader, 'listing', side_effect=changed):
            with self.assertRaisesRegex(EnvironmentError, 'metadata'):
                inventory_modules(closure, (self.dep,), self.context)
        with self.assertRaisesRegex(EnvironmentError, 'paths'):
            inventory_modules(closure, (replace(self.dep, module_paths=('.',)),), self.context)

    def test_large_raw_lake_configuration_does_not_use_json(self):
        self.metadata['lakefile.toml'] = b'# raw configuration\n' * 2000
        write(self.dep.path / 'lakefile.toml', self.metadata['lakefile.toml'])
        self.assertEqual(self.closure().pins, (self.pin,))
        with self.assertRaises(ValueError):
            lake_pins(canonical({'packagesDir': '.lake/packages', 'packages': [dict(name='dep', type='git')]}))

    def test_source_only_gate_covers_native_files_outside_module_paths(self):
        write(self.dep.path / 'assets/native.so')
        with self.assertRaisesRegex(EnvironmentError, 'native'):
            self.modules(source_only=True)


def benchmark_config(root):
    tools = tuple(Tool(name, write(root / name, name.encode(), executable=True), sha256(name.encode()).hexdigest()) for name in TOOLS)
    return VerificationConfig(root / 'benchmark', '1' * 40, 'sample', tools)


def benchmark_archive(config, *, extra=b''):
    prefix = 'generated/sample/'
    files = {p: b'' for p in ROOT_FILES}
    files['lake-manifest.json'] = lock()
    files.update({prefix + p: b'' for p in STARTER})
    files['manifests/problems/sample.toml'] = b'id="sample"\nstatus="active"\nmodule="Sample"\nholes=["goal"]'
    files[prefix + 'holes.json'] = canonical(dict(id='sample', module='Sample', holes=[dict(name='goal', kind='theorem')]))
    files[prefix + 'config.json'] = canonical(dict(theorem_names=['goal']))
    files[prefix + 'lakefile.toml'] = b'name="sample"'
    files[prefix + 'lean-toolchain'] = b'leanprover/lean4:v4.35.0-rc3'
    files[prefix + 'Challenge.lean'] = b'-- source\n' + extra
    return Archive(config.identity_bytes(), tuple(ArchiveFile(path, data) for path, data in sorted(files.items())))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = benchmark_config(self.root)

    def test_large_archive_raw_bytes_roundtrip_and_digest(self):
        archive = benchmark_archive(self.config, extra=b'x' * 2_000_000)
        original = __import__('json').dumps
        def bounded(value, **kwargs):
            # Only the small config and metadata can reach JSON.
            rendered = original(value, **kwargs)
            self.assertLessEqual(len(rendered), 16384)
            return rendered
        with patch('json.dumps', side_effect=bounded):
            payload = io.BytesIO()
            archive.save(payload)
            self.assertEqual(load_archive(payload.getvalue(), expected_id=archive.id), archive)
        self.assertLess(len(payload.getvalue()), 2_010_000)
        with self.assertRaises(ValueError):
            load_archive(payload.getvalue().replace(b'-- source', b'-- SOURCE'), expected_id=archive.id)

    def test_config_relocation_tool_pins_and_bounded_loading(self):
        import json
        value = dict(json.loads(self.config.identity_bytes()), benchmark=str(self.config.benchmark),
            tools={t.name: dict(path=str(t.path), sha256=t.sha256) for t in self.config.tools})
        self.assertEqual(load_config((json.dumps(value, indent=2) + '\n').encode()), self.config)
        archive = benchmark_archive(self.config)
        relocated = replace(self.config, benchmark=self.root / 'gone')
        self.assertEqual(match_relocation(relocated, archive).targets, (('Submission.goal', 'theorem'),))
        write(self.config.tools[0].path, b'changed', executable=True)
        with self.assertRaises(ValueError):
            match_relocation(relocated, archive)
        with patch('json.loads', side_effect=AssertionError('large JSON')), self.assertRaises(ValueError):
            load_config(b'x' * 16385)

    def test_archive_metadata_duplicates_archived_fixture_and_missing_files(self):
        archive = benchmark_archive(self.config)
        with self.assertRaises(ValueError):
            Archive(archive.config, archive.files + (archive.files[-1],))
        with self.assertRaises(ValueError):
            Archive(archive.config, archive.files[1:])
        changed = tuple(replace(f, data=f.data.replace(b'active', b'archived')) if f.path.startswith('manifests/') else f for f in archive.files)
        with self.assertRaisesRegex(ValueError, 'opt-in'):
            Archive(archive.config, changed)
        accepted = Archive(replace(self.config, allow_archived_fixture=True).identity_bytes(), changed)
        self.assertTrue(describe(accepted).fixture_only)

    def test_archival_checks_working_tree_and_head(self):
        archive = benchmark_archive(self.config)
        listing, blobs = {}, {}
        for index, file in enumerate(archive.files):
            write(self.config.benchmark / file.path, file.data)
            oid = f'{index:040x}'
            listing[file.path], blobs[oid] = (False, oid), file.data
        context = Context(self.root)
        with patch.object(GitReader, 'head', return_value=self.config.revision), patch.object(GitReader, 'listing', return_value=listing), patch.object(GitReader, 'run', side_effect=lambda *args: blobs[args[-1]]):
            saved = archive_benchmark(self.config, context)
            self.assertEqual(saved.id, archive.id)
            write(self.config.benchmark / 'generated/sample/Extra.lean')
            with self.assertRaises(ValueError):
                archive_benchmark(self.config, context)


class BoundaryTests(unittest.TestCase):
    def test_imports_only_lower_components_and_no_private_process_launches(self):
        import ast
        directory = Path(__file__).parents[1] / 'midas/environment'
        for path in directory.glob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 2:
                    self.assertIn(node.module.split('.')[0], ('domain', 'source', 'executor'), path.name)
                if isinstance(node, ast.Import):
                    self.assertFalse(any(name.name in ('subprocess', 'sqlite3', 'midas_formalizer') for name in node.names))


if __name__ == '__main__':
    unittest.main()
