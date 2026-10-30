import dis
import json
import marshal
from pathlib import Path
import sys
import unittest
from code_fingerprint import fingerprint


class FingerprintTests(unittest.TestCase):
    def test_reference_and_intern_changes_do_not_change_code_fingerprint(self):
        env = {}
        exec(compile('async def f(p):\n return p.get("hello-world")\n', 'probe', 'exec'), env)
        code = env['f'].__code__
        old = marshal.dumps(code)
        before = fingerprint(code)
        held = list(code.co_consts)
        held_names = code.co_names
        sys.intern(held[1])
        self.assertNotEqual(old, marshal.dumps(code), 'Repro must expose marshal instability')
        self.assertEqual(before, fingerprint(code))
        list(dis.get_instructions(code))
        self.assertEqual(before, fingerprint(code))
        self.assertTrue(held_names)

    def test_behavior_change_is_detected(self):
        code = compile('result = 4', 'probe', 'exec')
        self.assertNotEqual(fingerprint(code), fingerprint(code.replace(co_consts=(5, None))))

    def test_nested_function_change_is_detected(self):
        self.assertNotEqual(fingerprint(compile('def f(): return 1', 'probe', 'exec')),
                            fingerprint(compile('def f(): return 2', 'probe', 'exec')))

    def test_recompile_is_stable(self):
        source = 'def f(x):\n return x in {"b", "a", "c"}\n'
        self.assertEqual(fingerprint(compile(source, 'probe', 'exec')), fingerprint(compile(source, 'probe', 'exec')))


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FingerprintTests))
    output = Path(sys.argv[1])
    with output.open('x', encoding='utf-8') as stream:
        json.dump({'passed': result.wasSuccessful(), 'tests': result.testsRun,
            'failures': [str(x) for x in result.failures], 'errors': [str(x) for x in result.errors]}, stream, indent=2)
    raise SystemExit(not result.wasSuccessful())
