#!/usr/bin/env python3
"""topology — unified CLI for the topology skill."""

import sys

from . import benchmark_llm, discover, docs, help as help_module, hooks, hooks_install, init, notation, playbook, show, sync


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv

    if not argv or argv[0] in ('help', '-h', '--help'):
        print(help_module.build_help_text())
        return

    command, rest = argv[0], argv[1:]
    dispatch = {
        'init': lambda: init.main(rest),
        'discover': lambda: discover.main(),
        'sync': lambda: sync.main(),
        'benchmark': lambda: benchmark_llm.main(rest),
        'show': lambda: show.main(),
        'docs': lambda: docs.main(),
        'run': lambda: playbook.main(rest),
        'playbook': lambda: playbook.playbook_main(rest),
        'hook': lambda: hooks.main(rest),
        'hooks': lambda: hooks_install.main(rest),
        'notation': lambda: notation.main(rest),
    }

    if command not in dispatch:
        print(f'Unknown command: {command}', file=sys.stderr)
        print()
        print(help_module.build_help_text())
        sys.exit(1)

    dispatch[command]()


if __name__ == '__main__':
    main()
