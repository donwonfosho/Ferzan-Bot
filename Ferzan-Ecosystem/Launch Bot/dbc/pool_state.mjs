// Read-only helper for sol_trades.py: stays running and answers one JSON line per request.
// stdin line:  {"id": 1, "pools": ["<pool>", ...]}
// stdout line: {"id": 1, "states": {"<pool>": {"price_sol": n, "quote_reserve": "raw", "migrated": bool}}, "error"?: "..."}
// price_sol = SOL per whole token (tokens have 6 decimals, SOL 9), same formula as sol_pools.mjs.
import readline from 'readline'
import { Connection, PublicKey } from '@solana/web3.js'
import { DynamicBondingCurveClient } from '@meteora-ag/dynamic-bonding-curve-sdk'

const rpc = process.argv[2] || 'https://api.mainnet-beta.solana.com'
const conn = new Connection(rpc, 'confirmed')
const client = new DynamicBondingCurveClient(conn, 'confirmed')
const big = (v) => (v === undefined || v === null ? '0' : typeof v.toString === 'function' ? v.toString() : String(v))

const rl = readline.createInterface({ input: process.stdin })
rl.on('line', async (line) => {
    let id = 0
    try {
        const req = JSON.parse(line)
        id = req.id || 0
        const states = {}
        for (const p of (req.pools || []).slice(0, 40)) {
            try {
                const vp = await client.state.getPool(new PublicKey(p))
                if (!vp) continue
                const s = vp.poolState || vp
                const sq = BigInt(big(s.sqrtPrice))
                states[p] = {
                    price_sol: sq > 0n ? (Number((sq * sq) >> 64n) / 2 ** 64) * 1e-3 : 0,
                    quote_reserve: big(s.quoteReserve),
                    migrated: Number(s.isMigrated || 0) === 1,
                }
            } catch (e) {
                states[p] = { error: String(e && e.message ? e.message : e).slice(0, 120) }
            }
        }
        process.stdout.write(JSON.stringify({ id, states }) + '\n')
    } catch (e) {
        process.stdout.write(JSON.stringify({ id, states: {}, error: String(e && e.message ? e.message : e).slice(0, 160) }) + '\n')
    }
})
process.stdout.write(JSON.stringify({ ready: true }) + '\n')
