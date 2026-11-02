#!/bin/sh
# Runs every test suite. Integration suites start a headless LibreOffice.
#   sh scripts/run_tests.sh
# On macOS the UNO-capable Python is the one bundled with LibreOffice; set
# UNO_PYTHON to override.
set -e
cd "$(dirname "$0")/.."

if [ -z "$UNO_PYTHON" ]; then
  if [ -x /Applications/LibreOffice.app/Contents/Resources/python ]; then
    UNO_PYTHON=/Applications/LibreOffice.app/Contents/Resources/python
  else
    UNO_PYTHON=/usr/bin/python3
  fi
fi

python3 scripts/build_oxt.py
python3 scripts/build_oxt.py --test

echo "== unit"
python3 -m unittest discover -s tests/unit

for t in test_reader test_executor test_extension; do
  echo "== $t"
  "$UNO_PYTHON" tests/integration/$t.py
done

# Leave the product build installed, not the test build.
echo "Done. Install dist/ciu-libreoffice.oxt for normal use."
