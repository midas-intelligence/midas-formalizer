"""Native cache binding, corruption recovery and opaque input transport."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from midas.checker import native
from midas.domain import encoding as codec


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.root = ['str', ['anonymous'], 'MidasCandidateResult']
        self.packet = {'source': 'import Lean\n', 'logical': 'Main.lean', 'offset': 12}
        self.cert = {'declarations': [{'name': self.root, 'signature': {'target': 'True'},
                                      'source': 'by trivial', 'requires': []}]}

    def spec(self, packet=None, cert=None, env='env', runtime='runtime'):
        return native.specification(packet or self.packet, cert or self.cert, env, runtime)

    def test_keys_share_renamed_roots_but_bind_source_context_and_runtime(self):
        original = self.spec()['nodes'][0]['id']
        renamed = deepcopy(self.cert)
        renamed['declarations'][0]['name'] = ['str', self.root, 'assembled']
        self.assertEqual(self.spec(cert=renamed)['nodes'][0]['id'], original)
        variants = [self.spec(env='other'), self.spec(runtime='other'),
                    self.spec(packet=dict(self.packet, offset=13)),
                    self.spec(packet=dict(self.packet, source='import Other\n')),
                    self.spec(cert=dict(self.cert, assumptions=['conditional']))]
        changed = deepcopy(self.cert)
        changed['declarations'][0]['source'] = 'by exact True.intro'
        variants.append(self.spec(cert=changed))
        self.assertTrue(all(s['nodes'][0]['id'] != original for s in variants))
        self.assertNotEqual(self.spec()['root'], self.root)

    def test_dependency_change_invalidates_dependent_declaration(self):
        cert = deepcopy(self.cert)
        child = ['str', self.root, 'child']
        cert['declarations'][0]['name'] = child
        cert['declarations'].append({'name': self.root, 'signature': {'target': 'True'},
                                    'source': 'child', 'requires': [child]})
        before = self.spec(cert=cert)
        cert['declarations'][0]['source'] = 'by exact True.intro'
        after = self.spec(cert=cert)
        self.assertTrue(all(a['id'] != b['id'] for a, b in zip(before['nodes'], after['nodes'])))

    def test_cache_survives_new_instance_corruption_and_missing_files(self):
        spec = self.spec()
        ident = spec['nodes'][0]['id']
        with TemporaryDirectory() as temp:
            cache = native.DeclarationCache(Path(temp)/'declarations')
            self.assertEqual(cache.load(spec), ())
            cache.store((('producer', memoryview(b'opaque olean')),), spec)
            again = native.DeclarationCache(cache.directory)
            self.assertEqual(again.load(spec), ((ident, b'opaque olean'),))
            path = cache.directory/ident
            path.write_bytes(sha256(b'opaque olean').digest()+b'changed')
            self.assertEqual(again.load(spec), ())
            path.unlink()
            path.symlink_to(Path(temp)/'outside')
            (Path(temp)/'outside').write_bytes(b'data')
            self.assertEqual(again.load(spec), ())
            with self.assertRaises(ValueError):
                cache.store((('0'*64, b'wrong'),), spec)

    def test_frame_does_not_encode_native_bytes_or_use_json(self):
        blob = bytes(range(256))*4096
        ident = sha256(blob).hexdigest()
        encode = codec.encode
        encoded = []
        def recording(value, **kw):
            encoded.append(value)
            return encode(value, **kw)
        with patch.object(codec, 'encode', recording), patch('json.dumps', side_effect=AssertionError):
            data = native.frame(self.packet, ((ident, memoryview(blob)),))
        start = len(native.MAGIC)+8
        size = int.from_bytes(data[len(native.MAGIC):start], 'little')
        self.assertEqual(codec.decode(data[start:start+size]),
                         {'packet': self.packet, 'artifacts': [{'id': ident, 'bytes': len(blob)}]})
        self.assertEqual(data[start+size:], blob)
        self.assertEqual(len(encoded), 1)
        with self.assertRaises(ValueError):
            native.frame(self.packet, ((ident, blob), (ident, blob)))
        with self.assertRaises(ValueError):
            native.frame(self.packet, (('../bad', blob),))
        with patch.object(native, 'MAX_BYTES', 10), self.assertRaises(ValueError):
            native.frame(self.packet, ((ident, blob),))

    def test_cache_budget_skips_oversized_entries(self):
        with TemporaryDirectory() as temp:
            cache = native.DeclarationCache(temp)
            spec = self.spec()
            cache.store((('producer', b'0123456789'),), spec)
            with patch.object(native, 'MAX_BYTES', 9):
                self.assertEqual(cache.load(spec), ())
            with patch('midas.checker.native.os.replace', side_effect=OSError):
                cache.store((('producer', b'replacement'),), spec)
            self.assertEqual(cache.load(spec)[0][1], b'0123456789')


if __name__ == '__main__':
    unittest.main()
