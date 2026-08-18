set +e
echo "=== releases dir (newest 12) ==="
ls -lt /opt/eimemory/releases/ 2>&1 | head -12
echo
echo "=== current symlink ==="
ls -la /opt/eimemory/current 2>&1
echo
echo "=== current VERSION ==="
cat /opt/eimemory/current/VERSION 2>/dev/null
cat /opt/eimemory/current/.version 2>/dev/null
echo
echo "=== disk usage ==="
du -sh /opt/eimemory/releases 2>/dev/null
df -h /opt/eimemory 2>/dev/null
echo
echo "=== 1.9.13x releases (sorted) ==="
for d in /opt/eimemory/releases/*/; do
  v=""
  [ -f "$d/VERSION" ] && v=$(cat "$d/VERSION")
  [ -z "$v" ] && [ -f "$d/.version" ] && v=$(cat "$d/.version")
  printf "%s\t%s\n" "$v" "${d%/}"
done | sort -V | tail -10
echo
echo "=== latest 5 commit hashes ==="
ls -1t /opt/eimemory/releases/ | head -5
echo
echo "=== 1.9.133 in 1.9.13x list? ==="
for d in /opt/eimemory/releases/*/; do
  v=""
  [ -f "$d/VERSION" ] && v=$(cat "$d/VERSION")
  [ -z "$v" ] && [ -f "$d/.version" ] && v=$(cat "$d/.version")
  echo "$v $d"
done | grep "1.9.13"
