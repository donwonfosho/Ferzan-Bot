set -u; APP=/opt/ferzan/app; BR=batch-tonconfirm-48; WANT=ae8faa2aced49100c3e4ec64e1f47972d9a1b13e; KNOWN="6d9c67bb0054cf295ae5fffdde2fab835326c08f"
fail(){ echo "ABORT: $*"; exit 1; }
cd $APP || fail "no $APP"
OLD=$(git rev-parse HEAD); case " $KNOWN " in *" $OLD "*) ;; *) fail "live is $(git rev-parse --short HEAD), not a reviewed commit";; esac
[ -z "$(git status --porcelain --untracked-files=no -- Ferzan-Ecosystem)" ] || fail "uncommitted changes"
git fetch -q origin +refs/heads/$BR:refs/remotes/origin/$BR && [ "$(git rev-parse origin/$BR)" = "$WANT" ] || fail "branch is not the reviewed commit"
BAD=$(git diff --name-only HEAD origin/$BR | grep -v -E '^Ferzan-Ecosystem/(Buy Bot/(buy_bot\.py|tests/test_batch_d18\.py)|Launch Bot/(api\.py|tests/test_batch_d19\.py)|Trade Desk/(bot\.py|db\.py|evm_signer\.py|webapp\.py|webapp/index\.html|tests/test_batch_d2[01]\.py))$')
[ -z "$BAD" ] || fail "unexpected files: $BAD"
echo "dry-run ok"
git merge -q --ff-only origin/$BR || fail "merge failed"
ALL=""; for u in ferzan-buy ferzan-launch ferzan-launch-api ferzan-trade ferzan-webapp ferzan-trade-api; do systemctl cat $u >/dev/null 2>&1 && ALL="$ALL $u"; done
bad(){ P=$(systemctl show -p MainPID --value $1); ! systemctl is-active -q $1 || [ "$P" = 0 ] || journalctl _PID=$P --no-pager 2>/dev/null | grep -q Traceback; }
systemctl restart $ALL; sleep 20
for u in $ALL; do
  if bad $u; then sleep 20; fi
  if bad $u; then journalctl -u $u -n 8 --no-pager | tail -7; git reset -q --hard $OLD; systemctl restart $ALL; fail "$u unhealthy; rolled back"; fi
done
echo "INSTALLED ${WANT:0:7}: Buy Bot /paid by order reference, Solana complete checks the mint, late EVM fills recorded, feed auto-buy toggle"
