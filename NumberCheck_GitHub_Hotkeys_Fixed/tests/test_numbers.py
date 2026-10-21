import ast
from pathlib import Path

# Extract pure functions without starting the desktop GUI or requiring Windows.
source = Path(__file__).resolve().parents[1].joinpath('number_check.py').read_text(encoding='utf-8')
tree = ast.parse(source)
ns = {'re': __import__('re')}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in {'checksum_ok', 'id_candidates'}:
        mod = ast.Module(body=[node], type_ignores=[])
        exec(compile(mod, '<pure-functions>', 'exec'), ns)

def test_13_digits_accepted():
    assert ns['checksum_ok']('1101700203450') is True

def test_bad_checksum_rejected():
    assert ns['checksum_ok']('1101700203451') is False

def test_grouped_id_reading():
    assert '1101700203451' in ns['id_candidates']('เลขประจำตัว 1 1017 00203 45 1')

def test_not_join_unrelated_lines():
    assert ns['id_candidates']('12345\n67890123') == []
