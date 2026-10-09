set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=6f266cc03442d45247bf55d8d96f791eed992d30; KNOWN="9d39f72ce1b0a0143f84f76bbbffde46ccbf6f7b"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not b142 (9d39f72). Install b142 first";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Trade Desk/(bot\.py|tests/test_welcome_custody\.py)$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok: only Trade Desk bot.py (+1 test) change"
git merge -q --ff-only $WANT || fail "merge failed"
cd "Ferzan-Ecosystem/Trade Desk" || { git reset -q --hard $OLD; fail "no Trade Desk dir; rolled back"; }
python3 -m py_compile bot.py || { cd $APP; git reset -q --hard $OLD; fail "Trade Desk bot.py does not compile; rolled back"; }
cd $APP
U=""; for u in $(systemctl list-units --type=service --state=active --no-legend --plain 2>/dev/null | awk '{print $1}'); do
  case $u in ferzan-trade.service) U="$U $u";; esac; done
echo "restarting:$U"
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
if [ -n "$U" ]; then
  systemctl restart $U; sleep 20
  for u in $U; do
    if bad $u; then sleep 15; fi
    if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; git reset -q --hard $OLD; systemctl restart $U; fail "$u unhealthy; rolled back"; fi
  done
else echo "ferzan-trade.service is not running; code updated, nothing restarted"; fi
echo "INSTALLED ${WANT:0:7}: Trade Desk first screen now states the custody line. Check it with a brand-new Telegram account if you can."
