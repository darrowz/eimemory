set -e
cd /opt/eimemory
# eimemory is zero-deps (pyproject: dependencies = [])
# Just need pytest and that's it
pip3 install --break-system-packages pytest==9.0.3 2>&1 | tail -3 || pip3 install pytest==9.0.3 2>&1 | tail -3
python3 -c "import eimemory; print('eimemory import OK')"
python3 -m eimemory --help 2>&1 | head -3 || python3 -c "from eimemory.cli.main import main; main(['--help'])" 2>&1 | head -10
echo "--- DONE ---"
