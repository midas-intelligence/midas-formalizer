"""One command per operation; no legacy aliases or per-driver CLI branches."""
import argparse
import math
from pathlib import Path


class Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        seen = vars(namespace).setdefault('_seen', set())
        if self.dest in seen:
            parser.error(f'{option_string} may only be specified once')
        seen.add(self.dest)
        setattr(namespace, self.dest, values)


def integer(low, high):
    def parse(value):
        try:
            result = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError('Expected an integer') from error
        if not low <= result <= high:
            raise argparse.ArgumentTypeError(f'Expected {low} through {high}')
        return result
    return parse


def seconds(value):
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('Expected seconds') from error
    if not math.isfinite(result) or not 0 < result <= 60:
        raise argparse.ArgumentTypeError('Expected finite seconds greater than 0 and at most 60')
    return result


def option(parser, name, **options):
    parser.add_argument(name, action=Once, **options)


def selection(parser, *, saved=False):
    if saved:
        mode = parser.add_mutually_exclusive_group(required=True)
        option(mode, '--selection', type=Path, help='saved binary selection')
        option(mode, '--source', help='package-relative .lean path')
    else:
        option(parser, '--source', required=True, help='package-relative .lean path')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--target', action='append', help='exact Lean name; repeatable')
    mode.add_argument('--all-unfinished', action='store_true')


def runtime(parser, *, models=False):
    option(parser, '--cache', type=Path, default=Path.home()/'.cache/midas')
    if models:
        option(parser, '--codex', type=Path, help='app-server executable, pinned by hash when opened')
        parser.add_argument('--codex-env', action='append', default=[], metavar='NAME',
                            help='additional environment variable passed to app-server; repeatable')


def parser():
    result = argparse.ArgumentParser(prog='midas', description='Prepare Lean inputs, run proofs, and inspect committed results.',
                                     allow_abbrev=False)
    commands = result.add_subparsers(dest='command', required=True)
    descriptions = {
        'package': 'freeze a Lean package, or attach a library with --base',
        'select': 'inspect and validate targets; optionally save their frozen selection',
        'start': 'start a live, scripted, or supplied-candidate run',
        'resume': 'continue a saved run with authenticated recovery',
        'submit': 'submit a binary candidate to a saved run',
        'fork': 'create a run from an authenticated checkpoint',
        'status': 'inspect run progress or a bounded page of nodes',
        'stop': 'request cooperative cancellation from the current controller',
        'artifact': 'inspect an artifact reference or stream its verified bytes to a file',
        'export': 'verify and publish a proof bundle',
        'verify': 'independently audit a published bundle',
        'cache-probe': 'run the explicit four-call Responses cache diagnostic',
    }
    for name, help_text in descriptions.items():
        cmd = commands.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        cmd.add_argument('directory', type=Path, help='package, bundle, or run directory')
        cmd.add_argument('--json', action='store_true', help='emit bounded JSON records, one per line')
        if name not in ('status', 'stop', 'artifact'):
            runtime(cmd, models=name in ('start', 'resume', 'submit', 'fork'))
        if name in ('package', 'start', 'fork', 'verify', 'cache-probe'):
            option(cmd, '--config', type=Path, required=name in ('package', 'cache-probe'))
        if name in ('resume', 'submit', 'export'):
            option(cmd, '--raise-limits', type=Path, help='small prospective policy-increase JSON')
        if name in ('start', 'resume', 'submit', 'fork', 'export', 'verify'):
            option(cmd, '--package', type=Path, required=name in ('start', 'verify'))
        if name in ('start', 'resume', 'submit', 'export'):
            cmd.add_argument('--quiet', action='store_true', help='suppress progress on stderr')
        if name in ('start', 'resume', 'submit', 'select'):
            option(cmd, '--output', type=Path, help='save selection (select) or publish verified export (run commands)')
        if name in ('select', 'start'):
            selection(cmd, saved=name == 'start')
        if name in ('start', 'fork'):
            option(cmd, '--workflow', choices=('live', 'scripted', 'supplied'))
            option(cmd, '--script', type=Path, help='small JSON manifest of role/file pairs for raw replies')
        if name == 'start':
            option(cmd, '--candidates', type=Path, help='binary supplied-candidate submission')
            cmd.add_argument('--instructions', action='append', default=[], metavar='ROLE=FILE')
        elif name == 'submit':
            option(cmd, '--candidates', type=Path, required=True)
            option(cmd, '--owner', help='obligation ID, required when roots are ambiguous')
        elif name in ('fork', 'export'):
            cmd.add_argument('output', type=Path)
            if name == 'export':
                cmd.add_argument('--allow-sorry', action='store_true', help='explicitly permit an incomplete proof bundle')
        elif name == 'package':
            option(cmd, '--base', type=Path, help='existing package to extend with the library described by --config')
        elif name == 'status':
            cmd.add_argument('--nodes', action='store_true')
            option(cmd, '--owner', help='one obligation ID')
            cmd.add_argument('--roots', action='store_true', help='restrict nodes to selected roots')
            option(cmd, '--state', choices=('UNRESOLVED', 'CONDITIONAL', 'VERIFIED'))
            option(cmd, '--after', default='', help='exclusive obligation-ID cursor')
            option(cmd, '--limit', type=integer(1, 1000), default=100)
        elif name == 'stop':
            option(cmd, '--timeout', type=seconds, default=5)
        elif name == 'artifact':
            cmd.add_argument('id')
            option(cmd, '--output', type=Path)
            option(cmd, '--max-bytes', type=integer(0, 2**63-1), default=64*1024**2)
        elif name == 'verify':
            option(cmd, '--evidence-output', type=Path, help='save raw binary audit evidence')
    return result
