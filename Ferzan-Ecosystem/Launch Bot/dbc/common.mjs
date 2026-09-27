// Shared helpers for Ferzan's Meteora DBC scripts.
import fs from 'fs'
import path from 'path'
import { Keypair, VersionedTransaction } from '@solana/web3.js'

export function loadEnv(file = '/opt/ferzan/.env') {
    const out = {}
    try {
        for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
            const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/)
            if (!m) continue
            let v = m[2]
            if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1)
            out[m[1]] = v
        }
    } catch {}
    return { ...out, ...process.env }
}

// Keys live OUTSIDE the git repo so they can never be committed.
export const KEYDIR = '/opt/ferzan/dbc-keys'

export function loadOrCreateKey(name) {
    fs.mkdirSync(KEYDIR, { recursive: true, mode: 0o700 })
    const f = path.join(KEYDIR, name)
    if (fs.existsSync(f)) {
        return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(f, 'utf8'))))
    }
    const k = Keypair.generate()
    fs.writeFileSync(f, JSON.stringify(Array.from(k.secretKey)), { mode: 0o600 })
    return k
}

// MULTI_CONFIG_B23: every Ferzan partner config, newest first. METEORA_CONFIG is the one new launches
// use; METEORA_CONFIGS lists all of them (old ones too) so older coins keep trading.
export function allConfigs(inp = {}) {
    const env = loadEnv()
    const raw = [...(Array.isArray(inp.configs) ? inp.configs : []), inp.config, env.METEORA_CONFIG,
        ...String(env.METEORA_CONFIGS || '').split(',')]
    return [...new Set(raw.map((x) => String(x || '').trim()).filter((x) => /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(x)))]
}

// A coin's curve pool: derived from each known config, then (if the SDK can) looked up by its mint.
export async function findPool(client, mint, configs, deriveDbcPoolAddress, NATIVE_MINT, PublicKey) {
    for (const c of configs) {
        const pool = deriveDbcPoolAddress(NATIVE_MINT, mint, new PublicKey(c))
        const vp = await client.state.getPool(pool)
        if (vp) return { pool, vp, config: c }
    }
    if (typeof client.state.getPoolByBaseMint === 'function') {
        try {
            const hit = await client.state.getPoolByBaseMint(mint)
            if (hit) {
                const pool = hit.publicKey || hit.pubkey
                const vp = hit.account || hit
                if (pool) return { pool, vp, config: String(((vp.poolState || vp).config || '')) }
            }
        } catch {}
    }
    return null
}

// Simulate a legacy Transaction without needing any signatures.
export async function simulate(conn, tx) {
    const vtx = new VersionedTransaction(tx.compileMessage())
    const res = await conn.simulateTransaction(vtx, { sigVerify: false, replaceRecentBlockhash: true })
    return res.value
}
