// Ferzan TON keeper.  node ton_keeper.mjs <plan|run> <curve> [--send]
//   plan : read-only. Shows the keeper wallet, balances, the curve state and what `run` would do. Never sends.
//   run  : does the graduation for one filled curve, one stage at a time, skipping stages already done:
//            1 graduate  - call graduate() on the curve; the raised TON and the remaining coins arrive here
//            2 pool      - open the STON.fi pool with ALL of it (TON + coins)
//            3 lock      - send every LP token the keeper received to the null address (0:000..0), so the liquidity can never be pulled.
//                          NOT a burn: on STON.fi burning LP tokens WITHDRAWS the liquidity, it does not lock it.
//          Without --send it only prints what it would do.  The key never leaves this process.
// Key file: /opt/ferzan/dbc-keys/ton-keeper.json = {"mnemonic": ["word", ... 24]}; nothing here prints it.
// Prints one JSON line at the end: {ok, stage, ...}.
import fs from "node:fs";
import path from "node:path";
import { TonClient, WalletContractV4, internal, beginCell, Address, SendMode, fromNano, toNano } from "@ton/ton";
import { mnemonicToPrivateKey } from "@ton/crypto";

const KEY_FILE = process.env.TON_KEEPER_KEY_FILE || "/opt/ferzan/dbc-keys/ton-keeper.json";
const STATE_DIR = process.env.TON_KEEPER_STATE_DIR || "/opt/ferzan/ton-keeper/state";
const TESTNET = (process.env.TON_NETWORK || "").toLowerCase() === "testnet";
const ENDPOINT = process.env.TONCENTER_RPC || (TESTNET ? "https://testnet.toncenter.com/api/v2/jsonRPC" : "https://toncenter.com/api/v2/jsonRPC");
const OP_GRADUATE = 0x67726164;
const OP_BURN = 0x595f07bc;
const GRAD_ATTACH = toNano("0.3");
const NEED_TON_GRADUATE = toNano(process.env.TON_KEEPER_MIN_TON || "1");
const RESERVE_TON = toNano("3");           // stays in the keeper for fees; the rest goes into the pool
const TON_ADDRESS = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c";

