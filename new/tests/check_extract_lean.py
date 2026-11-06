"""Opt-in extractor regressions. Root AGENTS.md requires Lean permission.

Run: python3 -B -m tests.check_extract_lean /absolute/path/to/toolchain/bin/lean
"""

from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

from midas.checker.runtime import sources
from midas.domain import encoding as codec


def main():
    compiler = str(Path(sys.argv[1]).resolve(strict=True))
    with TemporaryDirectory(prefix='midas-extract-lean-') as temporary:
        root = Path(temporary)
        (root / 'Extract.lean').write_bytes(sources()['Extract.lean'])
        command = ['bwrap', '--die-with-parent', '--unshare-all', '--ro-bind', '/', '/',
                   '--dev', '/dev', '--proc', '/proc', '--bind', temporary, temporary,
                   '--chdir', temporary, '--setenv', 'LEAN_PATH', temporary,
                   '--setenv', 'LEAN_SYSROOT', str(Path(compiler).parent.parent), compiler]

        def run(arguments):
            return subprocess.run([*command, *arguments], capture_output=True, text=True, timeout=60)

        def extract(label, source, expected=()):
            path = root / (label + '.lean')
            path.write_text(source)
            evidence = root / (label + '.bin')
            result = run(['--run', 'Extract.lean', str(path), path.name, str(evidence), '200000', '1024'])
            diagnostic = result.stderr + result.stdout
            if expected:
                assert result.returncode != 0, label + ' unexpectedly succeeded'
                assert not evidence.exists(), label + ' emitted an inventory for invalid input'
                for text in expected:
                    assert text in diagnostic, (label, text, diagnostic)
                print(label + ': rejected with diagnostic', flush=True)
                return diagnostic
            assert result.returncode == 0, diagnostic
            print(label + ': inspected', flush=True)
            return codec.decode(evidence.read_bytes())

        # Keep the failing theorem on the original incident's line. Compare the
        # diagnostic with plain Lean, including its column and parser message.
        broken = 'theorem schreier_abelianization : ∃ (λZ : Nat), λZ = 0 := by sorry\n'
        wrapped = 'import Lean\n' + '\n' * 211 + 'set_option maxHeartbeats 0 in\n' + broken
        diagnostic = extract('ReservedWrapped', wrapped,
            ('input contains Lean parse errors', 'ReservedWrapped.lean:214:', "unexpected token 'λ'"))
        plain = run(['ReservedWrapped.lean'])
        assert plain.returncode != 0
        parser_message = re.search(r"ReservedWrapped\.lean:214:\d+: error: [^\n]+", plain.stdout + plain.stderr)
        assert parser_message is not None, plain.stdout + plain.stderr
        assert parser_message[0] in diagnostic, (parser_message[0], diagnostic)
        extract('ReservedPlain', 'import Lean\n' + '\n' * 212 + broken,
            ('input contains Lean parse errors', 'ReservedPlain.lean:214:', "unexpected token 'λ'"))
        extract('Malformed', 'import Lean\ndef malformed : Nat := )\n',
            ('input contains Lean parse errors', 'Malformed.lean:2:', "unexpected token ')'"))
        extract('Elaboration', 'import Lean\ndef invalid : Nat := True\n',
            ('input contains Lean elaboration errors', 'Elaboration.lean:2:', 'Type mismatch'))

        # Earlier warnings must not consume this command's messages: Lean resets
        # its log before each command, rather than accumulating across commands.
        diagnostic = extract('Incomplete', 'import Lean\ntheorem earlier : True := by sorry\naxiom incomplete : sorry\n',
            ('Incomplete.lean:3:6: incomplete: type contains sorry', 'type: sorry',
             'command messages:\n', 'Incomplete.lean:3:'))
        assert 'warning: declaration uses `sorry`' in diagnostic.split('command messages:\n', 1)[1], diagnostic
        assert 'Incomplete.lean:2:' not in diagnostic, diagnostic

        # Simulate a command that recovers an elaboration error while retaining
        # only a warning. The value audit must still reject its synthetic sorry.
        recovered = '''import Lean
open Lean Elab Command
elab "recover_theorem" : command => do
  elabCommand (← `(theorem recovered : True := by exact (0 : Nat)))
  modify fun s => { s with messages := s.messages.errorsToWarnings }
recover_theorem
'''
        extract('Recovered', recovered,
            ('Recovered.lean:6:0: recovered', ': value contains synthetic sorry (recovered elaboration error)',
             'type: True', 'command messages:\n', 'Type mismatch'))
        extract('Anonymous', '''import Lean
macro "generated_hole" : command => `(example : True := by sorry)
theorem earlier : True := by sorry
generated_hole
''', ('unsupported anonymous or generated hole-containing command at byte',))

        # Parsing must use the environment/scopes produced by earlier commands;
        # valid binders and explicit proof holes must retain their inventory.
        valid = '''import Lean
namespace Scope
scoped notation "ℕ₀" => Nat
open scoped Scope
theorem hole : ∃ (mapZ : ℕ₀ → ℕ₀), mapZ 0 = 0 := by sorry
set_option maxHeartbeats 0 in
theorem wrapped : True := by trivial
theorem quoted : ∃ («λZ» : Nat), «λZ» = 0 := ⟨0, rfl⟩
end Scope
'''
        inventory = extract('Valid', valid)
        assert inventory['format'] == 'midas-source-inventory-v1'
        rows = {row['selector']: row for row in inventory['declarations']}
        hole = rows['Scope.hole']
        assert hole['unfinished'] and hole['source_authored'] and hole['supported'], hole
        start, end = hole['replacement']
        assert b'by sorry' in valid.encode()[start:end]
        assert not rows['Scope.wrapped']['unfinished']
        assert not rows['Scope.quoted']['unfinished']
        print('extractor parser, recovery diagnostics, and valid inventory checks passed', flush=True)


if __name__ == '__main__':
    main()
