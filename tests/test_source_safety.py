"""Offline snapshot checks. Never import or execute the application."""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'bot.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
ASSIGNMENTS = {
    node.targets[0].id: node.value
    for node in TREE.body
    if isinstance(node, ast.Assign)
    and len(node.targets) == 1
    and isinstance(node.targets[0], ast.Name)
}


class SourceSafetyTests(unittest.TestCase):
    def test_source_compiles_without_execution(self):
        compile(SOURCE, 'bot.py', 'exec')

    def test_credentials_are_environment_based(self):
        for name in ('BOT_TOKEN', 'ADMIN_ID', 'PHOTO_CHANNEL_ID', 'SPREADSHEET_NAME', 'SA_FILE'):
            with self.subTest(name=name):
                self.assertIn('os.environ.get', ast.unparse(ASSIGNMENTS[name]))
                self.assertNotIsInstance(ASSIGNMENTS[name], ast.Constant)

    def test_inventory_defaults_are_empty(self):
        for name in ('WORKSHOPS', 'WORKSHOP_SECTIONS', 'SECTION_EQUIPMENT', 'WORKSHOP_SHEET_NAMES'):
            with self.subTest(name=name):
                self.assertEqual(ast.literal_eval(ASSIGNMENTS[name]), {})

    def test_environment_template_is_blank_or_disabled(self):
        for line in (ROOT / '.env.example').read_text(encoding='utf-8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            name, value = line.split('=', 1)
            self.assertIn(value.strip(), ('', '0'), name)

    def test_startup_guards_precede_state_loading(self):
        main = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        for statement in main.body[:2]:
            self.assertIsInstance(statement, ast.If)
            self.assertTrue(any(isinstance(n, ast.Raise) for n in ast.walk(statement)))
        first = ast.unparse(main.body[0])
        self.assertTrue(all(name in first for name in ('BOT_TOKEN', 'ADMIN_ID', 'SPREADSHEET_NAME')))
        self.assertIn('os.path.isfile(SA_FILE)', ast.unparse(main.body[1]))
        self.assertEqual(ast.unparse(main.body[2]), 'load_requests()')

    def test_no_common_credential_patterns_in_source(self):
        patterns = (
            r'\b\d{6,}:[A-Za-z0-9_-]{25,}\b',
            r'\bAIza[A-Za-z0-9_-]{30,}\b',
            r'\bgh[pousr]_[A-Za-z0-9]{20,}\b',
            r'\bgithub_pat_[A-Za-z0-9_]{20,}\b',
            r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
            r'[\w.-]+@[\w.-]+\.iam\.gserviceaccount\.com',
            r'[\w-]+\.apps\.googleusercontent\.com',
        )
        for pattern in patterns:
            self.assertIsNone(re.search(pattern, SOURCE), 'Credential-like content found; value suppressed')

    def test_no_real_looking_telegram_ids_in_literals(self):
        for node in ast.walk(TREE):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertIsNone(re.search(r'\b\d{7,}\b', node.value), f'ID-like string at line {node.lineno}')


if __name__ == '__main__':
    unittest.main()
