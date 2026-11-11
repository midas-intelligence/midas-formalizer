"""Offline ranking/segmentation guards; no Lean or optional dependencies."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from tests.benchmark_search_long_tokens import Tokenizer, build, compound_tokens, retrieve
from tests.benchmark_search_pretrained import retrieve as previous_retrieve


class LongTokenTests(unittest.TestCase):
    def test_compounds_preserve_identifier_boundaries(self):
        def decode(value):
            return [bytes.fromhex(token.removeprefix('longx')).decode() for token in compound_tokens(value)]
        self.assertEqual(decode('Nat.eq_pi_mul'), ['eq_pi','pi_mul','eq_pi_mul'])
        self.assertEqual(decode('eqPi'), ['eq_pi'])
        self.assertEqual(decode('eq pi Eq.Pi eq__pi'), [])
        self.assertEqual(decode('α_eq_β'), ['α_eq','eq_β','α_eq_β'])

    def test_long_match_beats_short_word_distractors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.sqlite'
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE documents(id INTEGER,name TEXT,documentation TEXT,statement TEXT)')
                # Equal component frequencies; only the target has the compound.
                db.executemany('INSERT INTO documents VALUES(?,?,?,?)',
                    [(i,f'Noise{i}.eq_pi','','') for i in range(1,121)] + [(121,'Target.pi_eq','','')])
            for mode in ('baseline','compounds'):
                tokenizer = Tokenizer(mode)
                path = root/(mode+'.sqlite')
                build(source,path,tokenizer)
                with sqlite3.connect(path) as db:
                    rows = retrieve(db,'pi_eq',tokenizer)
                    if mode == 'baseline':
                        self.assertEqual(rows,previous_retrieve(db,'pi_eq'))
                        self.assertNotIn((121,'Target.pi_eq'),rows)
                    else:
                        self.assertEqual(rows[0],(121,'Target.pi_eq'))
                    self.assertEqual(retrieve(db,'Target.pi_eq',tokenizer)[0],(121,'Target.pi_eq'))
                    self.assertTrue(retrieve(db,'pi',tokenizer))
                    self.assertEqual(retrieve(db,'the and',tokenizer),[])

    def test_filtering_bpe_pieces_keeps_lexical_short_queries(self):
        tokenizer = Tokenizer('append')
        tokenizer.word = lambda word: ()
        self.assertEqual(tokenizer('eq dvd pi'), 'eq dvd pi')


if __name__ == '__main__':
    unittest.main()
