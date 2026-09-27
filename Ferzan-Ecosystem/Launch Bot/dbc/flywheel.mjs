// Ferzan flywheel, one daily pass (run by ferzan_flywheel.py).
//   stdin: {mode: "plan"|"live", rpc, treasury, buyShareBps, reserveLamports, minBuyLamports, maxBuyLamports, carryLamports, skipBuy}
//   1 claim Ferzan's share of trading fees (every config whose fee claimer is the keeper wallet)
//   2 split: buyShare of the claim (+ carried budget) buys FERZAN, the rest goes to the treasury
//   3 buy FERZAN (curve before graduation, Jupiter after), 4 burn every FERZAN the keeper holds, 5 forward the rest
// plan: reads and simulates only. stdout: one JSON line.
import fs from 'fs'
import { spawnSync } from 'child_process'
import BN from 'bn.js'
import { Connection, PublicKey, Transaction, VersionedTransaction, SystemProgram, LAMPORTS_PER_SOL, sendAndConfirmTransaction } from '@solana/web3.js'
import { NATIVE_MINT, getAssociatedTokenAddressSync, createBurnCheckedInstruction } from '@solana/spl-token'
import * as DBC from '@meteora-ag/dynamic-bonding-curve-sdk'
import { loadOrCreateKey, allConfigs, findPool, simulate } from './common.mjs'

