import ast
from pathlib import Path

def parse_skeleton(source_code: str) -> str:
    tree = ast.parse(source_code)
    lines = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            lines.append(f'class {node.name} (L{node.lineno}): methods={methods}')
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            lines.append(f'def {node.name}({", ".join(args)}) (L{node.lineno})')
    return '\n'.join(lines) or 'No top-level symbols'

target = globals().get('TASK_INPUT', '')
if target and Path(target).exists():
    print(parse_skeleton(Path(target).read_text(encoding='utf-8')))
elif target:
    print(parse_skeleton(target))
