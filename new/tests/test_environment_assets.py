from hashlib import sha1, sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from midas.environment.assets import included_paths, source_assets
from midas.environment.inventory import EnvironmentError, File


class SourceAssetsTests(unittest.TestCase):
    def test_literal_paths_ignore_comments_and_string_contents(self):
        source = b'''/- include_str "absent" /- nested -/ -/
-- include_str "absent"
def text := "include_str absent"
def value := include_str ".." / "widget" / "app.js"
'''
        self.assertEqual(included_paths(source), ('../widget/app.js',))
        with self.assertRaisesRegex(EnvironmentError, 'literal'):
            included_paths(b'def value := include_str computedPath')

    def test_assets_require_committed_bytes_and_stay_inside_source_root(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'A').mkdir()
            (root/'widget').mkdir()
            data = b'committed widget'
            (root/'widget/app.js').write_bytes(data)
            source = b'def value := include_str ".." / "widget" / "app.js"\n'
            (root/'A/B.lean').write_bytes(source)
            pin = File('A/B.lean', len(source), False, sha256(source).hexdigest())
            digest = sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
            listing = lambda path: {path: (False, digest)}
            assets = source_assets(root, pin, 'A.B', listing, {})
            self.assertEqual(assets[0][0], 'widget/app.js')
            (root/'widget/app.js').write_bytes(b'changed widget')
            with self.assertRaisesRegex(EnvironmentError, 'committed'):
                source_assets(root, pin, 'A.B', listing, {})
            (root/'A/B.lean').write_bytes(b'def value := include_str "../../escape"')
            with self.assertRaises(EnvironmentError):
                source_assets(root, pin, 'A.B', listing, {})
