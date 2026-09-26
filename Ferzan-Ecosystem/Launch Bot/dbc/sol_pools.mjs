// Read-only: current state of Ferzan's Meteora DBC pools, for the launch indexer.
// stdin:  {rpc, config, items: [{mint, last_sig}]}   (every Ferzan Solana launch uses the one partner config)
// stdout: {pools: [{mint, pool, found, quote_reserve, threshold, migrated, price_sol, new_sigs, newest_sig, newest_ts, error?}]}
import fs from 'fs'
import { Connection, PublicKey } from '@solana/web3.js'
import { NATIVE_MINT } from '@solana/spl-token'
import { DynamicBondingCurveClient, deriveDbcPoolAddress } from '@meteora-ag/dynamic-bonding-curve-sdk'

const big = (v) => (v === undefined || v === null ? '0' : typeof v.toString === 'function' ? v.toString() : String(v))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

try {
    const inp = JSON.parse(fs.readFileSync(0, 'utf8'))
    const conn = new Connection(inp.rpc, 'confirmed')
    const client = new DynamicBondingCurveClient(conn, 'confirmed')
    const config = new PublicKey(inp.config)
    const thresholds = {}
    const out = []
    for (const it of (inp.items || []).slice(0, 60)) {
        const row = { mint: it.mint, pool: '', found: false }
        try {
            const pool = deriveDbcPoolAddress(NATIVE_MINT, new PublicKey(it.mint), config)
            row.pool = pool.toBase58()
            const vp = await client.state.getPool(pool)
            if (!vp) { out.push(row); continue }
            const s = vp.poolState || vp
            const cfgKey = (s.config || config).toBase58()
            if (!(cfgKey in thresholds)) {
                const cfg = await client.state.getPoolConfig(new PublicKey(cfgKey))
                thresholds[cfgKey] = big(cfg && cfg.migrationQuoteThreshold)
            }
            row.found = true
            row.quote_reserve = big(s.quoteReserve)
            row.threshold = thresholds[cfgKey]
            row.migrated = Number(s.isMigrated || 0) === 1
            // sqrtPrice is Q64.64 of (raw SOL per raw token); tokens have 6 decimals, SOL 9
            const sq = BigInt(big(s.sqrtPrice))
            row.price_sol = sq > 0n ? (Number((sq * sq) >> 64n) / 2 ** 64) * 1e-3 : 0
            const opts = it.last_sig ? { until: it.last_sig, limit: 200 } : { limit: 1000 }
            const sigs = await conn.getSignaturesForAddress(pool, opts, 'confirmed')
            const ok = sigs.filter((x) => !x.err)
            row.new_sigs = ok.length
            row.newest_sig = sigs.length ? sigs[0].signature : (it.last_sig || '')
            row.newest_ts = ok.length ? (ok[0].blockTime || 0) : 0
        } catch (e) {
            row.error = String(e && e.message ? e.message : e).slice(0, 160)
        }
        out.push(row)
        await sleep(120) // stay polite to public Solana RPCs
    }
    process.stdout.write(JSON.stringify({ pools: out }))
} catch (e) {
    process.stdout.write(JSON.stringify({ error: String(e && e.message ? e.message : e) }))
    process.exit(1)
}
