set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=b1fc2416d852e47c815bb211867660e325050503; KNOWN="6f266cc03442d45247bf55d8d96f791eed992d30"; DATE="2026-11-13T21:00:00Z"
ENV=/opt/ferzan/.env; B=/tmp/b144-backup; UNITS=""
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD)
[ "$OLD" = "$WANT" ] && { echo "Already installed (${WANT:0:7}). Nothing to do."; exit 0; }
case " $KNOWN " in *" $OLD "*) ;; *) fail "the server is on $(git rev-parse --short HEAD), which this installer was not built for. Nothing changed. Send that version to Claude.";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(api|ferzan_flagship|ferzan_flywheel|ferzan_promo|ferzan_refill|ferzan_watchdog|ferzan_when|launch_bot|launch_day)\.py$|^Ferzan-Ecosystem/Launch Bot/scripts/make_promo_images\.py$|^Ferzan-Ecosystem/Launch Bot/promo_img/countdown_[0-9a-z]+\.jpg$|^Ferzan-Ecosystem/Launch Bot/tests/test_(batch_d26|batch_d29|launch_day|ton_curve_tg)\.py$|^Ferzan-Ecosystem/Trade Desk/(bot|trust)\.py$|^Ferzan-Ecosystem/Trade Desk/tests/test_launch_date\.py$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
[ -f $ENV ] || fail "no $ENV"
echo "dry-run ok: code, countdown graphics and tests only"
rollback(){ cd $APP; git reset -q --hard $OLD; [ -f $B/env ] && cp -p $B/env $ENV; for u in $UNITS; do [ -f $B/$(basename $u) ] && cp -p $B/$(basename $u) $u; done; systemctl daemon-reload; }
rm -rf $B; mkdir -p $B; cp -p $ENV $B/env
git merge -q --ff-only $WANT || fail "merge failed"
# 1. the launch date, in the shared settings file (other settings are never printed)
if grep -q '^FERZAN_LAUNCH_AT=' $ENV; then sed -i "s|^FERZAN_LAUNCH_AT=.*|FERZAN_LAUNCH_AT=$DATE|" $ENV; else [ -n "$(tail -c1 $ENV)" ] && echo >> $ENV; echo "FERZAN_LAUNCH_AT=$DATE" >> $ENV; fi
grep -c '^FERZAN_LAUNCH_AT=' $ENV | grep -q '^1$' || { rollback; fail "could not set the date; rolled back"; }
echo "launch date set: $(grep '^FERZAN_LAUNCH_AT=' $ENV)"
# 2. code checks against the real server files
cd "$APP/Ferzan-Ecosystem/Launch Bot" || { rollback; fail "no Launch Bot dir; rolled back"; }
PROMO_LIVE= python3 -c "
import ferzan_when as w, ferzan_promo as p, ferzan_flagship as f
assert w.launch_at()==1794603600, w.launch_at()
assert p.LAUNCH_AT==f.LAUNCH_AT==1794603600
assert 49 in p.skipped(0)
assert 'Friday Nov 13' in w.label_et()
print('date check ok:', w.label_et())" || { rollback; fail "date check failed; rolled back"; }
for f in api.py launch_bot.py ferzan_flagship.py ferzan_flywheel.py ferzan_refill.py ferzan_watchdog.py launch_day.py "../Trade Desk/bot.py" "../Trade Desk/trust.py"; do python3 -m py_compile "$f" || { rollback; fail "$f does not compile; rolled back"; }; done
# 3. the two launch timers: moved to the new date. The real launch timer stays OFF.
for t in ferzan-flagship.timer ferzan-flagship-rehearsal.timer; do
  U=$(systemctl show -p FragmentPath --value $t 2>/dev/null)
  if [ -n "$U" ] && [ -f "$U" ] && [ "$(grep -c '^OnCalendar=' $U)" = 1 ]; then
    cp -p $U $B/$(basename $U); UNITS="$UNITS $U"
    case $t in ferzan-flagship.timer) NEW="2026-11-13 21:00:00 UTC";; *) NEW="2026-11-12 21:00:00 UTC";; esac
    echo "$t: $(grep '^OnCalendar=' $U)  ->  OnCalendar=$NEW"
    sed -i "s|^OnCalendar=.*|OnCalendar=$NEW|" $U
  else echo "$t: not changed (unit not found or unusual); tell Claude"; fi
done
systemctl daemon-reload
echo "real launch timer enabled? $(systemctl is-enabled ferzan-flagship.timer 2>&1) (it must say disabled until you decide)"
# 4. restart what reads the date at start-up
cd $APP; ALL=""; for u in ferzan-launch ferzan-launch-api ferzan-trade ferzan-webapp ferzan-trade-api; do systemctl is-active -q $u && ALL="$ALL $u"; done
echo "restarting:$ALL"
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
if [ -n "$ALL" ]; then
  systemctl restart $ALL; sleep 30
  for u in $ALL; do
    if bad $u; then sleep 20; fi
    if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; rollback; systemctl restart $ALL; fail "$u unhealthy; everything rolled back"; fi
  done
fi
echo "site data now says: $(curl -s -m 8 http://127.0.0.1:8000/api/pulse | python3 -c 'import sys,json; print(json.load(sys.stdin)["ferzan"]["launch_at"])' 2>/dev/null)  (expect 1794603600)"
echo "INSTALLED ${WANT:0:7}: FERZAN launch is Friday Nov 13, 4:00 PM Eastern. Backups are in $B. The real launch timer is still OFF."
