"""Pass ordinary commands to midas.cli; add only benchmark-specific commands."""
import argparse
import sys

from ..application import Application, files
from ..cli import main as midas_main
from ..cli.arguments import option, runtime
from ..cli.control import Signals
from ..cli.service import emit
from ..environment import load_config
from ..environment import safe_read
from ..domain import encoding as codec
from ..status.render import safe
from . import workspace, service


def parser():
    from pathlib import Path
    result = argparse.ArgumentParser(prog='midas.leaneval', allow_abbrev=False,
        description='LeanEval wrapper. All ordinary midas commands are also available; start accepts --workspace.')
    commands = result.add_subparsers(dest='command', required=True)
    for name, help_text in (('prepare', 'bind a trusted benchmark to an existing package'),
                            ('finalize', 'compare an ordinary export and publish judge files'),
                            ('verify-submission', 'independently recheck a published LeanEval submission')):
        cmd = commands.add_parser(name, help=help_text, allow_abbrev=False)
        cmd.add_argument('directory', type=Path)
        runtime(cmd)
        option(cmd, '--package', type=Path, required=name == 'prepare')
        option(cmd, '--verification-config', type=Path, required=True)
        option(cmd, '--config', type=Path)
        cmd.add_argument('--json', action='store_true')
        if name == 'prepare':
            cmd.add_argument('--replay-dependencies', action='store_true',
                help='rebuild dependency sources; by default trust frozen compiled dependency files')
        if name != 'prepare':
            option(cmd, '--workspace', type=Path, required=True)
            option(cmd, '--runtime', type=Path, required=True, help='explicit regular-file /usr runtime tree')
        if name == 'finalize':
            cmd.add_argument('output', type=Path)
        if name == 'verify-submission':
            option(cmd, '--evidence-output', type=Path)
    return result


def start(argv, *, application_factory, stdout, stderr):
    from pathlib import Path
    extra = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    option(extra, '--workspace', type=Path, required=True)
    options, rest = extra.parse_known_args(argv)
    if any(arg.split('=', 1)[0] in ('--package', '--selection', '--source', '--target', '--all-unfinished') for arg in rest):
        raise ValueError('--workspace already supplies the package and complete target selection')
    directory = options.workspace.absolute()
    metadata = codec.decode(safe_read(directory/'workspace.bin', 16384))
    # A small adapter supplies the already prepared library to the existing start workflow.
    def factory(*args, **kwargs):
        app = application_factory(*args, **kwargs)
        bound = workspace.load(app, directory)
        class BoundApplication:
            def __getattr__(self, name):
                return getattr(app, name)

            def start(self, *values, **settings):
                settings.update(package=bound.package, selection=bound.selection,
                                library=bound.library, library_reader=bound.library_reader)
                return app.start(*values, **settings)
        return BoundApplication()
    return midas_main(['start', *rest, '--package', metadata['package'], '--selection', str(directory/'selection.bin')],
        application_factory=factory, stdout=stdout, stderr=stderr)


def main(argv=None, *, stdout=None, stderr=None, application_factory=Application):
    argv = list(sys.argv[1:] if argv is None else argv)
    out, err = stdout or sys.stdout, stderr or sys.stderr
    if argv and argv[0] == 'start' and any(a in ('-h', '--help') for a in argv):
        out.write('LeanEval: --workspace DIR supplies the prepared package, selection, and source library.\n')
        return midas_main(['start', '--help'], stdout=out, stderr=err, application_factory=application_factory)
    if argv and argv[0] == 'start' and any(a.split('=', 1)[0] == '--workspace' for a in argv):
        try:
            return start(argv[1:], application_factory=application_factory, stdout=out, stderr=err)
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
            err.write('midas.leaneval: '+safe(str(error)[:2048])+'\n')
            return 2
    if argv and argv[0] not in ('prepare', 'finalize', 'verify-submission', '-h', '--help'):
        return midas_main(argv, stdout=out, stderr=err, application_factory=application_factory)
    args = parser().parse_args(argv)
    with Signals() as signals:
        try:
            app = application_factory(args.cache)
            config = files.config(args.config)
            verification = load_config(files.read(args.verification_config, 16384), base=args.verification_config.absolute().parent)
            if args.command == 'prepare':
                bound = workspace.prepare(app, args.directory, package=args.package, verification=verification,
                    config=config, cancellation=signals.cancelled, replay_dependencies=args.replay_dependencies)
                result = dict(type='leaneval-workspace', environment_id=bound.environment_id,
                              archive_id=bound.archive.id, selection_id=bound.selection.id)
            else:
                options = dict(workspace=args.workspace, verification=verification, runtime=args.runtime,
                               package=args.package, config=config, cancellation=signals.cancelled)
                if args.command == 'finalize':
                    result = service.finalize(app, args.directory, args.output, **options)
                else:
                    result, evidence = service.verify_submission(app, args.directory, **options)
                    if args.evidence_output is not None:
                        files.write(args.evidence_output, (evidence,))
            emit(out, result, json=args.json)
            out.flush()
            return 0
        except BrokenPipeError:
            return 0
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
            err.write('midas.leaneval: '+safe(str(error)[:2048])+'\n')
            return 130 if signals.cancelled.is_set() else 2
