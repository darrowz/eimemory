set -e
cd /opt/eimemory
rm -rf .venv  # remove broken leftover
pip3 install --break-system-packages -e . 2>&1 | tail -8
pip3 show eimemory 2>&1 | head -5
which eimemory
eimemory --help 2>&1 | head -5
echo "--- DONE ---"
