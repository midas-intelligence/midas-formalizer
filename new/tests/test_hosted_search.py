"""LeanExplore relevance regressions; no network, Lean or model execution."""
import io
from threading import Event
import time
import unittest
from unittest.mock import patch

from midas.tools import Backend, Search
from midas.tools.search import hosted


class HostedSearchTests(unittest.TestCase):
    def search(self, query, payload):
        with patch('json.loads', side_effect=AssertionError('whole-response JSON')), \
                patch('json.dumps', side_effect=AssertionError('whole-response JSON')):
            return tuple(row.names[0] for row in hosted(
                Search(query, Backend('leanexplore')), Event(), time.monotonic()+10,
                opener=lambda *args, **kwargs: io.BytesIO(payload)))

    def test_forward_difference_keeps_semantic_hits_and_rejects_choice_noise(self):
        # Names and description excerpts from the problematic run's live query.
        payload = br'''{"results":[
          {"name":"Polynomial.fwdDiff_iter_degree_add_one_eq_zero",
           "informalization":"Iterated Forward Difference of a Polynomial. The iterated forward difference is identically zero."},
          {"name":"fwdDiff_iter_eq_sum_shift",
           "informalization":"Iterated Forward Difference Formula.",
           "docstring":"Express the n-th forward difference of f at y in terms of the values f (y + k)."},
          {"name":"Multiset.choose",
           "docstring":"Given a proof hp of a unique element, choose p l hp returns that element.",
           "informalization":"Unique Selection from a Multiset. Given a multiset and a predicate, this operation returns the unique element satisfying the predicate."},
          {"name":"CategoryTheory.SingleObj.differenceFunctor_obj",
           "informalization":"Object Mapping of the Difference Functor. Every object maps to the unique object of the target category."}
        ]}'''
        self.assertEqual(self.search('polynomial forward difference Newton interpolation choose', payload),
            ('Polynomial.fwdDiff_iter_degree_add_one_eq_zero', 'fwdDiff_iter_eq_sum_shift'))

    def test_repeated_keywords_and_boilerplate_do_not_supply_extra_support(self):
        payload = br'''{"results":[
          {"name":"Int.alternating_sum_range_choose",
           "informalization":"Alternating Sum of Binomial Coefficients."},
          {"name":"Multiset.choose",
           "docstring":"The theorem for choose: choose an element of the multiset."}
        ]}'''
        self.assertEqual(self.search('find the theorem for sum choose negative choose inverse convolution', payload),
                         ('Int.alternating_sum_range_choose',))

    def test_declaration_header_preserves_mahler_binomial_identity(self):
        payload = br'''{"results":[
          {"name":"mahler_apply",
           "informalization":"Evaluation of Mahler Basis Functions. The Mahler basis function is a generalized binomial coefficient.",
           "source_text":"theorem mahler_apply (k : Nat) (x : R) : mahler k x = Ring.choose x k := by\n  exact helper\n"}
        ]}'''
        self.assertEqual(self.search('mahler polynomial eval choose', payload), ('mahler_apply',))

    def test_proof_and_neighbouring_commands_are_not_relevance_evidence(self):
        payload = br'''{"results":[
          {"name":"Multiset.choose",
           "source_text":"def choose (p : Prop) : Nat := by\n  exact Polynomial.helper p\ntheorem polynomial : True := by trivial\n"}
        ]}'''
        self.assertEqual(self.search('polynomial choose', payload), ())

    def test_descriptions_supply_natural_language_and_compound_terms(self):
        payload = br'''{"results":[
          {"name":"SSet.KanComplex.hornFilling",
           "informalization":"Horn Filling for Kan Complexes. Any horn can be filled."}
        ]}'''
        self.assertEqual(self.search('KanComplex extension', payload), ('SSet.KanComplex.hornFilling',))
        payload = br'''{"results":[
          {"name":"IsCompact.exists_isMaxOn",
           "informalization":"A continuous function on a compact set attains its maximum."}
        ]}'''
        self.assertEqual(self.search('continuous function attains maximum', payload), ('IsCompact.exists_isMaxOn',))

    def test_single_term_exact_names_and_sparse_metadata_remain_discoverable(self):
        payload = br'''{"results":[
          {"name":"Multiset.choose", "informalization":"Unique element selection."}
        ]}'''
        for query in ('choose', 'choose choose', 'Multiset.choose'):
            self.assertEqual(self.search(query, payload), ('Multiset.choose',))
        sparse = br'''{"results":[{"name":"Multiset.choose", "docstring":null}]}'''
        self.assertEqual(self.search('polynomial choose', sparse), ('Multiset.choose',))

    def test_incomplete_metadata_does_not_establish_irrelevance(self):
        payload = (b'{"results":[{"name":"N.result", "informalization":"' + b'x'*17000 +
                   b' polynomial choose"}]}')
        self.assertEqual(self.search('polynomial choose', payload), ('N.result',))

    def test_filtering_does_not_hide_invalid_names(self):
        payload = br'''{"results":[{"name":2, "informalization":"Unrelated selection."}]}'''
        with self.assertRaises(ValueError):
            self.search('polynomial choose', payload)


if __name__ == '__main__':
    unittest.main()
