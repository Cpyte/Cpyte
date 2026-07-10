import sys
import os
from .lexar import Lexer, LexerError
from .astparse import parse_file, ParseError
from .semantic_analasis import analyze
from .bytecoding import LLVM
from .compiling import run_jit, run_aot


def pretty_ast(node, indent=0):
    pad = '  ' * indent
    if isinstance(node, list):
        if not node:
            return f'{pad}(empty)'
        lines = []
        for item in node:
            lines.append(pretty_ast(item, indent))
        return '\n'.join(lines)

    name = type(node).__name__

    if name == 'Number':
        return f'{pad}{node.value}'

    if name == 'String':
        return f"{pad}'{node.value}'"

    if name == 'Variable':
        return f'{pad}{node.name}'

    if name == 'VarDecl':
        init = f' = {pretty_ast(node.init, indent)}' if node.init else ''
        return f'{pad}{node.var_type} {node.name}{init}'

    if name == 'ExprStmt':
        return f'{pad}statement:\n{pretty_ast(node.expr, indent + 1)}'

    if name == 'Assign':
        if isinstance(node.target, str):
            return f'{pad}{node.target} =\n{pretty_ast(node.value, indent + 1)}'
        return f'{pad}{pretty_ast(node.target, 0)} =\n{pretty_ast(node.value, indent + 1)}'

    if name == 'Return':
        if node.value is None:
            return f'{pad}return'
        return f'{pad}return\n{pretty_ast(node.value, indent + 1)}'

    if name == 'Print':
        return f'{pad}print\n{pretty_ast(node.value, indent + 1)}'

    if name == 'Input':
        return f'{pad}input()'

    if name == 'Break':
        return f'{pad}break'

    if name == 'Continue':
        return f'{pad}continue'

    if name == 'If':
        result = f'{pad}if\n{pretty_ast(node.cond, indent + 1)}'
        result += f'\n{pad}then:\n{pretty_ast(node.body, indent + 1)}'
        if node.orelse:
            result += f'\n{pad}else:\n{pretty_ast(node.orelse, indent + 1)}'
        return result

    if name == 'While':
        result = f'{pad}while\n{pretty_ast(node.cond, indent + 1)}'
        result += f'\n{pad}body:\n{pretty_ast(node.body, indent + 1)}'
        return result

    if name == 'Import':
        extra = f' [{node.src_file}]' if node.src_file else ''
        return f'{pad}import {node.module}{extra}'

    if name == 'NewExpr':
        size = f'[{pretty_ast(node.size, 0)}]' if node.size else ''
        return f'{pad}new {node.type_expr}{size}'

    if name == 'Deref':
        return f'{pad}*\n{pretty_ast(node.operand, indent + 1)}'

    if name == 'AddrOf':
        return f'{pad}&\n{pretty_ast(node.operand, indent + 1)}'

    if name == 'SizeOf':
        return f'{pad}sizeof({node.type_expr})'

    if name == 'StructDef':
        gp = f'<{", ".join(node.generic_params)}>' if node.generic_params else ''
        result = f'{pad}struct {node.name}{gp}:'
        for f in node.fields:
            result += f'\n{pad}  {f.type_expr} {f.name}'
        return result

    if name == 'Field':
        return f'{pad}{node.type_expr} {node.name}'

    if name == 'Switch':
        result = f'{pad}switch\n{pretty_ast(node.value, indent + 1)}'
        for val, body in node.cases:
            label = 'default' if val is None else f'case {pretty_ast(val, 0)}'
            result += f'\n{pad}  {label}:\n{pretty_ast(body, indent + 2)}'
        return result

    if name == 'BinOp':
        return f'{pad}{node.op.name}\n{pretty_ast(node.left, indent + 1)}\n{pretty_ast(node.right, indent + 1)}'

    if name == 'UnaryOp':
        return f'{pad}{node.op.name}\n{pretty_ast(node.operand, indent + 1)}'

    if name == 'Call':
        result = f'{pad}call\n{pretty_ast(node.callee, indent + 1)}'
        if node.args:
            result += f'\n{pad}args:'
            for arg in node.args:
                result += f'\n{pretty_ast(arg, indent + 1)}'
        return result

    if name == 'Index':
        return f'{pad}index\n{pretty_ast(node.obj, indent + 1)}\n{pretty_ast(node.index, indent + 1)}'

    if name == 'Attr':
        return f'{pad}.{node.name}\n{pretty_ast(node.obj, indent + 1)}'

    if name == 'FuncDef':
        vis = f'{node.visibility} ' if node.visibility else ''
        ret = f' -> {node.rettype}' if node.rettype else ''
        result = f'{pad}{vis}def {node.name}({", ".join(f"{k}: {v}" for k, v in node.params.items())}){ret}:'
        for stmt in node.body:
            result += f'\n{pretty_ast(stmt, indent + 1)}'
        return result

    if isinstance(node, dict):
        t = node.get('type', '?')
        if t == 'class':
            result = f'{pad}class {node["name"]}:'
            for stmt in node.get('body', []):
                result += f'\n{pretty_ast(stmt, indent + 1)}'
            return result
        if t == 'for':
            result = f'{pad}for {node["var"]} in\n{pretty_ast(node["iter"], indent + 1)}'
            result += f'\n{pad}body:'
            for stmt in node.get('body', []):
                result += f'\n{pretty_ast(stmt, indent + 1)}'
            return result
        return f'{pad}{node}'

    return f'{pad}{node}'


def main():
    tab_size = 4
    mode = 'jit'
    args = sys.argv[1:]

    while args and args[0].startswith('--'):
        flag = args.pop(0)
        if flag == '--tab-size':
            tab_size = int(args.pop(0))
        elif flag == '--ast':
            mode = 'ast'
        elif flag == '--emit-llvm':
            mode = 'emit-llvm'
        elif flag == '--jit':
            mode = 'jit'
        elif flag == '--aot':
            mode = 'aot'
        else:
            print(f'Unknown flag: {flag}', file=sys.stderr)
            sys.exit(1)

    if not args:
        print('Usage: cpy [--tab-size N] [--ast|--emit-llvm|--jit|--aot] <source file>', file=sys.stderr)
        sys.exit(1)

    with open(args[0]) as f:
        source = f.read()

    lex = Lexer(source, tab_size=tab_size)
    tokens = lex.get_tokens()

    try:
        parsed, _ = parse_file(tokens)
    except (LexerError, ParseError) as e:
        print(f'parse error: {e}', file=sys.stderr)
        sys.exit(1)

    result = analyze(source, parsed)
    if result:
        print(result, file=sys.stderr)
        sys.exit(1)

    if mode == 'ast':
        print(pretty_ast(parsed))
        sys.exit(0)

    c = LLVM()
    try:
        prog, src_files = c.emit_program(parsed)
    except Exception as e:
        print(f'codegen error: {e}', file=sys.stderr)
        sys.exit(1)

    if mode == 'emit-llvm':
        print(prog)
    elif mode == 'aot':
        out_base = args[0].rsplit('.', 1)[0] if '.' in args[0] else 'program'
        obj_file = 'program.o'
        run_aot(prog, output=obj_file, src_files=src_files)
        print(f'Wrote {out_base}')
    else:
        run_jit(prog, src_files=src_files)


if __name__ == '__main__':
    main()
