// Build ONE unsigned Meteora DBC swap for a visitor's wallet (the website's buy/sell).
// stdin:  {rpc, config, mint, owner, side: "buy"|"sell", amount (raw: lamports to spend | token units to sell), slippageBps, simulate?}
// stdout: {pool, tx_b64, amount_in, amount_out, min_out, sim_err?, sim_logs?} | {error}
import fs from 'fs'
import BN from 'bn.js'
import { Connection, PublicKey } from '@solana/web3.js'
import { NATIVE_MINT } from '@solana/spl-token'
import * as sdk from '@meteora-ag/dynamic-bonding-curve-sdk'
import { simulate, allConfigs, findPool } from './common.mjs' // MULTI_CONFIG_B23

const { DynamicBondingCurveClient, deriveDbcPoolAddress } = sdk

try {
    const inp = JSON.parse(fs.readFileSync(0, 'utf8'))
    if (inp.side !== 'buy' && inp.side !== 'sell') throw new Error('side must be buy or sell')
    if (!/^\d{1,20}$/.test(String(inp.amount)) || String(inp.amount) === '0') throw new Error('amount looks wrong')
    const slippageBps = Math.min(5000, Math.max(10, Math.floor(Number(inp.slippageBps || 500))))
    const conn = new Connection(inp.rpc, 'confirmed')
    const client = new DynamicBondingCurveClient(conn, 'confirmed')
    const configs = allConfigs(inp)
    const mint = new PublicKey(inp.mint)
    const owner = new PublicKey(inp.owner)

    let stage = 'load'
    const mark = (x) => { stage = x; globalThis.__stage = `[${x}] ` }
    mark('load')
    const hit = await findPool(client, mint, configs, deriveDbcPoolAddress, NATIVE_MINT, PublicKey)
    if (!hit) throw new Error('no Ferzan curve pool for this token')
    const pool = hit.pool, vp = hit.vp, config = new PublicKey(hit.config || configs[0])
    // 1.5.x returns {publicKey?, poolState?...}; swapQuote reads virtualPool.poolState.*, so always pass that wrapper.
    const vpWrap = vp.poolState ? vp : { poolState: vp }
    const state = vpWrap.poolState
    if (Number(state.isMigrated || 0) === 1) throw new Error('this curve graduated - trade it on the DEX pool')
    const cfg = await client.state.getPoolConfig(state.config || config)
    // Time-based configs count in seconds, slot-based ones in slots.
    const currentPoint = Number(cfg.activationType) === 1 ? new BN(Math.floor(Date.now() / 1000)) : new BN(await conn.getSlot('confirmed'))
    const swapBaseForQuote = inp.side === 'sell' // selling the token (base) for SOL (quote)
    const amountIn = new BN(String(inp.amount))

    // 1.5.13: swapQuote(virtualPool, config, swapBaseForQuote, amountIn, slippageBps, hasReferral, currentPoint, eligibleForFirstSwapWithMinFee)
    // Newer SDKs take one object. Try the positional form first, then the object forms.
    mark('quote')
    const quoteFn = sdk.swapQuote || (client.pool && client.pool.swapQuote && client.pool.swapQuote.bind(client.pool))
    if (!quoteFn) throw new Error('this SDK has no swapQuote')
    const attempts = [
        () => quoteFn(vpWrap, cfg, swapBaseForQuote, amountIn, slippageBps, false, currentPoint, false),
        () => quoteFn({ virtualPool: vpWrap, config: cfg, swapBaseForQuote, amountIn, slippageBps, hasReferral: false, currentPoint }),
        () => quoteFn({ virtualPool: state, config: cfg, swapBaseForQuote, amountIn, slippageBps, hasReferral: false, currentPoint }),
    ]
    let q = null
    const errs = []
    for (const run of attempts) {
        try {
            q = await run()
            if (q) break
        } catch (e) {
            errs.push(String(e && e.message ? e.message : e).slice(0, 80))
        }
    }
    if (!q) throw new Error(`quote failed: ${errs.join(' | ')}`)
    const outRaw = q.amountOut ?? q.outputAmount ?? q.outAmount
    if (outRaw === undefined || outRaw === null) throw new Error(`quote returned no amount (fields: ${Object.keys(q).join(',')})`)
    const amountOut = new BN(outRaw.toString())
    if (amountOut.lten(0)) throw new Error('that amount gets nothing back')
    const minOut = q.minimumAmountOut
        ? new BN(q.minimumAmountOut.toString())
        : amountOut.muln(10000 - slippageBps).divn(10000)

    mark('swap')
    // Newer docs call the pool field `pool`, older ones `poolAddress`; pass both.
    const tx = await client.pool.swap({
        owner, payer: owner, pool, poolAddress: pool,
        amountIn, minimumAmountOut: minOut, swapBaseForQuote, referralTokenAccount: null,
    })
    mark('serialize')
    tx.feePayer = owner
    tx.recentBlockhash = (await conn.getLatestBlockhash('confirmed')).blockhash
    const raw = tx.serialize({ requireAllSignatures: false, verifySignatures: false })
    const out = {
        pool: pool.toBase58(),
        tx_b64: Buffer.from(raw).toString('base64'),
        amount_in: amountIn.toString(),
        amount_out: amountOut.toString(),
        min_out: minOut.toString(),
    }
    if (inp.simulate) {
        const sim = await simulate(conn, tx)
        out.sim_err = sim.err || null
        out.sim_logs = (sim.logs || []).slice(-6)
    }
    process.stdout.write(JSON.stringify(out))
} catch (e) {
    const where = (e && e.stack ? String(e.stack).split('\n').slice(1, 3).map((l) => l.trim()).join(' <- ') : '').slice(0, 200)
    process.stdout.write(JSON.stringify({ error: `${globalThis.__stage || ''}${String(e && e.message ? e.message : e).slice(0, 300)}`, where }))
    process.exit(1)
}