const out = (o) => { console.log(JSON.stringify(o, (_, v) => (typeof v === "bigint" ? v.toString() : v))); process.exit(0); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const client = new TonClient({ endpoint: ENDPOINT, apiKey: process.env.TONCENTER_API_KEY || undefined });

async function loadWallet() {
  const j = JSON.parse(fs.readFileSync(KEY_FILE, "utf8"));
  const kp = await mnemonicToPrivateKey(j.mnemonic);
  const wallet = client.open(WalletContractV4.create({ workchain: 0, publicKey: kp.publicKey }));
  return { wallet, kp };
}
const same = (a, b) => Address.parse(a).equals(Address.parse(b));

async function curveState(curve) {
  const r = (await client.runMethod(Address.parse(curve), "get_curve")).stack;
  const n = () => r.readBigNumber();
  const [grad, supply, real, sold, start, complete, graduated] = [n(), n(), n(), n(), n(), n(), n()];
  return { grad, supply, real, sold, start, complete: complete !== 0n, graduated: graduated !== 0n };
}
async function getAddr(addr, method, args = []) {
  return (await client.runMethod(Address.parse(addr), method, args)).stack.readAddress();
}
async function jettonBalance(master, owner) {
  const w = await getAddr(master, "get_wallet_address", [{ type: "slice", cell: beginCell().storeAddress(Address.parse(owner)).endCell() }]);
  try { return { wallet: w, balance: (await client.runMethod(w, "get_wallet_data")).stack.readBigNumber() }; }
  catch { return { wallet: w, balance: 0n }; }   // wallet not deployed yet = zero coins
}
const stateFile = (curve) => path.join(STATE_DIR, curve.replace(/[^A-Za-z0-9_-]/g, "_") + ".json");
const readDone = (curve) => { try { return JSON.parse(fs.readFileSync(stateFile(curve), "utf8")); } catch { return {}; } };
const markDone = (curve, k, v = true) => { fs.mkdirSync(STATE_DIR, { recursive: true }); const d = readDone(curve); d[k] = v; fs.writeFileSync(stateFile(curve), JSON.stringify(d)); };

async function waitFor(fn, what, tries = 40, gap = 5000) {
  for (let i = 0; i < tries; i++) { try { const v = await fn(); if (v) return v; } catch { /* keep polling */ } await sleep(gap); }
  throw new Error("timed out waiting for " + what);
}
async function sendMessages(wallet, kp, msgs) {
  const seqno = await wallet.getSeqno();
  await wallet.sendTransfer({ seqno, secretKey: kp.secretKey, sendMode: SendMode.PAY_GAS_SEPARATELY + SendMode.IGNORE_ERRORS,
    messages: msgs.map((m) => internal({ to: m.to, value: m.value, body: m.body, bounce: true })) });
  await waitFor(async () => (await wallet.getSeqno()) > seqno, "the keeper transaction to land");
}

async function main() {
  const [mode, curve] = [process.argv[2], process.argv[3]];
  const send = process.argv.includes("--send");
  if (!["plan", "run"].includes(mode) || !curve) out({ ok: false, error: "usage: ton_keeper.mjs <plan|run> <curve> [--send]" });
  if (!fs.existsSync(KEY_FILE)) out({ ok: false, error: "no keeper key file" });
  const { wallet, kp } = await loadWallet();
  const kaddr = wallet.address.toString({ bounceable: false, testOnly: TESTNET });
  if (process.env.TON_KEEPER_ADDRESS && !same(process.env.TON_KEEPER_ADDRESS, kaddr))
    out({ ok: false, error: "the keeper key does not match TON_KEEPER_ADDRESS (the curve pays that address)", address: kaddr });
  const cs = await curveState(curve);
  if (!same((await getAddr(curve, "get_keeper")).toString(), kaddr)) out({ ok: false, error: "this curve does not graduate to our keeper" });
  const minter = (await getAddr(curve, "get_minter")).toString();
  const balance = await client.getBalance(wallet.address);
  const done = readDone(curve);
  const view = { address: kaddr, balance_ton: Number(fromNano(balance)), curve_complete: cs.complete, curve_graduated: cs.graduated, done };
  if (mode === "plan") out({ ok: true, plan: true, ...view,
    next: !cs.complete && !cs.graduated ? "curve not full yet" : !cs.graduated ? "graduate, then pool, then burn" : !done.burned ? "pool + burn" : "nothing" });

  // ---- 1. graduate
  if (!cs.graduated) {
    if (!cs.complete) out({ ok: false, error: "not full yet" });
    if (balance < NEED_TON_GRADUATE) out({ ok: false, error: "low_balance", address: kaddr, balance_ton: view.balance_ton, need_ton: Number(fromNano(NEED_TON_GRADUATE)) });
    if (!send) out({ ok: true, dry_run: true, would: "graduate " + curve, ...view });
    const body = beginCell().storeUint(OP_GRADUATE, 32).storeUint(Math.floor(Date.now() / 1000), 64).endCell();
    await sendMessages(wallet, kp, [{ to: Address.parse(curve), value: GRAD_ATTACH, body }]);
    await waitFor(async () => (await curveState(curve)).graduated, "the curve to report graduated", 40, 5000);
    markDone(curve, "graduated");
  }
  if (done.burned) out({ ok: true, stage: "done", ...view });

  // ---- 2. pool (only if we have not opened it yet)
  const { StonApiClient } = await import("@ston-fi/api");
  const { dexFactory } = await import("@ston-fi/sdk");
  const api = new StonApiClient();
  const bal = await client.getBalance(wallet.address);
  const jb = await jettonBalance(minter, kaddr);
  if (!readDone(curve).pooled) {
    // wait for the raised TON and the coins to arrive from the curve
    const arrived = await waitFor(async () => {
      const b = await client.getBalance(wallet.address); const j = await jettonBalance(minter, kaddr);
      return b > RESERVE_TON && j.balance > 0n ? { b, j } : null; }, "the curve's TON and coins to reach the keeper", 30, 6000).catch(() => null);
    if (!arrived) out({ ok: false, stage: "pool", error: "graduation funds have not reached the keeper yet" });
    let tonIn = arrived.b - RESERVE_TON; if (cs.grad > 0n && tonIn > cs.grad) tonIn = cs.grad;  // never pool more than the curve raised (keeper's own float stays put)
    const coinsIn = arrived.j.balance;
    const sim = await api.simulateLiquidityProvision({
      tokenA: TON_ADDRESS, tokenB: minter, provisionType: "Initial", slippageTolerance: "0.05",
      walletAddress: kaddr, tokenAUnits: tonIn.toString(), tokenBUnits: coinsIn.toString() });
    const contracts = dexFactory(sim.router);
    const router = client.open(contracts.Router.create(sim.router.address));
    const pton = contracts.pTON.create(sim.router.ptonMasterAddress);
    const tonTx = await router.getProvideLiquidityTonTxParams({ userWalletAddress: kaddr, minLpOut: "1", sendTokenAddress: TON_ADDRESS,
      sendAmount: tonIn.toString(), otherTokenAddress: minter, proxyTon: pton });
    const jetTx = await router.getProvideLiquidityJettonTxParams({ userWalletAddress: kaddr, minLpOut: "1", sendTokenAddress: minter,
      sendAmount: coinsIn.toString(), otherTokenAddress: pton.address.toString(), routerAddress: sim.router.address });
    if (!send) out({ ok: true, dry_run: true, would: "open the STON.fi pool", ton_in: Number(fromNano(tonIn)), coins_in: coinsIn.toString(), router: sim.router.address.toString() });
    await sendMessages(wallet, kp, [tonTx, jetTx].map((t) => ({ to: t.to, value: t.value, body: t.body })));
    markDone(curve, "pooled");
    markDone(curve, "router", sim.router.address.toString());
  }

  // ---- 3. lock every LP token we received by sending it to the null address.
  // Do NOT use the jetton burn op here: STON.fi treats burning LP as "remove liquidity" and pays the pool out.
  const NULL_ADDR = Address.parseRaw("0:" + "0".repeat(64));
  const findLp = async () => {
    const r = await fetch("https://toncenter.com/api/v3/jetton/wallets?limit=100&offset=0&owner_address=" + encodeURIComponent(kaddr),
      { headers: process.env.TONCENTER_API_KEY ? { "X-API-Key": process.env.TONCENTER_API_KEY } : {} });
    if (!r.ok) throw new Error("toncenter jetton list " + r.status);
    const list = (await r.json()).jetton_wallets || [];
    for (const jw of list) {
      if (BigInt(jw.balance || "0") <= 0n) continue;
      if (same(jw.jetton, minter)) continue;            // the coin itself, not an LP token
      let pool; try { pool = await api.getPool(Address.parse(jw.jetton).toString()); } catch { continue; }
      const strs = []; (function walk(o) { if (typeof o === "string") strs.push(o); else if (o && typeof o === "object") Object.values(o).forEach(walk); })(pool);
      const hasCoin = strs.some((x) => { try { return Address.parse(x).equals(Address.parse(minter)); } catch { return false; } });
      if (hasCoin) return { pool: Address.parse(jw.jetton).toString(), wallet: Address.parse(jw.address), b: BigInt(jw.balance) };
    }
    return null;
  };
  const lpBalance = async (w) => (await client.runMethod(w, "get_wallet_data")).stack.readBigNumber();
  const lp = await waitFor(findLp, "the LP tokens to show up in the keeper wallet", 40, 6000).catch(() => null);
  if (!lp) out({ ok: false, stage: "lock", error: "no LP tokens for this coin found in the keeper wallet" });
  if (!send) out({ ok: true, dry_run: true, would: "lock " + lp.b.toString() + " LP (send to the null address)", pool: lp.pool, lp_wallet: lp.wallet.toString() });
  const xfer = beginCell().storeUint(0x0f8a7ea5, 32).storeUint(Math.floor(Date.now() / 1000), 64).storeCoins(lp.b)
    .storeAddress(NULL_ADDR).storeAddress(wallet.address).storeBit(0).storeCoins(0).storeBit(0).endCell();
  await sendMessages(wallet, kp, [{ to: lp.wallet, value: toNano("0.1"), body: xfer }]);
  await waitFor(async () => (await lpBalance(lp.wallet).catch(() => lp.b)) === 0n, "the LP transfer to land", 30, 5000);
  markDone(curve, "burned");
  out({ ok: true, stage: "done", pool: lp.pool, lp_locked: lp.b.toString(), locked_to: "null address (0:000...0)",
        proof: (TESTNET ? "https://testnet.tonviewer.com/" : "https://tonviewer.com/") + lp.pool });
}
main().catch((e) => out({ ok: false, error: String(e && e.message || e).slice(0, 300) }));
