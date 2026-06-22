"""Top-level PyInstaller entry shim.

PyInstaller must NOT target a module *inside* the package (skillshare_cli/main.py)
— running that file as __main__ strips its package context and every
`from .authflow import …` blows up with "attempted relative import with no known
parent package". This shim lives outside the package and imports it the normal
absolute way, so PyInstaller bundles the whole `skillshare_cli` package and all
relative imports resolve. Build with:  pyinstaller --onefile --name skillshare run.py
"""

from skillshare_cli.main import main

if __name__ == "__main__":
    main()
