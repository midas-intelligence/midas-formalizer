"""Opt-in replay/export regression against a prepared pinned Mathlib checkout.

python -B -m tests.check_mathlib_options_lean /toolchain/bin/lean /benchmark/checkout
Requires the Mathlib.Order.Hom.Bounded cache closure and AGENTS.md permission.
"""
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory

from midas.application.preparation import operate
from midas.checker.rebuild import rebuild
from midas.domain import ResourceLimits
from midas.environment import (Dependency, Environment, PackageConfig, configure_modules,
    inventory_modules, lake_pins, replay_dependencies, resolve_closure, safe_read)
from midas.environment.package import output
from midas.export import prepare_library


def main():
    toolchain = Path(sys.argv[1]).resolve(strict=True).parent.parent
    checkout = Path(sys.argv[2]).resolve(strict=True)
    lock = safe_read(checkout/'lake-manifest.json', 16384)
    names = {'mathlib'}
    pending = ['mathlib']
    while pending:
        name = pending.pop()
        child_lock = safe_read(checkout/'.lake/packages'/name/'lake-manifest.json', 16384)
        for child in lake_pins(child_lock):
            if child not in names:
                names.add(child)
                pending.append(child)
    dependencies = tuple(Dependency(name, checkout/'.lake/packages'/name, ('.lake/build/lib/lean',),
        ('.', 'scripts') if name == 'batteries' else ('.',)) for name in sorted(names))
    # The narrow cache has no objects for packages outside the required import closure.
    for dependency in dependencies:
        (dependency.path/'.lake/build/lib/lean').mkdir(parents=True, exist_ok=True)
    target = 'Mathlib.Order.Hom.Bounded'
    limits = ResourceLimits(1800000, 4096, 32*1024**2, 64*1024**2)
    with TemporaryDirectory(prefix='midas-mathlib-options-') as temporary:
        root = Path(temporary)
        project = root/'project'
        project.mkdir()
        (project/'Main.lean').write_text('import '+target+'\nexample : True := by trivial\n')

        def check(context):
            started = time.monotonic()
            env = Environment.prepare(root/'package', PackageConfig('4.35.0-rc3', toolchain,
                (('project', project), *((d.name, d.path) for d in dependencies)),
                ('project/Main.lean',), ('project', *(d.name+'/.lake/build/lib/lean' for d in dependencies),
                                       'toolchain/lib/lean')), context)
            closure = resolve_closure(lock, ('mathlib',), dependencies, context)
            modules = inventory_modules(closure, dependencies, context, required=(target,),
                standard=frozenset(n for n, p in env.manifest.modules.items() if p.startswith('toolchain/')),
                source_only=True)
            capture_started = time.monotonic()
            configured = configure_modules(env, closure, modules, context)
            captured = time.monotonic()-capture_started
            item = next(m for m in configured.modules if m.name == target)
            assert dict(item.lean_options)['maxSynthPendingDepth'] == 3
            assert configured.id != modules.id
            print(f'captured {len(configured.modules)} modules in {captured:.2f}s; Bounded maxSynthPendingDepth=3', flush=True)
            replay = replay_dependencies(env, configured, dependencies, context)
            print(f'replayed {len(replay.build_order)} modules with exact artifact hashes ({time.monotonic()-started:.2f}s total)', flush=True)
            library = prepare_library(env, configured, replay, dependencies, context.staging)
            rows = library.decode()
            assert next(r for r in rows if r.name == target).options == item.lean_options
            inventory_ref = context.staging.write_bounded(library.payload, max_bytes=64*1024**2)
            from types import SimpleNamespace
            # This preparation fixture owns the captured artifacts directly.
            build_context = SimpleNamespace(workspace=context.workspace, reader=context.staging,
                check=context.check, run_process=context.run_process, limits=context.limits)
            env.validate(hash_toolchain=True, fresh=True, check=context.check)
            workspace, result = rebuild(env, {'inventory': inventory_ref.to_dict(),
                'logical': 'Audit.lean', 'source': ''}, build_context)
            output(result)
            env.validate(hash_toolchain=True, fresh=True, check=context.check)
            print(f'independent export rebuild matched {len(rows)} modules ({time.monotonic()-started:.2f}s total)', flush=True)
        operate(check, limits=limits)


if __name__ == '__main__':
    main()
