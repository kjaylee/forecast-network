"""Binding mapping wrappers must normalize before exact JSON type validation."""
import ast
import unittest
from collections import UserDict, UserList
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def actual_normalizer():
    tree = ast.parse((Path(__file__).resolve().parents[1]/'apps/web/src/entry.py').read_text())
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == 'python_value')
    scope = {'Any': Any, 'Mapping': Mapping, 'Sequence': Sequence}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual_runtime_normalizer', 'exec'), scope)
    return scope['python_value']


class RuntimeValuesTests(unittest.TestCase):
    def test_binding_wrappers_become_plain_json_values_recursively(self):
        result = actual_normalizer()(UserDict({'response': UserDict({'agrees': True, 'explanation': 'Bounded review'}),
                                             'choices': UserList([UserDict({'index': 0})])}))
        self.assertIs(type(result), dict)
        self.assertIs(type(result['response']), dict)
        self.assertIs(type(result['choices']), list)
        self.assertIs(type(result['choices'][0]), dict)
        self.assertIs(type(result['response']['agrees']), bool)
        self.assertIs(type(result['choices'][0]['index']), int)

    def test_native_proxy_conversion_then_wrapper_normalization(self):
        class Proxy:
            def to_py(self):
                return UserDict({'results': UserList([UserDict({'ok': 1})])})
        self.assertEqual(actual_normalizer()(Proxy()), {'results': [{'ok': 1}]})

    def test_non_string_keys_and_excessive_depth_fail_closed(self):
        with self.assertRaises(ValueError):
            actual_normalizer()(UserDict({1: 'invalid'}))
        nested = {}
        for _ in range(34):
            nested = {'child': nested}
        with self.assertRaises(ValueError):
            actual_normalizer()(nested)

    def test_text_bytes_null_and_integral_values_are_not_reinterpreted(self):
        normalize = actual_normalizer()
        for value in ('text', b'raw', bytearray(b'raw'), None, 10, True):
            self.assertEqual(normalize(value), value)
            self.assertIs(type(normalize(value)), type(value))
