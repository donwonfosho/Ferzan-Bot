set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=ece2bf8bdefb16859d2cbc9198a9568dff93c28f; KNOWN="4f9bfa431eb07be5a19a8bb141ce4c2b82b2328a"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not b139 (4f9bfa4). Install b139 first";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(launch_bot\.py|tests/test_batch_d28\.py)$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok: only launch_bot.py (+1 test) change"
git merge -q --ff-only $WANT || fail "merge failed"
cd "Ferzan-Ecosystem/Launch Bot" || { git reset -q --hard $OLD; fail "no Launch Bot dir; rolled back"; }
python3 -m py_compile launch_bot.py || { cd $APP; git reset -q --hard $OLD; fail "launch_bot.py does not compile; rolled back"; }
cd $APP
U=""; for u in $(systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}'); do
  systemctl is-active -q $u && systemctl cat $u 2>/dev/null | grep -q 'launch_bot\.py' && U="$U $u"; done
echo "restarting:$U"
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
if [ -n "$U" ]; then
  systemctl restart $U; sleep 20
  for u in $U; do
    if bad $u; then sleep 15; fi
    if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; git reset -q --hard $OLD; systemctl restart $U; fail "$u unhealthy; rolled back"; fi
  done
else echo "no running launch bot service found; code updated, nothing restarted"; fi
echo "INSTALLED ${WANT:0:7}: new /start text. Now open the Launch Bot and send /start."
