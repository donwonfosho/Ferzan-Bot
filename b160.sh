set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=d9301561ac484f0995a3a4ac2b7420f6926702f2
KNOWN="ac0ced299900543897fd03232436037f377ded9e 585cf526c004b629369f1f3fccded2475f4b7904 cc25ce4d86488c3df5d473da06d7db95c7414918 b2efb4ac09310f3a6d1c630712568b108548e5cb 8a1bc5ca286f812fb7cb426fae22c854078b4253 9388ba9eceb6faf46e8fbbb12a216dad9e46a4cd 1646d747f120595fa097333b74af2e83793eb221 6af7973700f26ef07510103d3832679b25b0a42e"
TD="$APP/Ferzan-Ecosystem/Trade Desk"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD)
[ "$OLD" = "$WANT" ] && { echo "Already installed (${WANT:0:7}). Nothing to do."; exit 0; }
case " $KNOWN " in *" $OLD "*) ;; *) fail "the server is on $(git rev-parse --short HEAD), which this installer was not built for. Nothing changed. Send that version to Claude.";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Trade Desk/(bot\.py|webapp\.py|fundlinks\.py|webapp/index\.html|tests/test_fundlinks\.py)$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok: adds Add funds / Cash out (home screen, chain picker, Mini App card, /api/funds) plus buy-on-Base-then-bridge for chains with no card provider."
SV="ferzan-trade-api ferzan-webapp"
rollback(){ cd $APP; git reset -q --hard $OLD; for s in $SV; do systemctl restart $s 2>/dev/null; done; }
git merge -q --ff-only $WANT || fail "merge failed"
cd "$TD" || { rollback; fail "no Trade Desk dir; rolled back"; }
[ -x /opt/ferzan/.venv/bin/python ] && PY=/opt/ferzan/.venv/bin/python || PY=python3
$PY -m py_compile bot.py webapp.py fundlinks.py || { rollback; fail "does not compile; rolled back"; }
$PY -m unittest tests.test_fundlinks 2>&1 | tail -3 | grep -q '^OK' || { rollback; fail "tests failed; rolled back"; }
echo "tests ok"
for s in $SV; do systemctl restart $s || { rollback; fail "$s restart failed; rolled back"; }; done
sleep 8
for s in $SV; do systemctl is-active --quiet $s || { rollback; fail "$s not running; rolled back to ${OLD:0:7}"; }; done
echo "services running: $SV"
echo "MoonPay keys set (names only): $(grep -c '^MOONPAY_' /opt/ferzan/.env) of 2 lines (0 = unbranded but working)"
echo "INSTALLED ${WANT:0:7}. To undo: cd $APP && git reset --hard $OLD"