const { DynamicBondingCurveClient, deriveDbcPoolAddress } = DBC
const out = { ok: false, steps: [] }
const log = (s) => out.steps.push(s)
const finish = () => { process.stdout.write(JSON.stringify(out) + '\n'); process.exit(out.ok ? 0 : 1) }
const runNode = (script, payload) => {
    const p = spawnSync('node', [script], { input: JSON.stringify(payload), encoding: 'utf8', timeout: 180000, cwd: process.cwd() })
    const s = p.stdout || ''
    try { return JSON.parse(s.slice(s.indexOf('{'))) } catch { return { error: (p.stderr || s || 'no output').slice(-200) } }
}
async function sendSigned(conn, raw, keeper) {
    const buf = Buffer.from(raw, 'base64')
    let sig
    try {
        const tx = Transaction.from(buf); tx.partialSign(keeper)
        sig = await conn.sendRawTransaction(tx.serialize(), { maxRetries: 5 })
    } catch {
        const vtx = VersionedTransaction.deserialize(buf); vtx.sign([keeper])
        sig = await conn.sendRawTransaction(vtx.serialize(), { maxRetries: 5 })
    }
    const res = await conn.confirmTransaction(sig, 'confirmed')
    if (res.value && res.value.err) throw new Error(`transaction failed: ${JSON.stringify(res.value.err)}`)
    return sig
}
try {
    const inp = JSON.parse(fs.readFileSync(0, 'utf8'))
    const live = inp.mode === 'live'
    const conn = new Connection(inp.rpc, 'confirmed')
    const client = new DynamicBondingCurveClient(conn, 'confirmed')
    const keeper = loadOrCreateKey('fee-keeper.json')
    const mint = loadOrCreateKey('ferzan-mint.json').publicKey
    const treasury = new PublicKey(inp.treasury)
    const share = Math.max(0, Math.min(10000, Number(inp.buyShareBps || 3000)))
    const reserve = Number(inp.reserveLamports || 20_000_000)
    Object.assign(out, { mode: inp.mode, keeper: keeper.publicKey.toBase58(), ferzan: mint.toBase58() })
    const startBal = await conn.getBalance(keeper.publicKey)
    out.keeper_sol_start = startBal / LAMPORTS_PER_SOL

    // 1) claim
    const list = runNode('fees.mjs', { action: 'list', role: 'partner', wallet: keeper.publicKey.toBase58(), rpc: inp.rpc })
    if (list.error) throw new Error(`fee list: ${list.error}`)
    const pools = (list.pools || []).filter((p) => BigInt(p.quote_fee || 0) > 0n).slice(0, 12)
    out.claimable_sol = Number(list.total_quote || 0) / LAMPORTS_PER_SOL
    log(`claimable ${out.claimable_sol.toFixed(6)} SOL from ${pools.length} pools`)
    let claimed = 0
    if (pools.length && startBal < 5_000_000) log('keeper has under 0.005 SOL for network fees: fund it; skipping the claim')
    else if (pools.length) {
        const built = runNode('fees.mjs', { action: 'build', role: 'partner', wallet: keeper.publicKey.toBase58(), rpc: inp.rpc,
            pools: pools.map((p) => p.pool), simulate: !live })
        if (built.error) throw new Error(`fee claim build: ${built.error}`)
        out.claim_sigs = []
        for (const t of built.txs || []) {
            if (!live) { if (t.sim_err) log(`claim simulation failed: ${JSON.stringify(t.sim_err)}`); claimed += Number(t.quote || 0); continue }
            out.claim_sigs.push(await sendSigned(conn, t.tx_b64, keeper)); claimed += Number(t.quote || 0)
        }
    }
    out.claimed_sol = claimed / LAMPORTS_PER_SOL
    const toBuyNew = Math.floor(claimed * share / 10000)
    let budget = Number(inp.carryLamports || 0) + toBuyNew
    let forward = claimed - toBuyNew

    // 2+3) buy FERZAN
    out.bought_sol = 0
    const hit = await findPool(client, mint, allConfigs({}), deriveDbcPoolAddress, NATIVE_MINT, PublicKey)
    const amount = Math.min(budget, Number(inp.maxBuyLamports || 2 * LAMPORTS_PER_SOL))
    if (inp.skipBuy) log('buyback paused (before FERZAN launch or in its first hour); budget carried')
    else if (amount < Number(inp.minBuyLamports || 10_000_000)) log(`buyback budget ${(budget / LAMPORTS_PER_SOL).toFixed(4)} SOL is below the minimum; carried`)
    else {
        const migrated = hit && Number((hit.vp.poolState || hit.vp).isMigrated || 0) === 1
        if (hit && !migrated) {
            const s = runNode('build_swap.mjs', { rpc: inp.rpc, mint: mint.toBase58(), owner: keeper.publicKey.toBase58(), side: 'buy',
                amount: String(amount), slippageBps: 300, simulate: true })
            if (s.error || s.sim_err) log(`buy not possible today: ${s.error || JSON.stringify(s.sim_err)}; carried`)
            else if (live) { out.buy_sig = await sendSigned(conn, s.tx_b64, keeper); out.bought_sol = amount / LAMPORTS_PER_SOL; budget -= amount }
            else { log(`would buy ${(amount / LAMPORTS_PER_SOL).toFixed(4)} SOL of FERZAN on the curve (about ${s.amount_out} raw)`); budget -= amount; out.bought_sol = amount / LAMPORTS_PER_SOL }
        } else if (hit && migrated) {
            const q = await (await fetch(`https://lite-api.jup.ag/swap/v1/quote?inputMint=${NATIVE_MINT.toBase58()}&outputMint=${mint.toBase58()}&amount=${amount}&slippageBps=300`)).json()
            if (!q || q.error || !q.outAmount) log(`no Jupiter route today: ${q && q.error}; carried`)
            else {
                const sw = await (await fetch('https://lite-api.jup.ag/swap/v1/swap', { method: 'POST', headers: { 'content-type': 'application/json' },
                    body: JSON.stringify({ quoteResponse: q, userPublicKey: keeper.publicKey.toBase58(), wrapAndUnwrapSol: true, dynamicComputeUnitLimit: true }) })).json()
                if (!sw.swapTransaction) log('Jupiter did not build a swap; carried')
                else if (live) { out.buy_sig = await sendSigned(conn, sw.swapTransaction, keeper); out.bought_sol = amount / LAMPORTS_PER_SOL; budget -= amount }
                else { log(`would buy ${(amount / LAMPORTS_PER_SOL).toFixed(4)} SOL of FERZAN on Jupiter`); budget -= amount; out.bought_sol = amount / LAMPORTS_PER_SOL }
            }
        } else log('FERZAN pool not found yet; budget carried')
    }

    // 4) burn everything the keeper holds
    out.burned_raw = '0'
    const mintInfo = await conn.getAccountInfo(mint)
    if (mintInfo) {
        const programId = mintInfo.owner
        const ata = getAssociatedTokenAddressSync(mint, keeper.publicKey, false, programId)
        const bal = await conn.getTokenAccountBalance(ata).catch(() => null)
        const raw = bal && bal.value ? bal.value.amount : '0'
        if (BigInt(raw) > 0n) {
            if (live) {
                const tx = new Transaction().add(createBurnCheckedInstruction(ata, mint, keeper.publicKey, BigInt(raw), 6, [], programId))
                out.burn_sig = await sendAndConfirmTransaction(conn, tx, [keeper], { commitment: 'confirmed' })
            } else log(`would burn ${raw} raw FERZAN`)
            out.burned_raw = raw
        }
    }

    // 5) forward the rest to the treasury, keeping the reserve and the carried budget
    const now = await conn.getBalance(keeper.publicKey)
    const spendable = Math.max(0, now - reserve - budget)
    forward = Math.min(forward, spendable)
    out.forward_sol = forward / LAMPORTS_PER_SOL
    if (forward >= 1_000_000) {
        if (live) {
            const tx = new Transaction().add(SystemProgram.transfer({ fromPubkey: keeper.publicKey, toPubkey: treasury, lamports: forward }))
            out.forward_sig = await sendAndConfirmTransaction(conn, tx, [keeper], { commitment: 'confirmed' })
        } else log(`would send ${(forward / LAMPORTS_PER_SOL).toFixed(6)} SOL to the treasury`)
    }
    out.carry_lamports = budget
    out.ok = true
    finish()
} catch (e) {
    out.error = String(e && e.message ? e.message : e).slice(0, 300)
    finish()
}
