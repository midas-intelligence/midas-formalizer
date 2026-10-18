"""Thin command routing. The application owns every formalization workflow."""
from contextlib import nullcontext
import os
import sys

from ..domain import SelectionRequest, encode_selection
from ..status import record, terminal, write_json
from ..status.render import safe, write_details, write_nodes
from .arguments import parser
from .control import Signals, listen, stop
from .display import Display


def emit(stream, value, *, json=False):
    if json:
        write_json(stream, value)
    else:
        stream.write(' | '.join(f'{key}: {safe(item)}' for key, item in value.items() if key != 'type')+'\n')


def request(args):
    return SelectionRequest(args.source, tuple(args.target or ()), bool(args.all_unfinished or not args.target))


def selected(args):
    from ..application import files
    if args.selection is not None:
        if args.target or args.all_unfinished:
            raise ValueError('--selection already contains its target scope')
        return files.read(args.selection, 64*1024**2)
    if not args.target and not args.all_unfinished:
        raise ValueError('Starting from --source requires --target or --all-unfinished')
    return request(args)


def instructions(values):
    from ..application.files import read
    result = {}
    for value in values:
        role, separator, path = value.partition('=')
        if not separator or role not in ('prover', 'reviewer', 'researcher') or role in result:
            raise ValueError('Instructions require distinct prover/reviewer/researcher=FILE entries')
        result[role] = read(path, 16384)
    return tuple(result.items())


