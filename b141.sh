set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=5856c22185ee11b9ad739e25c3ba6b9670c80e6b; KNOWN="ece2bf8bdefb16859d2cbc9198a9568dff93c28f"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not b140 (ece2bf8). Install b140 first";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Guardian Bot/(guardian_bot\.py|tests/test_start_card\.py)$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok: only guardian_bot.py (+1 test) change"
git merge -q --ff-only $WANT || fail "merge failed"
cd "Ferzan-Ecosystem/Guardian Bot" || { git reset -q --hard $OLD; fail "no Guardian Bot dir; rolled back"; }
python3 -m py_compile guardian_bot.py || { cd $APP; git reset -q --hard $OLD; fail "guardian_bot.py does not compile; rolled back"; }
cd $APP
U=""; for u in $(systemctl list-units --type=service --state=active --no-legend --plain 2>/dev/null | awk '{print $1}'); do
  systemctl cat $u 2>/dev/null | grep -q 'guardian_bot\.py' && U="$U $u"; done
echo "restarting:$U"
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
if [ -n "$U" ]; then
  systemctl restart $U; sleep 20
  for u in $U; do
    if bad $u; then sleep 15; fi
    if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; git reset -q --hard $OLD; systemctl restart $U; fail "$u unhealthy; rolled back"; fi
  done
else echo "no running guardian service found; code updated, nothing restarted"; fi
echo "INSTALLED ${WANT:0:7}: Guardian /start card updated (Buy button fixed). Send /start to Ferzan Guardian in a private chat."
