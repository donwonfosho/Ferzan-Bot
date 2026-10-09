set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=eb2278daec282b83054f0aeccb2a41d6009124d1; KNOWN="715bcf076d6a86ddc164e816dd219e26d021ae0d 0f43b50b0759b3737618ed516d7e2595ce425a5d"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not b136 (715bcf0) or b137 (0f43b50). Install b136 first";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(ferzan_promo|x_poster)\.py$|^Ferzan-Ecosystem/Launch Bot/tests/test_batch_d26\.py$|^Ferzan-Ecosystem/Launch Bot/promo_img/promo_(4[5-9]|50)\.jpg$')
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
print('promo set ok:', 50-len(sk), 'in rotation; X is careful mode')" || { cd $APP; git reset -q --hard $OLD; fail "promo check failed; rolled back"; }
touch /opt/ferzan/x-paused && echo "X posting is PAUSED (file /opt/ferzan/x-paused). Resume later with: rm /opt/ferzan/x-paused"
PROMO_LIVE= python3 -c "
import statefile as s, ferzan_promo as p
st=s.read_json(p.STATE, dict)
if st.get('promo_i',0) < 44:
    st['promo_i']=44; s.write_json(p.STATE, st); print('rotation starts with the new graphics')
else: print('rotation pointer left as it was:', st.get('promo_i'))" || echo "pointer step skipped; new graphics join the rotation in turn"
ALL=""; for u in ferzan-curve-indexer; do systemctl cat $u >/dev/null 2>&1 && ALL="$ALL $u"; done
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
if [ -n "$ALL" ]; then
  systemctl restart $ALL; sleep 20
  for u in $ALL; do
    if bad $u; then sleep 20; fi
    if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; cd $APP; git reset -q --hard $OLD; systemctl restart $ALL; fail "$u unhealthy; rolled back"; fi
  done
fi
echo "INSTALLED ${WANT:0:7}: 6 new promo graphics, 7 older promos retired, careful X mode (X is paused until you remove /opt/ferzan/x-paused)."