def execute(args, out, err, signals, application_factory):
    from ..application import files, inspection
    name = args.command
    if name == 'status':
        if args.nodes or args.owner or args.roots or args.state or args.after or 'limit' in args._seen:
            if args.owner and args.after:
                raise ValueError('--owner and --after are mutually exclusive')
            page = inspection.nodes(args.directory, after=args.after, limit=args.limit,
                                    owner=args.owner, state=args.state, roots=args.roots)
            if args.owner and not page.items:
                raise ValueError('No node matches the requested owner and filters')
            write_nodes(out, page, json=args.json)
            if page.details is not None:
                write_details(out, page.details, json=args.json)
        else:
            view = inspection.Inspector().inspect(args.directory)
            write_json(out, record(view)) if args.json else out.write(terminal(view))
        return 0
    if name == 'stop':
        accepted = stop(args.directory, timeout=args.timeout)
        emit(out, dict(type='stop', accepted=accepted), json=args.json)
        return 0
    if name == 'artifact':
        reference = inspection.artifact(args.directory, args.id)
        if args.output is not None:
            destination = args.output.absolute()
            if destination.resolve().is_relative_to(args.directory.resolve()):
                raise ValueError('Artifact output must be outside the run directory')
            files.write(destination, inspection.artifact_chunks(args.directory, args.id, max_bytes=args.max_bytes))
        emit(out, dict(type='artifact', **reference.to_dict()), json=args.json)
        return 0

    codex, codex_env = getattr(args, 'codex', None), getattr(args, 'codex_env', ())
    app = application_factory(args.cache, codex=(codex, tuple(codex_env)) if codex else None)
    if codex_env and codex is None:
        raise ValueError('--codex-env requires --codex')
    if name == 'package':
        environment = files.package(app, args.directory, args.config, base=args.base, cancellation=signals.cancelled)
        emit(out, dict(type='package', environment_id=environment.id), json=args.json)
        return 0
    if name == 'select':
        selection = app.select(args.directory, request(args), cancellation=signals.cancelled)
        if args.output is not None:
            files.write(args.output, (encode_selection(selection),))
        emit(out, dict(type='selection', selection_id=selection.id, environment_id=selection.environment_id,
                       targets=len(selection.identities)), json=args.json)
        for target, identity in zip(selection.selected.targets, selection.identities):
            # Names can be large structural metadata. Human previews are explicitly bounded.
            emit(out, dict(type='target', name=target.selector[:1024], name_truncated=len(target.selector)>1024,
                           id=identity.id, kind=target.kind), json=args.json)
        return 0
    if name == 'verify':
        result = app.verify(args.directory, package=args.package, config=files.config(args.config),
                            cancellation=signals.cancelled)
        if args.evidence_output is not None and result.evidence is not None:
            files.write(args.evidence_output, (result.evidence,))
        emit(out, dict(type='verification', verified=result.verified, reason=result.reason), json=args.json)
        return 130 if signals.cancelled.is_set() else result.exit_code
    if name == 'cache-probe':
        result = app.cache_probe(args.directory, config=files.config(args.config), cancellation=signals.cancelled)
        emit(out, dict(type='cache-probe', status=result['status'], observation=result['observation']), json=args.json)
        for failure, cached, written, execution in result['steps']:
            emit(out, dict(type='cache-step', failure=failure, cache_tokens=cached,
                           cache_write_tokens=written, execution_id=execution), json=args.json)
        return 130 if signals.cancelled.is_set() else 0 if result['status'] == 'complete' else 1

    if name in ('start', 'fork'):
        script = files.script(args.script) if args.script else None
        mode = args.workflow
        if script is not None:
            if mode not in (None, 'scripted'):
                raise ValueError('--script requires the scripted workflow')
            mode = 'scripted'
        if mode == 'scripted' and script is None and name == 'start':
            raise ValueError('Scripted start requires --script')
    if name == 'start':
        payload = files.read(args.candidates, 8*1024**2) if args.candidates else None
        if payload is not None:
            from ..domain import load_submission
            load_submission(payload)
            if mode not in (None, 'supplied'):
                raise ValueError('--candidates requires the supplied workflow')
            mode = 'supplied'
        run = app.start(args.directory, package=args.package,
            selection=selected(args), config=files.config(args.config),
            workflow=mode or 'live', script=script or (), instructions=instructions(args.instructions),
            cancellation=signals.cancelled)
    elif name == 'fork':
        with app.fork(args.directory, args.output, config=files.config(args.config) if args.config else None,
                      package=args.package, workflow=mode, script=script) as run:
            emit(out, dict(type='fork', run_id=run.writer.run_id, directory=str(args.output)), json=args.json)
        return 0
    else:
        payload = files.read(args.candidates, 8*1024**2) if name == 'submit' else None
        if payload is not None:
            from ..domain import load_submission
            load_submission(payload)
        run = app.resume(args.directory, package=args.package,
            increases=files.document(args.raise_limits) if args.raise_limits else None,
            export_only=name == 'export', allow_sorry=getattr(args, 'allow_sorry', False),
            cancellation=signals.cancelled)
    with run, listen(run, signals):
        if payload is not None:
            run.submit(payload, owner=getattr(args, 'owner', None))
        with nullcontext(None) if args.quiet else Display(err) as display:
            result = run.run(observe=display.publish if display is not None else None)
        if result.exit_code == 0 and args.output is not None:
            run.materialize(args.output)
        view = app.inspect(args.directory)
        write_json(out, record(view)) if args.json else out.write(terminal(view))
        return result.exit_code


def main(argv=None, *, stdout=None, stderr=None, application_factory=None):
    out, err = stdout or sys.stdout, stderr or sys.stderr
    args = parser().parse_args(argv)
    if not hasattr(args, '_seen'):
        args._seen = set()
    if application_factory is None:
        def application_factory(*values, **options):
            from ..application import Application
            return Application(*values, **options)
    with Signals() as signals:
        try:
            result = execute(args, out, err, signals, application_factory)
            out.flush()
            return result
        except BrokenPipeError:
            if out is sys.stdout:
                fd = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(fd, out.fileno())
                finally:
                    os.close(fd)
            return 0
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
            err.write('midas: '+safe(str(error)[:2048])+'\n')
            return 130 if signals.cancelled.is_set() else 2
