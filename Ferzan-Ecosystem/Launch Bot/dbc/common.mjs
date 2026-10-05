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

function readKey(f, name) {
    let arr
    try { arr = JSON.parse(fs.readFileSync(f, 'utf8')) } catch (e) {
        throw new Error(`key file ${name} cannot be read (${String(e.message).slice(0, 60)}); refusing to replace it`)
    }
    if (!Array.isArray(arr) || arr.length !== 64) throw new Error(`key file ${name} is damaged (not a 64-byte key); refusing to replace it`)
    return Keypair.fromSecretKey(Uint8Array.from(arr))
}

// For anything that runs unattended or moves money. A missing key file is a problem to fix by hand
// (restore it from backup), never something to quietly replace with a brand-new address.
export function loadKey(name) {
    const f = path.join(KEYDIR, name)
    if (!fs.existsSync(f)) {
        throw new Error(`key file ${name} is missing from ${KEYDIR}; refusing to make a new one (restore it from backup, or run the setup script)`)
    }
    return readKey(f, name)
}

// Setup scripts only. Safe against two processes racing: the first file written wins and the
// loser reads it, so nobody keeps a key that was overwritten a moment later.
export function loadOrCreateKey(name) {
    fs.mkdirSync(KEYDIR, { recursive: true, mode: 0o700 })
    const f = path.join(KEYDIR, name)
    if (fs.existsSync(f)) return readKey(f, name)
    const k = Keypair.generate()
    const tmp = `${f}.${process.pid}.${Date.now()}.tmp`
    const fd = fs.openSync(tmp, 'wx', 0o600)
    try {
        fs.writeSync(fd, JSON.stringify(Array.from(k.secretKey)))
        fs.fsyncSync(fd)
    } finally { fs.closeSync(fd) }
    try {
        fs.linkSync(tmp, f) // atomic, and fails if the file appeared in the meantime
        return k
    } catch (e) {
        if (e.code === 'EEXIST') return readKey(f, name)
        throw e
    } finally { try { fs.unlinkSync(tmp) } catch {} }
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
