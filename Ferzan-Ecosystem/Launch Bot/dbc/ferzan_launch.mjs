// FERZAN flagship launch on the Ferzan flagship Meteora config.
//   stdin: {mode: "plan"|"send", rpc, name, symbol, uri, vault}
//   plan: checks everything and simulates the pool creation (sends nothing)
//   send: creates the pool, then hands the pool creator to the Squads vault; resumes safely if re-run
// stdout: one JSON line. Keys stay in /opt/ferzan/dbc-keys; only public addresses are printed.
import fs from 'fs'
import { Connection, PublicKey, LAMPORTS_PER_SOL, SystemProgram, sendAndConfirmTransaction } from '@solana/web3.js'
import { NATIVE_MINT } from '@solana/spl-token'
import * as DBC from '@meteora-ag/dynamic-bonding-curve-sdk'
import { loadOrCreateKey, simulate } from './common.mjs'

const { DynamicBondingCurveClient, deriveDbcPoolAddress } = DBC
const out = { ok: false }
const done = (extra = {}) => { process.stdout.write(JSON.stringify({ ...out, ...extra }) + '\n'); process.exit(out.ok ? 0 : 1) }
try {
    const inp = JSON.parse(fs.readFileSync(0, 'utf8'))
    const conn = new Connection(inp.rpc, 'confirmed')
    const client = new DynamicBondingCurveClient(conn, 'confirmed')
    const launcher = loadOrCreateKey('ferzan-launcher.json')
    const mint = loadOrCreateKey('ferzan-mint.json') // FERZAN's address, fixed once
    const config = loadOrCreateKey('ferzan-flagship-config.json').publicKey
    const vault = new PublicKey(inp.vault)
    const pool = deriveDbcPoolAddress(NATIVE_MINT, mint.publicKey, config)
    Object.assign(out, { launcher: launcher.publicKey.toBase58(), mint: mint.publicKey.toBase58(), pool: pool.toBase58(),
        config: config.toBase58(), vault: vault.toBase58() })

    const bal = await conn.getBalance(launcher.publicKey)
    out.launcher_sol = bal / LAMPORTS_PER_SOL
    if (!(await conn.getAccountInfo(config))) throw new Error('flagship config is not on Solana')
    const va = await conn.getAccountInfo(vault)
    out.vault_check = !va ? 'no account yet (fine for a Squads vault with no SOL)'
        : va.owner.equals(SystemProgram.programId) && !va.executable ? `ok (holds ${(va.lamports / LAMPORTS_PER_SOL).toFixed(4)} SOL)`
            : `WRONG: owned by ${va.owner.toBase58()}`
    if (out.vault_check.startsWith('WRONG')) throw new Error('the vault address is not a normal wallet account; use the Squads VAULT address')
    out.transfer_fn = typeof client.creator.transferPoolCreator === 'function'
        ? String(client.creator.transferPoolCreator).replace(/\s+/g, ' ').slice(0, 260) : 'MISSING'
    if (out.transfer_fn === 'MISSING') throw new Error('this SDK cannot hand over the pool creator')

    let existing = await client.state.getPool(pool)
    out.pool_exists = !!existing
    if (!existing) {
        if (bal < 0.05 * LAMPORTS_PER_SOL) throw new Error(`the launcher needs at least 0.05 SOL (has ${out.launcher_sol.toFixed(4)})`)
        const tx = await client.creator.createPoolWithFirstBuy({
            createPoolParam: { baseMint: mint.publicKey, config, name: String(inp.name).slice(0, 32), symbol: String(inp.symbol).slice(0, 10),
                uri: String(inp.uri || ''), payer: launcher.publicKey, poolCreator: launcher.publicKey },
            firstBuyParam: undefined, // no dev buy
        })
        tx.feePayer = launcher.publicKey
        tx.recentBlockhash = (await conn.getLatestBlockhash('confirmed')).blockhash
        const sim = await simulate(conn, tx)
        out.create_sim = sim.err ? { err: sim.err, logs: (sim.logs || []).slice(-6) } : 'ok'
        if (sim.err) throw new Error('pool creation would fail')
        if (inp.mode !== 'send') { out.ok = true; done({ note: 'PLAN ONLY. Nothing sent.' }) }
        out.create_sig = await sendAndConfirmTransaction(conn, tx, [launcher, mint], { commitment: 'confirmed', maxRetries: 5 })
        for (let i = 0; i < 20 && !existing; i++) { await new Promise((r) => setTimeout(r, 1000)); existing = await client.state.getPool(pool) }
        if (!existing) throw new Error('pool not visible after creation')
    } else if (inp.mode !== 'send') { out.ok = true; done({ note: 'PLAN: pool already exists' }) }

    // Hand the creator role (creator fees + the 650M locked tokens at graduation) to the Squads vault.
    const creatorOf = (vp) => (vp.poolState || vp).creator
    if (creatorOf(existing).equals(vault)) { out.ok = true; out.creator_now = vault.toBase58(); done({ note: 'creator already the vault' }) }
    let lastErr = ''
    for (let attempt = 1; attempt <= 3; attempt++) {
        try {
            const ttx = await client.creator.transferPoolCreator({ virtualPool: pool, creator: launcher.publicKey, newCreator: vault })
            ttx.feePayer = launcher.publicKey
            ttx.recentBlockhash = (await conn.getLatestBlockhash('confirmed')).blockhash
            out.transfer_sig = await sendAndConfirmTransaction(conn, ttx, [launcher], { commitment: 'confirmed', maxRetries: 5 })
            break
        } catch (e) { lastErr = String(e && e.message ? e.message : e).slice(0, 200); await new Promise((r) => setTimeout(r, 3000)) }
    }
    const after = await client.state.getPool(pool)
    out.creator_now = creatorOf(after).toBase58()
    if (!creatorOf(after).equals(vault)) throw new Error(`pool is live but the creator handover failed: ${lastErr}`)
    out.ok = true
    done()
} catch (e) {
    out.error = String(e && e.message ? e.message : e).slice(0, 300)
    done()
}
