set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=ac0ced299900543897fd03232436037f377ded9e
KNOWN="6b7eb23b828512296b110653ffadf3612560d73f 60a014381d88b8dba20072d44350e48c68ff5559"
ENV=/opt/ferzan/.env; LB="$APP/Ferzan-Ecosystem/Launch Bot"; WEB=/var/www/ferzan-launch; B=/tmp/b152-backup
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD)
[ "$OLD" = "$WANT" ] && { echo "Already installed (${WANT:0:7}). Nothing to do."; exit 0; }
case " $KNOWN " in *" $OLD "*) ;; *) fail "the server is on $(git rev-parse --short HEAD), which this installer was not built for. Nothing changed. Send that version to Claude.";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(api|ton_relay|guardian_app|buy_app)\.py$|^Ferzan-Ecosystem/Launch Bot/tests/test_(ton_relay|guardian_setup)\.py$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
[ -d "$WEB" ] && [ -f "$WEB/app.html" ] || fail "$WEB or its app.html is missing. Nothing changed."
for u in ferzan-launch-api; do systemctl is-active -q $u || fail "$u is not running"; done
echo "dry-run ok: adds the TON relay routes the website will use (reads wallet state, broadcasts signed messages)"
rollback(){ cd $APP; git reset -q --hard $OLD; }
rm -rf $B; mkdir -p $B; cp -p $ENV $B/env
git merge -q --ff-only $WANT || fail "merge failed"
cd "$LB" || { rollback; fail "no Launch Bot dir; rolled back"; }
for f in api.py ton_relay.py; do python3 -m py_compile "$f" || { rollback; fail "$f does not compile; rolled back"; }; done
[ -x /opt/ferzan/.venv/bin/python ] && PY=/opt/ferzan/.venv/bin/python || PY=python3
t(){ (cd "$1" && $PY -m unittest $2 2>&1 | tail -4 | grep -q '^OK') || { (cd "$1" && $PY -m unittest $2 2>&1 | tail -12); rollback; fail "tests failed ($2); rolled back"; }; }
t "$LB" "tests.test_ton_relay tests.test_guardian_app tests.test_buy_app tests.test_buy_app_track tests.test_guardian_setup"
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
C=$(curl -s -m 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/ton/relay/wallet/not-an-address)
echo "relay refuses a bad address with: $C (expect 400)"
[ "$C" = 400 ] || { rollback; systemctl restart $ALL; fail "relay route did not answer as expected; rolled back"; }
C=$(curl -s -m 8 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"boc":"AAAA"}' http://127.0.0.1:8000/api/ton/relay/send)
echo "relay refuses a junk message with: $C (expect 400)"
[ "$C" = 400 ] || { rollback; systemctl restart $ALL; fail "relay send route did not answer as expected; rolled back"; }
echo "live check of the account wallet through the relay (state/seqno; 502 would mean the TON network still refuses):"
curl -s -m 20 -w ' [http %{http_code}]
' http://127.0.0.1:8000/api/ton/relay/wallet/UQCUH5O7y33p4Y8GCZlqxEKTXY_sgnjW_LhK2cMGa4g-ZeoD
echo "toncenter key present in env file: $(grep -c '^TONCENTER_API_KEY=.\+' $ENV) (1 = yes, 0 = no: ask Claude)"
echo "INSTALLED ${WANT:0:7}. Backups in $B."
