set -u; APP=/opt/ferzan/app; WANT=738f48c9e35b08c9d94a4e75c1d48a66904d1512
ENV=/opt/ferzan/.env; SRC="$APP/Ferzan-Ecosystem/Launch Bot/miniapp"; WEB=/var/www/ferzan-launch; B=/tmp/b149-backup
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
[ "$(git rev-parse HEAD)" = "$WANT" ] || fail "the server is on $(git rev-parse --short HEAD), not ${WANT:0:7}. Run b148 first. Nothing changed."
[ -d "$WEB" ] && [ -f "$WEB/app.html" ] || fail "$WEB or its app.html is missing. Nothing changed."
for f in guardian buybot; do [ -f "$SRC/$f.html" ] || fail "$SRC/$f.html missing. Nothing changed."; done
for k in GUARDIAN_TOKEN BUYBOT_TOKEN; do grep -q "^$k=.\+" $ENV || fail "$k is empty in $ENV"; done
for u in ferzan-guardian ferzan-buy; do systemctl is-active -q $u || fail "$u is not running"; done
nginx -t >/dev/null 2>&1 || fail "nginx config does not test clean right now; not touching anything"
BASE=$(grep '^MINI_APP_BASE_URL=' $ENV | cut -d= -f2-); BASE=${BASE:-https://launch.ferzaneco.com/miniapp}
echo "dry-run ok: copy guardian.html and buybot.html into $WEB (same owner and mode as app.html); nginx is not changed"
rm -rf $B; mkdir -p $B; cp -p $ENV $B/env
NEWF=""
rollback(){ for f in $NEWF; do rm -f "$WEB/$f"; done; for f in guardian.html buybot.html; do [ -f "$B/$f" ] && cp -p "$B/$f" "$WEB/$f"; done; cp -p $B/env $ENV; }
for f in guardian.html buybot.html; do
  if [ -f "$WEB/$f" ]; then cp -p "$WEB/$f" "$B/$f"; else NEWF="$NEWF $f"; fi
  cp "$SRC/$f" "$WEB/$f" && chown --reference="$WEB/app.html" "$WEB/$f" && chmod --reference="$WEB/app.html" "$WEB/$f" || { rollback; fail "could not copy $f; rolled back"; }
  cmp -s "$SRC/$f" "$WEB/$f" || { rollback; fail "$f copy does not match; rolled back"; }
done
OK=""
for p in "guardian:GUARDIAN_MINIAPP_URL:ferzan-guardian" "buybot:BUYBOT_MINIAPP_URL:ferzan-buy"; do
  pg=${p%%:*}; rest=${p#*:}; var=${rest%%:*}; svc=${rest#*:}
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' $BASE/$pg.html)
  echo "page $BASE/$pg.html answers: $code"
  [ "$code" = 200 ] || { rollback; fail "$pg page is not served ($code); rolled back. Tell Claude."; }
  if grep -q "^$var=" $ENV; then sed -i "s|^$var=.*|$var=$BASE/$pg.html|" $ENV; else [ -n "$(tail -c1 $ENV)" ] && echo >> $ENV; echo "$var=$BASE/$pg.html" >> $ENV; fi
  OK="$OK $svc"
done
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
echo "restarting:$OK"
systemctl restart $OK; sleep 30
for u in $OK; do
  if bad $u; then sleep 20; fi
  if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; rollback; systemctl restart $OK; fail "$u unhealthy; rolled back (pages removed, settings restored)"; fi
done
echo "INSTALLED: Guardian and Buy app pages are live and both bots now show their app button. Backups in $B."
echo "NOTE: nginx serves $WEB, a copy of the repo pages. Any later page change must be copied there too (future installers will do it)."
