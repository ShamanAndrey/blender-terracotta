"""Build terracotta.zip for installation.

Globs the package rather than listing files: a hardcoded list silently ships a
stale addon the day a module is added -- which already happened once.
"""

import glob
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "terracotta")
OUT = os.path.join(ROOT, "terracotta.zip")


def package_files():
    files = []
    for pattern in ("*.py", "*.blend"):
        files.extend(glob.glob(os.path.join(PKG, pattern)))
    return sorted(files)


def main():
    files = package_files()
    # The addon is standalone only if the bundled datablocks actually ship;
    # a zip without them "builds fine" and fails at the user's desk.
    names = {os.path.basename(f) for f in files}
    for required in ("__init__.py", "workspaces.blend", "examples.blend"):
        if required not in names:
            raise SystemExit(
                f"refusing to build: {required} is missing from {PKG} -- "
                "regenerate it before packaging")
    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for full in files:
            z.write(full, os.path.join("terracotta", os.path.basename(full)))
    print(f"wrote {OUT}: {len(files)} files")
    for f in files:
        print("  ", os.path.basename(f))


if __name__ == "__main__":
    main()
