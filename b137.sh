set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=0f43b50b0759b3737618ed516d7e2595ce425a5d; KNOWN="715bcf076d6a86ddc164e816dd219e26d021ae0d"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not b136 (715bcf0). Install b136 first";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/ferzan_promo\.py$|^Ferzan-Ecosystem/Launch Bot/tests/test_batch_d26\.py$|^Ferzan-Ecosystem/Launch Bot/promo_img/promo_(4[5-9]|50)\.jpg$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok"
git merge -q --ff-only $WANT || fail "merge failed"
cd "Ferzan-Ecosystem/Launch Bot" || { git reset -q --hard $OLD; fail "no Launch Bot dir; rolled back"; }
PROMO_LIVE= python3 -c "
import ferzan_promo as p, ferzan_media as m
assert len(p.PROMOS)==50, len(p.PROMOS)
sk=p.skipped(p.LAUNCH_AT-60); assert len(sk)==19 and all(1<=n<=50 for n in sk), sk
assert set(range(45,51)).isdisjoint(sk)
assert all(m.img('promo_%02d.jpg' % n) for n in range(1,51))
assert all(p.next_promo(i)+1 not in sk for i in range(50))
print('promo set ok:', len(sk), 'skipped,', 50-len(sk), 'in rotation')" || { cd $APP; git reset -q --hard $OLD; fail "promo check failed; rolled back"; }
PROMO_LIVE= python3 -c "
import statefile as s, ferzan_promo as p
st=s.read_json(p.STATE, dict)
if st.get('promo_i',0) < 44:
    st['promo_i']=44; s.write_json(p.STATE, st); print('rotation starts with the new graphics')
else: print('rotation pointer left as it was:', st.get('promo_i'))" || echo "pointer step skipped; new graphics join the rotation in turn"
echo "INSTALLED ${WANT:0:7}: 6 new graphics in the promo rotation, 7 more older promos retired. No restart needed."
