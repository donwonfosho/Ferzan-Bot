// Builds the STON.fi swap message with the official SDK (works for v1 and v2 pools). It signs and sends NOTHING:
// it prints {to, value, body_b64, router_version, min_ask_units, ask_units} and the Trade Bot signs it.
//   node ton_swap_params.mjs '{"dir":"sell"|"buy","wallet":"<user TON address>","jetton":"<EQ..>","units":"<base units>","slip":"0.10"}'
import { TonClient, Address } from "@ton/ton";
import { StonApiClient } from "@ston-fi/api";
import { dexFactory } from "@ston-fi/sdk";

const TON = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c";  // the API rejects the word "ton"
const out = (o) => { console.log(JSON.stringify(o)); process.exit(0); };
try {
  const a = JSON.parse(process.argv[2] || "{}");
  if (!["sell", "buy"].includes(a.dir) || !a.wallet || !a.jetton || !a.units) out({ ok: false, error: "bad arguments" });
  const client = new TonClient({ endpoint: process.env.TONCENTER_RPC || "https://toncenter.com/api/v2/jsonRPC", apiKey: process.env.TONCENTER_API_KEY || undefined });
  const api = new StonApiClient();
  const sim = await api.simulateSwap({
    offerAddress: a.dir === "sell" ? a.jetton : TON,
    askAddress: a.dir === "sell" ? TON : a.jetton,
    offerUnits: String(a.units),
    slippageTolerance: String(a.slip || "0.10"),
  });
  const contracts = dexFactory(sim.router);
  const router = client.open(contracts.Router.create(sim.router.address));
  const proxyTon = contracts.pTON.create(sim.router.ptonMasterAddress);
  const p = a.dir === "sell"
    ? await router.getSwapJettonToTonTxParams({ userWalletAddress: a.wallet, offerJettonAddress: sim.offerAddress, offerAmount: sim.offerUnits, minAskAmount: sim.minAskUnits, proxyTon })
    : await router.getSwapTonToJettonTxParams({ userWalletAddress: a.wallet, askJettonAddress: sim.askAddress, offerAmount: sim.offerUnits, minAskAmount: sim.minAskUnits, proxyTon });
  out({ ok: true, to: p.to.toString(), value: p.value.toString(), body_b64: p.body.toBoc().toString("base64"),
        router_version: `${sim.router.majorVersion}.${sim.router.minorVersion}`, min_ask_units: String(sim.minAskUnits), ask_units: String(sim.askUnits) });
} catch (e) { out({ ok: false, error: String((e && e.message) || e).slice(0, 240) }); }
