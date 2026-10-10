set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=f7d4c82897eff83868e5fe0e82b35dd037677818
KNOWN="5b68c15570d136a644d14cbcc2900bd2f3667707"
LB="$APP/Ferzan-Ecosystem/Launch Bot"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD)
[ "$OLD" = "$WANT" ] && { echo "Already installed (${WANT:0:7}). Nothing to do."; exit 0; }
case " $KNOWN " in *" $OLD "*) ;; *) fail "the server is on $(git rev-parse --short HEAD), which this installer was not built for. Nothing changed. Send that version to Claude.";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(api|curve_indexer|x_poster)\.py$|^Ferzan-Ecosystem/Launch Bot/tests/test_(batch_d27|site_trade_links)\.py$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
ALL=""; for u in ferzan-launch ferzan-launch-api ferzan-curve-indexer; do systemctl cat $u >/dev/null 2>&1 && ALL="$ALL $u"; done
[ -n "$ALL" ] || fail "no Ferzan launch services found"
for u in $ALL; do systemctl is-active -q $u || fail "$u is not running"; done
echo "dry-run ok: trade links in channel posts, launch cards and X posts go to ferzan-factory.com (restarts:$ALL)"
rollback(){ cd $APP; git reset -q --hard $OLD; }
git merge -q --ff-only $WANT || fail "merge failed"
cd "$LB" || { rollback; fail "no Launch Bot dir; rolled back"; }
for f in api.py curve_indexer.py x_poster.py; do python3 -m py_compile "$f" || { rollback; fail "$f does not compile; rolled back"; }; done
[ -x /opt/ferzan/.venv/bin/python ] && PY=/opt/ferzan/.venv/bin/python || PY=python3
t(){ $PY -m unittest $1 2>&1 | tail -4 | grep -q '^OK' || { $PY -m unittest $1 2>&1 | tail -12; rollback; fail "tests failed ($1); rolled back"; }; }
t "tests.test_site_trade_links tests.test_batch_d27 tests.test_status_check"
echo "tests ok"
cd $APP
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
systemctl restart $ALL; sleep 25
for u in $ALL; do
  if bad $u; then sleep 20; fi
  if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; rollback; systemctl restart $ALL; fail "$u unhealthy; everything rolled back"; fi
done
echo "services running:$ALL"
R=$(curl -s -m 12 http://127.0.0.1:8000/api/status); echo "status answer: $R"
echo "$R" | grep -q '"status"' || { rollback; systemctl restart $ALL; fail "/api/status did not answer; rolled back"; }
echo "INSTALLED ${WANT:0:7}. To undo: cd $APP && git reset --hard $OLD && systemctl restart$ALL"
