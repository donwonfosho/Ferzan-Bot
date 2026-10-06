set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=5d8a9b962ac11f9ce8ded115e8f77b42bb66c39c; KNOWN="c1621f9b1c3d47a754e63279914200f59b25c532"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not b133 (c1621f9). Install b131, b132, b133 first";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(ferzan_promo|ferzan_media)\.py$|^Ferzan-Ecosystem/Launch Bot/promo_img/promo_(39|40|41|42|43|44)\.(jpg|mp4)$|^Ferzan-Ecosystem/Launch Bot/tests/(test_batch_d26|test_batch_d2)\.py$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok"
git merge -q --ff-only $WANT || fail "merge failed"
cd "Ferzan-Ecosystem/Launch Bot" || { git reset -q --hard $OLD; fail "no Launch Bot dir; rolled back"; }
PROMO_LIVE= python3 -c "
import ferzan_promo as p, ferzan_media as m
assert len(p.PROMOS)==44, len(p.PROMOS)
assert all(m.img('promo_%02d.jpg'%(i+1)) for i in range(44)), 'missing image'
assert m.vid('promo_44.mp4'), 'missing video'
print('promo set ok:', len(p.PROMOS), 'promos, video ok')" || { cd $APP; git reset -q --hard $OLD; fail "promo check failed; rolled back"; }
echo "INSTALLED ${WANT:0:7}: 5 new promo graphics + build-drop video in the rotation (promos 39-44), each with its own text and hashtags. No restart needed: the 10-minute promo timer picks it up on its next run."
