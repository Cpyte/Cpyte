import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'source'))
from cpyte.linker import (
    Linker, find_linker, build as _build, LinkerNotFoundError
)


def main():
    args = sys.argv[1:]

    if not args or args[0] in ('-h', '--help'):
        print('usage: python linker/main.py <command> [options]', file=sys.stderr)
        print('commands:', file=sys.stderr)
        print('  find                    print the detected linker path', file=sys.stderr)
        print('  build <objects...>      compile .c files and link into executable', file=sys.stderr)
        sys.exit(1 if args and args[0] not in ('-h', '--help') else 0)

    cmd = args.pop(0)

    if cmd == 'find':
        try:
            print(find_linker())
        except LinkerNotFoundError as e:
            print(e, file=sys.stderr)
            sys.exit(1)

    elif cmd == 'build':
        output = None
        debug = False
        opt = 3
        positional = []
        i = 0
        while i < len(args):
            a = args[i]
            if a == '-o' and i + 1 < len(args):
                output = args[i + 1]
                i += 2
            elif a == '-g':
                debug = True
                i += 1
            elif a == '-O' and i + 1 < len(args):
                opt = int(args[i + 1])
                i += 2
            elif a.startswith('-O') and len(a) > 2:
                opt = int(a[2:])
                i += 1
            else:
                positional.append(a)
                i += 1
        if not positional:
            print('error: no object files specified', file=sys.stderr)
            sys.exit(1)
        _build(positional, output=output, debug=debug, opt_level=opt)

    else:
        print(f'unknown command: {cmd}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
