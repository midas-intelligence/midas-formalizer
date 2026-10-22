"""Status contracts over actual committed storage, without Lean execution."""
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from midas.application import inspection
from midas.domain import OwnerVersion, ProofNode, Usage
from midas.policy import Stage
from midas.status import record, terminal
from midas.status.render import write_nodes
from midas.storage import Changes, NodeChange
from tests import test_application as fixtures


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Contracts()
        self.fixture.setUp()
        self.root = self.fixture.root

    def tearDown(self):
        self.fixture.tearDown()

    def test_inspection_is_read_only_and_matches_completed_run(self):
        with self.fixture.start() as run:
            run.submit(fixtures.submission(self.fixture.selected))
            final = run.run()
            reader = inspection.Inspector()
            with patch.object(self.fixture.app, 'environment', side_effect=AssertionError('environment access')):
                view = reader.inspect(self.root/'run')
                self.assertEqual(view.completion, final)
                self.assertIs(view, reader.inspect(self.root/'run'))
                self.assertEqual(view.progress.export_verification, Stage.VERIFIED)
                self.assertNotIn('package', record(view))
                self.assertNotIn('toolchain', record(view))
                text = terminal(view)
                self.assertIn('completed: 1 | conditional: 0 | active: 0 | waiting: 0', text)
                self.assertNotIn('integration:', text)
                self.assertNotIn('verification:', text)

    def test_summary_reads_do_not_decode_proofs_and_pages_are_filtered(self):
        with self.fixture.start() as run:
            source = run.writer.node(self.fixture.selected.identities[0].id).node.interface
            nodes = tuple(NodeChange(None, ProofNode(OwnerVersion('obl_'+f'{i:064x}', 0),
                self.fixture.selected.environment_id, source)) for i in range(5))
            with run.writer.transaction(run.writer.session) as tx:
                tx.apply('nodes', Changes(nodes=nodes, graph_revision=run.writer.status_snapshot().graph_revision))
            with patch('midas.storage.codec.unnode', side_effect=AssertionError('proof decode')):
                inspector = inspection.Inspector()
                view = inspector.inspect(self.root/'run')
                self.assertEqual(sum(dict(view.status.nodes).values()), 6)
                self.assertEqual(record(view)['nodes'], {'UNRESOLVED': 1})
                self.assertEqual(record(view)['discarded_nodes'], 5)
                self.assertIn('waiting: 1 | discarded: 5', terminal(view))
                page = inspection.nodes(self.root/'run', limit=2, state='UNRESOLVED')
                self.assertEqual(len(page.items), 2)
                second = inspection.nodes(self.root/'run', limit=2, after=page.after)
                self.assertTrue(set(n.owner for n in page.items).isdisjoint(n.owner for n in second.items))
                roots = inspection.nodes(self.root/'run', roots=True)
                self.assertEqual(len(roots.items), 1)
                self.assertIsNone(roots.after)
                with self.assertRaises(ValueError):
                    inspection.nodes(self.root/'run', limit=1001)

    def test_unknown_usage_and_terminal_controls(self):
        with self.fixture.start():
            view = inspection.Inspector().inspect(self.root/'run')
        accounting = replace(view.status.accounting, known=Usage(10, 0, 4, 1500000), unknown=Usage(1, 0, 0, 1))
        view = replace(view, node_counts=None, status=replace(view.status, accounting=accounting,
                       nodes=(('VERIFIED', 2), ('CONDITIONAL', 3), ('UNRESOLVED', 5)), active_nodes=2),
                       completion=replace(view.completion, reasons=('active_workers', 'bad\x1b[2J\ntext\u202e')))
        value = record(view)
        self.assertIsNone(value['usage']['input_tokens'])
        self.assertIsNone(value['usage']['cost_microusd'])
        self.assertEqual(value['known_usage']['input_tokens'], 10)
        text = terminal(view)
        self.assertIn('$1.500000 + unknown', text)
        self.assertIn('completed: 2 | conditional: 3 | active: 2 | waiting: 3', text)
        self.assertEqual(len(text.splitlines()), 2)
        self.assertNotIn('active_workers', text)
        self.assertIn('reasons: bad', text)
        self.assertNotIn('\x1b', text)
        self.assertNotIn('\u202e', text)

    def test_pending_submission_nodes_stay_current_and_failed_leftovers_are_discarded(self):
        from midas.application import records as r
        from midas.application.service import commit
        with self.fixture.start() as run:
            reader = run.writer
            root = self.fixture.selected.identities[0].id
            _, state = r.owner_state(reader, root)
            interface = reader.node(root).node.interface
            owners = tuple('obl_'+f'{i:064x}' for i in range(4))
            with reader.transaction(reader.session) as tx:
                tx.apply('helpers', Changes(nodes=tuple(NodeChange(None, ProofNode(OwnerVersion(owner, 0),
                    self.fixture.selected.environment_id, interface)) for owner in owners),
                    graph_revision=reader.status_snapshot().graph_revision))
            commit(reader, 'submissions', r.merge(
                r.save_owner(reader, root, replace(state, submission='current')),
                r.save_owner(reader, owners[0], replace(state, submission='nested')),
                r.record(reader, 'application.submissions', 'current',
                         dict(phase='binding', rows={str(i): dict(owner=owner) for i, owner in enumerate(owners[:2])})),
                r.record(reader, 'application.submissions', 'nested',
                         dict(phase='proof', rows={'leaf': dict(owner=owners[2])})),
                r.record(reader, 'application.submissions', 'failed',
                         dict(phase='failed', rows={str(i): dict(owner=owner) for i, owner in enumerate(owners)}))))
            inspector = inspection.Inspector()
            with patch('midas.storage.codec.unnode', side_effect=AssertionError('proof decode')):
                view = inspector.inspect(self.root/'run')
                self.assertEqual(record(view)['nodes'], {'UNRESOLVED': 4})
                self.assertEqual(record(view)['discarded_nodes'], 1)
                self.assertIn('waiting: 4 | discarded: 1', terminal(view))
                # Reusing a helper from a failed submission keeps it current; abandoning
                # the current submission removes its entire unattached subtree.
                commit(reader, 'abandon', r.save_owner(reader, root, state))
                view = inspector.inspect(self.root/'run')
                self.assertEqual(record(view)['nodes'], {'UNRESOLVED': 1})
                self.assertEqual(record(view)['discarded_nodes'], 4)

    def test_worst_case_reasons_stay_within_json_metadata_limit(self):
        from midas.status import write_json
        with self.fixture.start():
            view = inspection.Inspector().inspect(self.root/'run')
        view = replace(view, completion=replace(view.completion, reasons=('\x1b'*256,)*20))
        out = io.StringIO()
        write_json(out, record(view))
        self.assertLess(len(out.getvalue().encode()), 16384)
        self.assertTrue(json.loads(out.getvalue())['reasons_truncated'])

    def test_owner_details_keep_source_and_evidence_behind_references(self):
        with self.fixture.start() as run:
            owner = self.fixture.selected.identities[0].id
            page = inspection.nodes(self.root/'run', owner=owner)
            self.assertEqual(page.details.id, owner)
            self.assertEqual({label for label, _ in page.details.artifacts}, {'context', 'identity'})
            self.assertIsNone(page.details.reason)

    def test_json_node_output_serializes_rows_separately(self):
        with self.fixture.start():
            page = inspection.nodes(self.root/'run')
        # A full page may be large. Individual JSON calls must remain bounded.
        page = replace(page, items=page.items*1000)
        output = io.StringIO()
        original = json.dumps
        def bounded(value, **options):
            self.assertNotIsInstance(value, list)
            return original(value, **options)
        with patch('json.dumps', side_effect=bounded):
            write_nodes(output, page, json=True)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1001)
        self.assertTrue(all(len(line.encode()) < 16384 for line in lines))

    def test_revision_invalidates_cache_and_stale_saved_completion(self):
        with self.fixture.start() as run:
            run.run()
            reader = inspection.Inspector()
            before = reader.inspect(self.root/'run')
            from midas.application import records as r
            from midas.application.service import commit
            commit(run.writer, 'change', r.record(run.writer, r.SCOPE, 'completion',
                [0, 'verified', 'verified', 'verified', 'export_verified', [], 0]))
            after = reader.inspect(self.root/'run')
            self.assertIsNot(before, after)
            self.assertEqual(after.completion.proof_state, 'incomplete')

    def test_read_only_imports_do_not_load_execution_services(self):
        code = "from midas.application.inspection import Inspector; import sys; assert not any('midas.'+s in sys.modules for s in ('provider','checker','executor','controller'))"
        subprocess.run([sys.executable, '-B', '-c', code], check=True, cwd=Path(__file__).resolve().parents[1])


if __name__ == '__main__':
    unittest.main()
