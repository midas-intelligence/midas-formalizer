"""Alternating v2/new medians for export algorithms; no compiler processes."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer import finalization as old_finalization
from midas_formalizer import source_proofs as old_proofs
from midas_formalizer.source_proof_admission import Admission
from midas_formalizer.persisted import canonical
from midas.domain import encoding as codec
from midas.export import proofs
from tests.benchmark_source import measure
from tests.test_export import Harness


def main():
    with TemporaryDirectory() as temporary:
        h = Harness(Path(temporary))
        root = h.graph.nodes[0].proof
        cert = proofs.load(h.artifacts, root.evidence)
        reader = SimpleNamespace(read=lambda key, **kwargs: h.artifacts.data[key],
            info=lambda key: SimpleNamespace(id=key, sha256=sha256(h.artifacts.data[key]).hexdigest(), length=len(h.artifacts.data[key])))
        for label, padding, number in (('integration_small', b'', 100),
                                        ('integration_2MiB', b'-- padding\n' * 200000, 10)):
            selected = replace(h.selection, source=h.selection.source + padding)
            legacy = SimpleNamespace(selection=SimpleNamespace(selected=selected.selected,
                payload=canonical({'source': selected.source.hex(), 'declarations': []})))
            old = lambda: old_finalization.integrate(reader, legacy, [{'certificate': root.evidence.to_dict()}])
            new = lambda: proofs.render(selected, (root,), h.artifacts)
            before, after, ratio = measure(old, new, number)
            print(f'{label}: v2={before:.2f} us new={after:.2f} us ratio={ratio:.3f}', flush=True)
        parent_request = h.conditional()
        parent = proofs.load(h.artifacts, h.graph.nodes[0].selected.evidence, closed=False)
        child = proofs.load(h.artifacts, h.graph.nodes[1].selected.evidence)
        packet = parent_request.decode()['packet']
        deps = packet['dependencies']
        spec = {'dependencies': [dict(deps[0], certificate='child')]}
        old = lambda: old_proofs.compose_polymorphic(parent, packet, spec, lambda _: child, 'a'*64, Admission())
        new = lambda: proofs.compose(parent, (child,), deps)
        before, after, ratio = measure(old, new, 100)
        print(f'polymorphic_assembly: v2={before:.2f} us new={after:.2f} us ratio={ratio:.3f}', flush=True)


if __name__ == '__main__':
    main()
