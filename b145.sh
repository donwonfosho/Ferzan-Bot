set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=a952d21146824e7d76082160267414c29204043d; KNOWN="b1fc2416d852e47c815bb211867660e325050503"
ENV=/opt/ferzan/.env; LB="$APP/Ferzan-Ecosystem/Launch Bot"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD)
[ "$OLD" = "$WANT" ] && { echo "Already installed (${WANT:0:7}). Nothing to do."; exit 0; }
case " $KNOWN " in *" $OLD "*) ;; *) fail "the server is on $(git rev-parse --short HEAD), which this installer was not built for. Nothing changed. Send that version to Claude.";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(api|guardian_app)\.py$|^Ferzan-Ecosystem/Launch Bot/miniapp/guardian\.html$|^Ferzan-Ecosystem/Launch Bot/tests/test_guardian_app\.py$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
[ -f $ENV ] || fail "no $ENV"
grep -q '^GUARDIAN_TOKEN=.\+' $ENV || fail "GUARDIAN_TOKEN is empty in $ENV"
systemctl is-active -q ferzan-launch-api || fail "ferzan-launch-api is not running"
echo "dry-run ok: 4 files (launch API route, Guardian backend, page, tests)"
rollback(){ cd $APP; git reset -q --hard $OLD; }
git merge -q --ff-only $WANT || fail "merge failed"
cd "$LB" || { rollback; fail "no Launch Bot dir; rolled back"; }
for f in api.py guardian_app.py; do python3 -m py_compile "$f" || { rollback; fail "$f does not compile; rolled back"; }; done
[ -x /opt/ferzan/.venv/bin/python ] && PY=/opt/ferzan/.venv/bin/python || PY=python3
$PY -m unittest tests.test_guardian_app 2>&1 | tail -4 | grep -q '^OK' || { $PY -m unittest tests.test_guardian_app 2>&1 | tail -12; rollback; fail "tests failed; rolled back"; }
echo "tests ok (20)"
cd $APP
echo "restarting: ferzan-launch-api"
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
systemctl restart ferzan-launch-api; sleep 30
if bad ferzan-launch-api; then sleep 20; fi
if bad ferzan-launch-api; then journalctl -u ferzan-launch-api -n 8 --no-pager | tail -7; rollback; systemctl restart ferzan-launch-api; fail "ferzan-launch-api unhealthy; rolled back"; fi
C=$(curl -s -m 8 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"initData":"x"}' http://127.0.0.1:8000/api/guardian/groups)
echo "Guardian route answers a fake sign-in with: $C (expect 401)"
[ "$C" = 401 ] || { rollback; systemctl restart ferzan-launch-api; fail "route did not answer as expected; rolled back"; }
BASE=$(grep '^MINI_APP_BASE_URL=' $ENV | cut -d= -f2-); BASE=${BASE:-https://launch.ferzaneco.com/miniapp}
echo "page at $BASE/guardian.html answers: $(curl -s -m 10 -o /dev/null -w '%{http_code}' $BASE/guardian.html)  (200 = the page is being served; 404 = tell Claude)"
echo "INSTALLED ${WANT:0:7}: Guardian Mini App backend is live. The bot has no button for it yet."
