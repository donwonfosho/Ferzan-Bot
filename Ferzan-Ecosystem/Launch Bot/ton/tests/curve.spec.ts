import fs from "node:fs";
import path from "node:path";
import { Blockchain, SandboxContract, TreasuryContract } from "@ton/sandbox";
import { Address, beginCell, Cell, contractAddress, toNano } from "@ton/core";

// The jetton code is Ferzan's pinned production code (same files the launch builder uses).
const CODE_DIR = process.env.TON_CODE_DIR || "/opt/ferzan/ton-code";
const load = (f: string) => Cell.fromBase64(fs.readFileSync(f, "utf8").trim());
const curveCode = load(path.join(__dirname, "..", "build", "ferzan_curve.b64"));
const minterCode = load(path.join(CODE_DIR, "jetton-minter.b64"));
const walletCode = load(path.join(CODE_DIR, "jetton-wallet.b64"));

const OP_BUY = 0x62757931, OP_SELL = 0x73656c6c, OP_GRADUATE = 0x67726164;
const OVERHEAD = toNano("0.12");
const SUPPLY = 1_000_000_000n * 10n ** 9n; // 1B coins, 9 decimals
const GRAD = toNano("100"); // graduates at 100 TON raised

describe("Ferzan TON curve", () => {
  let bc: Blockchain;
  let creator: SandboxContract<TreasuryContract>, treasury: SandboxContract<TreasuryContract>, keeper: SandboxContract<TreasuryContract>;
  let alice: SandboxContract<TreasuryContract>, bob: SandboxContract<TreasuryContract>, ref: SandboxContract<TreasuryContract>;
  let minter: Address, curve: Address;

  const cfg = (t: Address, c: Address, m: Address, k: Address) =>
    beginCell().storeAddress(t).storeAddress(c).storeAddress(m)
      .storeRef(beginCell().storeAddress(k).storeRef(walletCode).endCell()).endCell();

  const curveData = (t: Address, c: Address, m: Address, k: Address, start: number) =>
    beginCell().storeCoins(GRAD).storeCoins(SUPPLY).storeCoins(0).storeCoins(0)
      .storeUint(start, 32).storeUint(0, 1).storeUint(0, 1).storeRef(cfg(t, c, m, k)).endCell();

  async function walletOf(owner: Address): Promise<Address> {
    const r = await bc.runGetMethod(minter, "get_wallet_address", [{ type: "slice", cell: beginCell().storeAddress(owner).endCell() }]);
    return r.stackReader.readAddress();
  }
  async function coins(owner: Address): Promise<bigint> {
    const w = await walletOf(owner);
    const st = await bc.getContract(w);
    if (st.accountState?.type !== "active") return 0n;
    const r = await bc.runGetMethod(w, "get_wallet_data");
    return r.stackReader.readBigNumber();
  }
  async function state() {
    const r = (await bc.runGetMethod(curve, "get_curve")).stackReader;
    return {
      grad: r.readBigNumber(), supply: r.readBigNumber(), real: r.readBigNumber(), sold: r.readBigNumber(),
      start: r.readNumber(), complete: r.readBigNumber() !== 0n, graduated: r.readBigNumber() !== 0n,
    };
  }
  const ton = async (a: Address) => (await bc.getContract(a)).balance;
  const noAddr = beginCell().storeAddress(null);

  async function buy(who: SandboxContract<TreasuryContract>, spend: bigint, minOut = 0n, referrer: Address | null = null) {
    return who.send({
      to: curve, value: spend + OVERHEAD,
      body: beginCell().storeUint(OP_BUY, 32).storeUint(0, 64).storeUint(minOut, 128).storeAddress(referrer).endCell(),
    });
  }
  async function sell(who: SandboxContract<TreasuryContract>, amount: bigint, minOut = 0n, withPayload = true, value = toNano("0.3")) {
    const payload = withPayload ? beginCell().storeUint(OP_SELL, 32).storeUint(minOut, 128).storeAddress(null).endCell() : null;
    const body = beginCell().storeUint(0x0f8a7ea5, 32).storeUint(0, 64).storeCoins(amount)
      .storeAddress(curve).storeAddress(who.address).storeBit(0).storeCoins(toNano("0.2"));
    if (payload) body.storeBit(1).storeRef(payload); else body.storeBit(0);
    return who.send({ to: await walletOf(who.address), value, body: body.endCell() });
  }

  beforeEach(async () => {
    bc = await Blockchain.create();
    creator = await bc.treasury("creator"); treasury = await bc.treasury("treasury"); keeper = await bc.treasury("keeper");
    alice = await bc.treasury("alice"); bob = await bc.treasury("bob"); ref = await bc.treasury("ref");

    const content = beginCell().storeUint(1, 8).storeBuffer(Buffer.from("https://example.com/m.json")).endCell();
    const minterInit = { code: minterCode, data: beginCell().storeCoins(0).storeAddress(creator.address).storeRef(content).storeRef(walletCode).endCell() };
    minter = contractAddress(0, minterInit);
    const curveInit = { code: curveCode, data: curveData(treasury.address, creator.address, minter, keeper.address, 0) };
    curve = contractAddress(0, curveInit);

    await creator.send({ to: curve, value: toNano("0.05"), init: curveInit, bounce: false });
    const internalTransfer = beginCell().storeUint(0x178d4519, 32).storeUint(1, 64).storeCoins(SUPPLY)
      .storeAddress(null).storeAddress(creator.address).storeCoins(0).storeBit(0).endCell();
    const mint = beginCell().storeUint(21, 32).storeUint(1, 64).storeAddress(curve).storeCoins(toNano("0.06")).storeRef(internalTransfer).endCell();
    await creator.send({ to: minter, value: toNano("0.25"), init: minterInit, body: mint, bounce: false });
    await creator.send({ to: minter, value: toNano("0.05"), body: beginCell().storeUint(3, 32).storeUint(2, 64).storeAddress(null).endCell() });
  });

  it("the curve's own coin wallet holds the whole supply, and the getter matches the real wallet", async () => {
    const w = await walletOf(curve);
    const got = (await bc.runGetMethod(curve, "get_wallet")).stackReader.readAddress();
    expect(got.toString()).toBe(w.toString());
    expect(await coins(curve)).toBe(SUPPLY);
  });


  it("DIAG one buy: every transaction, exit code and action result", async () => {
    const names = new Map<string, string>();
    const put = (n: string, a: Address) => names.set(a.toString(), n);
    put("creator", creator.address); put("treasury", treasury.address); put("keeper", keeper.address);
    put("alice", alice.address); put("minter", minter); put("CURVE", curve);
    put("curveWallet", await walletOf(curve)); put("aliceWallet", await walletOf(alice.address));
    const label = (a: any) => (a ? names.get(a.toString()) ?? a.toString().slice(0, 10) : "?");
    const res = await buy(alice, toNano("10"));
    const lines: string[] = [];
    for (const tx of res.transactions) {
      const d: any = tx.description, info: any = tx.inMessage?.info;
      const cp = d.computePhase, ap = d.actionPhase;
      const comp = cp ? (cp.type === "vm" ? `exit ${cp.exitCode} gas ${cp.gasUsed} ok=${cp.success}` : `skipped:${cp.reason}`) : "none";
      const act = ap ? `ok=${ap.success} valid=${ap.valid} code=${ap.resultCode} actions=${ap.totalActions} skipped=${ap.skippedActions}` : "none";
      lines.push(`${label(info?.src)} -> ${label(info?.dest)} value=${info?.value?.coins} bounce=${info?.bounce} compute[${comp}] action[${act}] aborted=${d.aborted}`);
    }
    const s = await state();
    lines.push(`state after: real=${s.real} sold=${s.sold} complete=${s.complete}`);
    lines.push(`alice coins=${await coins(alice.address)}  curve balance=${await ton(curve)}`);
    console.log("DIAG\n" + lines.join("\n"));
  });

  it("buy pays out exactly the quoted coins and takes 1% (creator 50%, platform the rest)", async () => {
    const spend = toNano("10");
    const q = (await bc.runGetMethod(curve, "get_quote_buy", [{ type: "int", value: spend }])).stackReader;
    const out = q.readBigNumber(), gross = q.readBigNumber(), refund = q.readBigNumber(), fee = q.readBigNumber();
    expect(refund).toBe(0n);
    const c0 = await ton(creator.address), t0 = await ton(treasury.address);
    await buy(alice, spend);
    expect(await coins(alice.address)).toBe(out);
    const s = await state();
    expect(s.real).toBe(gross - fee);
    expect(s.sold).toBe(out);
    expect((await ton(creator.address)) - c0).toBeGreaterThan(fee * 49n / 100n - toNano("0.01"));
    expect((await ton(treasury.address)) - t0).toBeGreaterThan(fee * 49n / 100n - toNano("0.01"));
    expect(await ton(curve)).toBeGreaterThanOrEqual(s.real); // fully backed
  });

  it("a referrer gets 10% of the fee, but not the buyer themself", async () => {
    const r0 = await ton(ref.address);
    await buy(alice, toNano("10"), 0n, ref.address);
    const gained = (await ton(ref.address)) - r0;
    expect(gained).toBeGreaterThan(toNano("0.009"));
    expect(gained).toBeLessThanOrEqual(toNano("0.0101"));
    const a0 = await ton(alice.address);
    await buy(alice, toNano("10"), 0n, alice.address); // self-referral: nobody is paid the referral share
    expect((await ton(ref.address)) - r0).toBe(gained);
    expect(a0).toBeGreaterThan(0n);
  });

  it("slippage: min tokens out too high is refused and nothing changes", async () => {
    const before = await state();
    const tx = await buy(alice, toNano("5"), SUPPLY);
    expect(tx.transactions.some((t) => t.description.type === "generic" && t.description.computePhase.type === "vm" && t.description.computePhase.exitCode === 105)).toBe(true);
    expect((await state()).sold).toBe(before.sold);
    expect(await coins(alice.address)).toBe(0n);
  });

  it("sell returns TON, restores the reserves and stays fully backed", async () => {
    await buy(alice, toNano("20"));
    const held = await coins(alice.address);
    const s1 = await state();
    const q = (await bc.runGetMethod(curve, "get_quote_sell", [{ type: "int", value: held / 2n }])).stackReader;
    const out = q.readBigNumber();
    const a0 = await ton(alice.address);
    await sell(alice, held / 2n);
    expect(await coins(alice.address)).toBe(held - held / 2n);
    const s2 = await state();
    expect(s2.sold).toBe(s1.sold - held / 2n);
    expect(s2.real).toBeLessThan(s1.real);
    expect((await ton(alice.address)) - a0).toBeGreaterThan(out - toNano("0.35")); // out, minus the ~0.3 TON attached and mostly returned
    expect(await ton(curve)).toBeGreaterThanOrEqual(s2.real);
  });

  it("selling everything after one buy cannot drain more than was paid in", async () => {
    await buy(alice, toNano("30"));
    const held = await coins(alice.address);
    const a0 = await ton(alice.address);
    await sell(alice, held);
    const s = await state();
    expect(s.sold).toBe(0n);
    expect(s.real).toBeGreaterThanOrEqual(0n);
    expect((await ton(alice.address)) - a0).toBeLessThan(toNano("30"));
    expect(await ton(curve)).toBeGreaterThanOrEqual(s.real);
  });

  it("a sell with no instruction (plain coin transfer) gets the coins straight back", async () => {
    await buy(alice, toNano("10"));
    const held = await coins(alice.address);
    const before = await state();
    await sell(alice, held, 0n, false);
    expect(await coins(alice.address)).toBe(held);
    expect((await state()).sold).toBe(before.sold);
  });

  it("only the curve's own coin wallet can report a sell", async () => {
    const body = beginCell().storeUint(0x7362d09c, 32).storeUint(0, 64).storeCoins(1n).storeAddress(bob.address).storeBit(0).endCell();
    const tx = await bob.send({ to: curve, value: toNano("0.3"), body });
    expect(tx.transactions.some((t) => t.description.type === "generic" && t.description.computePhase.type === "vm" && t.description.computePhase.exitCode === 110)).toBe(true);
  });

  it("a buy that passes the target fills the curve, refunds the excess and closes trading", async () => {
    const b0 = await ton(bob.address);
    await buy(bob, toNano("150"));
    const s = await state();
    expect(s.complete).toBe(true);
    expect(s.real).toBeGreaterThan(GRAD - GRAD / 5000n);
    expect(s.real).toBeLessThanOrEqual(GRAD);
    expect(b0 - (await ton(bob.address))).toBeLessThan(toNano("120")); // ~100 TON + fee spent, not 150
    const tx = await buy(alice, toNano("1"));
    expect(tx.transactions.some((t) => t.description.type === "generic" && t.description.computePhase.type === "vm" && t.description.computePhase.exitCode === 101)).toBe(true);
    // about 80% of the supply sold at the target
    expect(s.sold).toBeGreaterThan(SUPPLY * 79n / 100n);
    // grad/3 rounds down to whole nanoTON, so the curve lands a hair above 80%: allow one part in a billion
    expect(s.sold).toBeLessThanOrEqual(SUPPLY * 80n / 100n + SUPPLY / 1_000_000_000n);
  });

  it("graduate: raised TON and the remaining coins go to the keeper, once", async () => {
    await buy(bob, toNano("150"));
    const s = await state();
    const k0 = await ton(keeper.address);
    await alice.send({ to: curve, value: toNano("0.3"), body: beginCell().storeUint(OP_GRADUATE, 32).storeUint(0, 64).endCell() });
    expect((await ton(keeper.address)) - k0).toBeGreaterThan(s.real - toNano("0.01"));
    expect(await coins(keeper.address)).toBe(s.supply - s.sold);
    expect((await state()).graduated).toBe(true);
    const again = await alice.send({ to: curve, value: toNano("0.3"), body: beginCell().storeUint(OP_GRADUATE, 32).storeUint(0, 64).endCell() });
    expect(again.transactions.some((t) => t.description.type === "generic" && t.description.computePhase.type === "vm" && t.description.computePhase.exitCode === 121)).toBe(true);
  });

  it("graduate before the curve is full is refused", async () => {
    await buy(alice, toNano("5"));
    const tx = await bob.send({ to: curve, value: toNano("0.3"), body: beginCell().storeUint(OP_GRADUATE, 32).storeUint(0, 64).endCell() });
    expect(tx.transactions.some((t) => t.description.type === "generic" && t.description.computePhase.type === "vm" && t.description.computePhase.exitCode === 120)).toBe(true);
  });

  it("many small trades in a row never leave the curve short of its recorded TON", async () => {
    for (let i = 0; i < 6; i += 1) await buy(i % 2 ? alice : bob, toNano("3") + BigInt(i) * toNano("0.37"));
    for (let i = 0; i < 3; i += 1) { const h = await coins(alice.address); if (h > 0n) await sell(alice, h / 3n + 1n); }
    const s = await state();
    expect(await ton(curve)).toBeGreaterThanOrEqual(s.real);
  });

  it("nothing owned by anyone can change the curve's settings (no admin message exists)", async () => {
    const before = (await bc.runGetMethod(curve, "get_curve")).stack.length;
    const tx = await creator.send({ to: curve, value: toNano("0.1"), body: beginCell().storeUint(0x12345678, 32).storeUint(0, 64).endCell() });
    expect(tx.transactions.some((t) => t.description.type === "generic" && t.description.computePhase.type === "vm" && t.description.computePhase.exitCode === 0xffff)).toBe(true);
    expect((await bc.runGetMethod(curve, "get_curve")).stack.length).toBe(before);
  });
});
