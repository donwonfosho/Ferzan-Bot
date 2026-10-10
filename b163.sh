set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=5b68c15570d136a644d14cbcc2900bd2f3667707
KNOWN="39edea353ac064c04ede76244836fbe079a9f3b1"
LB="$APP/Ferzan-Ecosystem/Launch Bot"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD)
[ "$OLD" = "$WANT" ] && { echo "Already installed (${WANT:0:7}). Nothing to do."; exit 0; }
case " $KNOWN " in *" $OLD "*) ;; *) fail "the server is on $(git rev-parse --short HEAD), which this installer was not built for. Nothing changed. Send that version to Claude.";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(api|status_check)\.py$|^Ferzan-Ecosystem/Launch Bot/tests/test_status_check\.py$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
systemctl is-active -q ferzan-launch-api || fail "ferzan-launch-api is not running"
echo "dry-run ok: adds the public read-only /api/status (normal / degraded / down) the website footer will show"
rollback(){ cd $APP; git reset -q --hard $OLD; }
git merge -q --ff-only $WANT || fail "merge failed"
cd "$LB" || { rollback; fail "no Launch Bot dir; rolled back"; }
for f in api.py status_check.py; do python3 -m py_compile "$f" || { rollback; fail "$f does not compile; rolled back"; }; done
[ -x /opt/ferzan/.venv/bin/python ] && PY=/opt/ferzan/.venv/bin/python || PY=python3
$PY -m unittest tests.test_status_check 2>&1 | tail -4 | grep -q '^OK' || { $PY -m unittest tests.test_status_check 2>&1 | tail -12; rollback; fail "tests failed; rolled back"; }
echo "tests ok"
cd $APP
bad(){ P=$(systemctl show -p MainPID --value ferzan-launch-api); ! systemctl is-active -q ferzan-launch-api || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
systemctl restart ferzan-launch-api; sleep 25
if bad; then sleep 20; fi
if bad; then journalctl -u ferzan-launch-api -n 8 --no-pager | tail -7; rollback; systemctl restart ferzan-launch-api; fail "ferzan-launch-api unhealthy; rolled back"; fi
echo "ferzan-launch-api running"
R=$(curl -s -m 12 http://127.0.0.1:8000/api/status)
echo "status answer: $R"
echo "$R" | grep -q '"status"' || { rollback; systemctl restart ferzan-launch-api; fail "/api/status did not answer; rolled back"; }
echo "$R" | grep -q 'ferzan-' && { rollback; systemctl restart ferzan-launch-api; fail "status leaked a service name; rolled back"; }
echo "public check (through the web server): $(curl -s -m 12 -o /dev/null -w 'http %{http_code}' https://launch.ferzaneco.com/api/status)"
echo "INSTALLED ${WANT:0:7}. To undo: cd $APP && git reset --hard $OLD && systemctl restart ferzan-launch-api"
