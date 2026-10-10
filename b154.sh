set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=cc25ce4d86488c3df5d473da06d7db95c7414918
KNOWN="ac0ced299900543897fd03232436037f377ded9e 585cf526c004b629369f1f3fccded2475f4b7904"
LB="$APP/Ferzan-Ecosystem/Launch Bot"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD)
[ "$OLD" = "$WANT" ] && { echo "Already installed (${WANT:0:7}). Nothing to do."; exit 0; }
case " $KNOWN " in *" $OLD "*) ;; *) fail "the server is on $(git rev-parse --short HEAD), which this installer was not built for. Nothing changed. Send that version to Claude.";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(ferzan_promo\.py|tests/test_batch_d26\.py|promo_img/promo_(5[1-9]|6[0-9]|7[0-7])\.jpg)$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok: adds 17 new promo graphics + copy (51-77) and retires the old 50. Promos stay in preview unless PROMO_LIVE=1."
rollback(){ cd $APP; git reset -q --hard $OLD; }
git merge -q --ff-only $WANT || fail "merge failed"
cd "$LB" || { rollback; fail "no Launch Bot dir; rolled back"; }
python3 -m py_compile ferzan_promo.py || { rollback; fail "does not compile; rolled back"; }
for n in $(seq 51 77); do [ -s promo_img/promo_$n.jpg ] || { rollback; fail "image $n missing; rolled back"; }; done
[ -x /opt/ferzan/.venv/bin/python ] && PY=/opt/ferzan/.venv/bin/python || PY=python3
$PY -m unittest tests.test_batch_d26 2>&1 | tail -3 | grep -q '^OK' || { rollback; fail "tests failed; rolled back"; }
echo "tests ok"
echo "PROMO_SKIP set in env file: $(grep -c '^PROMO_SKIP=' /opt/ferzan/.env) (0 = the new default applies; if 1, tell Claude)"
echo "PROMO_LIVE: $(grep '^PROMO_LIVE=' /opt/ferzan/.env | sed 's/=.*/=<set>/' )"
echo "INSTALLED ${WANT:0:7}. To undo: cd $APP && git reset --hard $OLD"
