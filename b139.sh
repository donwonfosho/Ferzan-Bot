set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=4f9bfa431eb07be5a19a8bb141ce4c2b82b2328a; KNOWN="715bcf076d6a86ddc164e816dd219e26d021ae0d 0f43b50b0759b3737618ed516d7e2595ce425a5d eb2278daec282b83054f0aeccb2a41d6009124d1"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not b136/b137/b138. Install b136 first";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
E1='^Ferzan-Ecosystem/Launch Bot/(ferzan_promo|x_poster|api|ferzan_flagship|launch_bot)\.py$|^Ferzan-Ecosystem/Launch Bot/tests/test_batch_d(26|27)\.py$|^Ferzan-Ecosystem/Launch Bot/promo_img/promo_(4[5-9]|50)\.jpg$'
E2='^Ferzan-Ecosystem/Launch Bot/miniapp/(app|evm|solana)\.html$|^Ferzan-Ecosystem/Trade Desk/bot\.py$|^Ferzan-Ecosystem/Guardian Bot/guardian_bot\.py$'
BAD=$(git diff --name-only HEAD $WANT | grep -v -E "$E1|$E2")
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok"
git merge -q --ff-only $WANT || fail "merge failed"
cd "Ferzan-Ecosystem/Launch Bot" || { git reset -q --hard $OLD; fail "no Launch Bot dir; rolled back"; }
PROMO_LIVE= python3 -c "
import ferzan_promo as p, ferzan_media as m, x_poster as x
assert len(p.PROMOS)==50, len(p.PROMOS)
sk=p.skipped(p.LAUNCH_AT-60); assert len(sk)==19, sk
assert set(range(45,51)).isdisjoint(sk)
assert all(m.img('promo_%02d.jpg' % n) for n in range(1,51))
assert x.event_cap()<=4 and callable(x.paused)
assert '@ferzanfactory' in p.COUNTDOWN_FACTS[12*3600]
print('promo set ok:', 50-len(sk), 'in rotation; X is careful mode; new X handle in place')" || { cd $APP; git reset -q --hard $OLD; fail "promo check failed; rolled back"; }
for f in "api.py" "launch_bot.py" "ferzan_flagship.py" "../Trade Desk/bot.py" "../Guardian Bot/guardian_bot.py"; do python3 -m py_compile "$f" || { cd $APP; git reset -q --hard $OLD; fail "$f does not compile; rolled back"; }; done
touch /opt/ferzan/x-paused && echo "X posting is PAUSED (file /opt/ferzan/x-paused). Resume later with: rm /opt/ferzan/x-paused"
PROMO_LIVE= python3 -c "
import statefile as s, ferzan_promo as p
st=s.read_json(p.STATE, dict)
if st.get('promo_i',0) < 44:
    st['promo_i']=44; s.write_json(p.STATE, st); print('rotation starts with the new graphics')
else: print('rotation pointer left as it was:', st.get('promo_i'))" || echo "pointer step skipped; new graphics join the rotation in turn"
cd $APP
ALL=""; for u in $(systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}'); do
  systemctl is-active -q $u && systemctl cat $u 2>/dev/null | grep -q -E 'Ferzan-Ecosystem/(Launch Bot|Trade Desk|Guardian Bot)' && ALL="$ALL $u"; done
echo "restarting:$ALL"
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
if [ -n "$ALL" ]; then
  systemctl restart $ALL; sleep 25
  for u in $ALL; do
    if bad $u; then sleep 20; fi
    if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; git reset -q --hard $OLD; systemctl restart $ALL; fail "$u unhealthy; rolled back"; fi
  done
fi
[ -f /opt/ferzan/.env ] && echo "FERZAN_X_URL set in .env: $(grep -c '^FERZAN_X_URL=' /opt/ferzan/.env) (0 is right; if 1, change it to https://x.com/ferzanfactory)"
echo "INSTALLED ${WANT:0:7}: new X handle @ferzanfactory everywhere in the bots; 6 new promo graphics; careful X mode (X paused until you remove /opt/ferzan/x-paused)."
