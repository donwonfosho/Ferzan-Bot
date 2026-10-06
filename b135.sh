set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=448ad1e1ac1f68d6e03feb210f16630764f64061; KNOWN="5d8a9b962ac11f9ce8ded115e8f77b42bb66c39c"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not b134 (5d8a9b9). Install b131-b134 first";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && git cat-file -e "$WANT^{commit}" && git merge-base --is-ancestor $WANT origin/$BR || fail "reviewed commit is not on the branch"
BAD=$(git diff --name-only HEAD $WANT | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(x_poster|curve_indexer|api)\.py$|^Ferzan-Ecosystem/Launch Bot/miniapp/solana\.html$|^Ferzan-Ecosystem/Launch Bot/tests/test_batch_d27\.py$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok"
git merge -q --ff-only $WANT || fail "merge failed"
ALL=""; for u in ferzan-launch ferzan-launch-api ferzan-sol-trades; do systemctl cat $u >/dev/null 2>&1 && ALL="$ALL $u"; done
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
systemctl restart $ALL; sleep 20
for u in $ALL; do
  if bad $u; then sleep 20; fi
  if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; git reset -q --hard $OLD; systemctl restart $ALL; fail "$u unhealthy; rolled back"; fi
done
echo "INSTALLED ${WANT:0:7}: Solana coins now link to ferzan-factory.com instead of Jupiter (X posts, Telegram buttons, launch API, mini app)"
