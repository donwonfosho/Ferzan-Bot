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

// Simulate a legacy Transaction without needing any signatures.
export async function simulate(conn, tx) {
    const vtx = new VersionedTransaction(tx.compileMessage())
    const res = await conn.simulateTransaction(vtx, { sigVerify: false, replaceRecentBlockhash: true })
    return res.value
}
