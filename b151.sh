set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=6b7eb23b828512296b110653ffadf3612560d73f
KNOWN="60a014381d88b8dba20072d44350e48c68ff5559"
ENV=/opt/ferzan/.env; LB="$APP/Ferzan-Ecosystem/Launch Bot"; WEB=/var/www/ferzan-launch; B=/tmp/b151-backup
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD)
[ "$OLD" = "$WANT" ] && { echo "Already installed (${WANT:0:7}). Nothing to do."; exit 0; }
case " $KNOWN " in *" $OLD "*) ;; *) fail "the server is on $(git rev-parse --short HEAD), which this installer was not built for. Nothing changed. Send that version to Claude.";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(guardian_app|buy_app)\.py$|^Ferzan-Ecosystem/Launch Bot/tests/test_guardian_setup\.py$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
[ -d "$WEB" ] && [ -f "$WEB/app.html" ] || fail "$WEB or its app.html is missing. Nothing changed."
for k in GUARDIAN_TOKEN BUYBOT_TOKEN; do grep -q "^$k=.\+" $ENV || fail "$k is empty in $ENV"; done
for u in ferzan-launch-api; do systemctl is-active -q $u || fail "$u is not running"; done
echo "dry-run ok: the app backends will read the bot tokens from /opt/ferzan/.env"
rollback(){ cd $APP; git reset -q --hard $OLD; }
rm -rf $B; mkdir -p $B; cp -p $ENV $B/env
git merge -q --ff-only $WANT || fail "merge failed"
cd "$LB" || { rollback; fail "no Launch Bot dir; rolled back"; }
for f in guardian_app.py buy_app.py; do python3 -m py_compile "$f" || { rollback; fail "$f does not compile; rolled back"; }; done
[ -x /opt/ferzan/.venv/bin/python ] && PY=/opt/ferzan/.venv/bin/python || PY=python3
t(){ (cd "$1" && $PY -m unittest $2 2>&1 | tail -4 | grep -q '^OK') || { (cd "$1" && $PY -m unittest $2 2>&1 | tail -12); rollback; fail "tests failed ($2); rolled back"; }; }
t "$LB" "tests.test_guardian_app tests.test_buy_app tests.test_buy_app_track tests.test_guardian_setup"
echo "tests ok"
cd $APP
ALL="ferzan-launch-api"
echo "restarting: $ALL"
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
systemctl restart $ALL; sleep 30
for u in $ALL; do
  if bad $u; then sleep 20; fi
  if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; rollback; systemctl restart $ALL; fail "$u unhealthy; everything rolled back"; fi
done
for r in buybot/lookup buybot/track guardian/tier guardian/perm; do
  C=$(curl -s -m 8 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"initData":"x","chat_id":-1,"chain":"base","ca":"x","tier":"x","command":"x"}' http://127.0.0.1:8000/api/$r)
  echo "$r answers a fake sign-in with: $C (expect 401)"
  [ "$C" = 401 ] || { rollback; systemctl restart $ALL; fail "$r did not answer as expected; rolled back"; }
done
echo "launch API sees the tokens: $(cd "$LB" && $PY -c 'import guardian_app,buy_app;print("guardian",bool(guardian_app._token()),"buy",bool(buy_app._token()))')"
BASE=$(grep '^MINI_APP_BASE_URL=' $ENV | cut -d= -f2-); BASE=${BASE:-https://launch.ferzaneco.com/miniapp}
for pg in guardian buybot; do echo "page $BASE/$pg.html answers: $(curl -s -m 10 -o /dev/null -w '%{http_code}' $BASE/$pg.html)"; done
echo "INSTALLED ${WANT:0:7}. Backups in $B."
