set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=1ddb99eed861ea33ed9b96d758e2ef3e223a2d23; KNOWN="ae8faa2aced49100c3e4ec64e1f47972d9a1b13e"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not a reviewed commit";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && [ "$(git rev-parse origin/$BR)" = "$WANT" ] || fail "branch is not the reviewed commit"
BAD=$(git diff --name-only HEAD origin/$BR | grep -v -E '^Ferzan-Ecosystem/(Guardian Bot/(guardian_bot\.py|tests/test_batch_d24\.py)|Liquidity Bot/(liq/basestonk_mm\.py|tests/test_batch_d23\.py)|Trade Desk/(bridge|evm_signer|hood|ton_signer)\.py|Trade Desk/tests/test_batch_d22\.py)$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok"
git merge -q --ff-only origin/$BR || fail "merge failed"
ALL=""; for u in ferzan-guardian ferzan-liq ferzan-trade ferzan-webapp ferzan-trade-api; do systemctl cat $u >/dev/null 2>&1 && ALL="$ALL $u"; done
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
systemctl restart $ALL; sleep 20
for u in $ALL; do
  if bad $u; then sleep 20; fi
  if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; git reset -q --hard $OLD; systemctl restart $ALL; fail "$u unhealthy; rolled back"; fi
done
echo "INSTALLED ${WANT:0:7}: swaps and approvals pinned to 0x AllowanceHolder, bridge/Hood checked, MM approvals pinned, TON seqno lag, shadowban protects admins"
