"""Builds dist/ciu-libreoffice.oxt. --test also packages the test hook."""

import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INCLUDE = ["description.xml", "description", "META-INF", "registry", "python"]
SKIP_DIRS = {"__pycache__"}

TEST_COMPONENT = ("tests/integration/ciu_selftest.py", "python/ciu_selftest.py")
TEST_ENTRY = ('  <manifest:file-entry manifest:full-path="python/ciu_selftest.py"\n'
              '                       manifest:media-type="application/vnd.sun.star.uno-component;type=Python"/>\n')


def build(test=False, out=None):
    name = "ciu-libreoffice-test.oxt" if test else "ciu-libreoffice.oxt"
    out = out or os.path.join(ROOT, "dist", name)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in INCLUDE:
            path = os.path.join(ROOT, item)
            if os.path.isfile(path):
                z.write(path, item)
                continue
            for base, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for f in sorted(files):
                    if f.endswith(".pyc"):
                        continue
                    full = os.path.join(base, f)
                    arc = os.path.relpath(full, ROOT).replace(os.sep, "/")
                    if test and arc == "META-INF/manifest.xml":
                        with open(full, encoding="utf-8") as fh:
                            text = fh.read()
                        z.writestr(arc, text.replace("</manifest:manifest>",
                                                     TEST_ENTRY + "</manifest:manifest>"))
                        continue
                    z.write(full, arc)
        if test:
            z.write(os.path.join(ROOT, TEST_COMPONENT[0]), TEST_COMPONENT[1])
    return out


if __name__ == "__main__":
    print(build(test="--test" in sys.argv))
