set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=715bcf076d6a86ddc164e816dd219e26d021ae0d; KNOWN="448ad1e1ac1f68d6e03feb210f16630764f64061"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not b135 (448ad1e). Install b131-b135 first";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/ferzan_promo\.py$|^Ferzan-Ecosystem/Launch Bot/tests/test_batch_d26\.py$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok"
git merge -q --ff-only $WANT || fail "merge failed"
cd "Ferzan-Ecosystem/Launch Bot" || { git reset -q --hard $OLD; fail "no Launch Bot dir; rolled back"; }
PROMO_LIVE= python3 -c "
import ferzan_promo as p
assert len(p.PROMOS)==44, len(p.PROMOS)
sk=p.skipped(); assert sk and all(1<=n<=44 for n in sk), sk
assert all(p.next_promo(i)+1 not in sk for i in range(44))
print('promo skip ok:', len(sk), 'skipped,', 44-len(sk), 'in rotation')" || { cd $APP; git reset -q --hard $OLD; fail "promo check failed; rolled back"; }
echo "INSTALLED ${WANT:0:7}: older duplicate promos are skipped; the 6 new ones and the best of the rest stay in rotation. No restart needed."
