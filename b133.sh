set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=c1621f9b1c3d47a754e63279914200f59b25c532; KNOWN="1ddb99eed861ea33ed9b96d758e2ef3e223a2d23"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not a reviewed commit";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && [ "$(git rev-parse origin/$BR)" = "$WANT" ] || fail "branch is not the reviewed commit"
BAD=$(git diff --name-only HEAD origin/$BR | grep -v -E '^Ferzan-Ecosystem/Launch Bot/(api|ferzan_flagship|launch_bot|launch_reserved)\.py$|^Ferzan-Ecosystem/Launch Bot/tests/(test_batch_d25|test_curve_model)\.py$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok"
git merge -q --ff-only origin/$BR || fail "merge failed"
ALL=""; for u in ferzan-launch ferzan-launch-api; do systemctl cat $u >/dev/null 2>&1 && ALL="$ALL $u"; done
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
systemctl restart $ALL; sleep 20
for u in $ALL; do
  if bad $u; then sleep 20; fi
  if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; git reset -q --hard $OLD; systemctl restart $ALL; fail "$u unhealthy; rolled back"; fi
done
echo "INSTALLED ${WANT:0:7}: Ferzan lookalike names and major tickers blocked from launching, FERZAN logo stored permanently"
