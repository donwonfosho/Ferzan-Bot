set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=d9413e7aacbf313a6d21e048aa0c9b5e5d015627
KNOWN="b1fc2416d852e47c815bb211867660e325050503 a952d21146824e7d76082160267414c29204043d 8c3a506dfd9f3c48638a8714e8d71411d5d4aebd"
ENV=/opt/ferzan/.env; LB="$APP/Ferzan-Ecosystem/Launch Bot"; B=/tmp/b147-backup
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD)
[ "$OLD" = "$WANT" ] && { echo "Already installed (${WANT:0:7}). Nothing to do."; exit 0; }
case " $KNOWN " in *" $OLD "*) ;; *) fail "the server is on $(git rev-parse --short HEAD), which this installer was not built for. Nothing changed. Send that version to Claude.";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(api|guardian_app|buy_app)\.py$|^Ferzan-Ecosystem/Launch Bot/miniapp/(guardian|buybot)\.html$|^Ferzan-Ecosystem/Launch Bot/tests/test_(guardian|buy)_app\.py$|^Ferzan-Ecosystem/Trade Desk/(db|ton_signer|ton_addr_fmt)\.py$|^Ferzan-Ecosystem/Trade Desk/tests/test_ton_wallet_form\.py$|^Ferzan-Ecosystem/Guardian Bot/(guardian_bot\.py|tests/test_start_card\.py)$|^Ferzan-Ecosystem/Buy Bot/(buy_bot\.py|tests/test_start_card\.py)$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
[ -f $ENV ] || fail "no $ENV"
for k in GUARDIAN_TOKEN BUYBOT_TOKEN; do grep -q "^$k=.\+" $ENV || fail "$k is empty in $ENV"; done
for u in ferzan-launch-api ferzan-guardian ferzan-buy; do systemctl is-active -q $u || fail "$u is not running"; done
echo "dry-run ok: Guardian + Buy mini apps, their /start buttons, TON wallet form (if not already installed)"
rollback(){ cd $APP; git reset -q --hard $OLD; [ -f $B/env ] && cp -p $B/env $ENV; }
rm -rf $B; mkdir -p $B; cp -p $ENV $B/env
git merge -q --ff-only $WANT || fail "merge failed"
cd "$LB" || { rollback; fail "no Launch Bot dir; rolled back"; }
for f in api.py guardian_app.py buy_app.py "../Trade Desk/db.py" "../Trade Desk/ton_signer.py" "../Trade Desk/ton_addr_fmt.py" "../Guardian Bot/guardian_bot.py" "../Buy Bot/buy_bot.py"; do python3 -m py_compile "$f" || { rollback; fail "$f does not compile; rolled back"; }; done
[ -x /opt/ferzan/.venv/bin/python ] && PY=/opt/ferzan/.venv/bin/python || PY=python3
t(){ (cd "$1" && $PY -m unittest $2 2>&1 | tail -4 | grep -q '^OK') || { (cd "$1" && $PY -m unittest $2 2>&1 | tail -12); rollback; fail "tests failed ($2); rolled back"; }; }
t "$LB" "tests.test_guardian_app tests.test_buy_app"
t "../Trade Desk" "tests.test_ton_wallet_form"
t "../Buy Bot" "tests.test_start_card"
t "../Guardian Bot" "tests.test_start_card"
echo "tests ok (Guardian 20, Buy 15, TON wallet form 6, start cards)"
cd $APP
ALL=""; for u in ferzan-launch-api ferzan-guardian ferzan-buy; do ALL="$ALL $u"; done
[ "$OLD" != "8c3a506dfd9f3c48638a8714e8d71411d5d4aebd" ] && for u in ferzan-trade ferzan-webapp ferzan-trade-api; do systemctl is-active -q $u && ALL="$ALL $u"; done
echo "restarting:$ALL"
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
health(){ systemctl restart $1; sleep 30; for u in $1; do if bad $u; then sleep 20; fi; if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; return 1; fi; done; return 0; }
health "$ALL" || { rollback; systemctl restart $ALL; fail "a service is unhealthy; everything rolled back"; }
for r in guardian buybot; do
  C=$(curl -s -m 8 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"initData":"x"}' http://127.0.0.1:8000/api/$r/groups)
  echo "$r route answers a fake sign-in with: $C (expect 401)"
  [ "$C" = 401 ] || { rollback; systemctl restart $ALL; fail "$r route did not answer as expected; rolled back"; }
done
BASE=$(grep '^MINI_APP_BASE_URL=' $ENV | cut -d= -f2-); BASE=${BASE:-https://launch.ferzaneco.com/miniapp}
NEED=""
for p in "guardian:GUARDIAN_MINIAPP_URL:ferzan-guardian" "buybot:BUYBOT_MINIAPP_URL:ferzan-buy"; do
  pg=${p%%:*}; rest=${p#*:}; var=${rest%%:*}; svc=${rest#*:}
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' $BASE/$pg.html)
  echo "page $BASE/$pg.html answers: $code"
  if [ "$code" = 200 ]; then
    if grep -q "^$var=" $ENV; then sed -i "s|^$var=.*|$var=$BASE/$pg.html|" $ENV; else [ -n "$(tail -c1 $ENV)" ] && echo >> $ENV; echo "$var=$BASE/$pg.html" >> $ENV; fi
    NEED="$NEED $svc"
  else echo "  -> the $pg app button stays hidden (page not served yet). Tell Claude."; fi
done
if [ -n "$NEED" ]; then echo "restarting for the app buttons:$NEED"; health "$NEED" || { rollback; systemctl restart $ALL; fail "a bot is unhealthy after enabling the app button; rolled back"; }; fi
echo "INSTALLED ${WANT:0:7}. Backups in $B."
