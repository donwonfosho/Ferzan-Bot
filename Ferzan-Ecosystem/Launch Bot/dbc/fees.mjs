// Meteora DBC trading-fee claims (creator share + platform/partner share).
// Reads JSON on stdin, prints JSON on stdout. Nothing here holds or uses a private key:
// it only builds unsigned transactions that the claiming wallet signs in the Mini App.
//   {action:'list',  role:'creator'|'partner', wallet, rpc, config}
//   {action:'build', role, wallet, pools:[...], rpc, config, simulate?}
import fs from 'fs'
import BN from 'bn.js'
import { Connection, PublicKey, Transaction } from '@solana/web3.js'
import { ASSOCIATED_TOKEN_PROGRAM_ID, createCloseAccountInstruction } from '@solana/spl-token'
import { DynamicBondingCurveClient } from '@meteora-ag/dynamic-bonding-curve-sdk'
import { simulate } from './common.mjs'

const U64_MAX = new BN('18446744073709551615')
const WSOL = 'So11111111111111111111111111111111111111112'

// ---- DAMM v2 (after a Meteora curve graduates): the permanently locked LP positions that the
// migration gives the creator and the platform keep earning trading fees. Optional: if the
// @meteora-ag/cp-amm-sdk package isn't installed, these are simply skipped.
let _damm = null
async function damm(conn) {
    if (_damm === null) {
        try {
            const m = await import('@meteora-ag/cp-amm-sdk')
            _damm = { m, amm: new m.CpAmm(conn) }
        } catch { _damm = false }
    }
    return _damm || null
}

async function listDamm(conn, wallet) {
    const d = await damm(conn)
    if (!d) return []
    const out = []
    for (const p of await d.amm.getPositionsByUser(wallet)) {
        try {
            const ps = await d.amm.fetchPoolState(p.positionState.pool)
            const fee = d.m.getUnClaimLpFee(ps, p.positionState)
            const aSol = ps.tokenAMint.toBase58() === WSOL
            const solFee = aSol ? fee.feeTokenA : fee.feeTokenB
            const tokFee = aSol ? fee.feeTokenB : fee.feeTokenA
            if (!isPos(solFee) && !isPos(tokFee)) continue
            out.push({
                kind: 'damm', pool: p.position.toBase58(), dammPool: p.positionState.pool.toBase58(),
                mint: (aSol ? ps.tokenBMint : ps.tokenAMint).toBase58(),
                quote_fee: big(solFee), base_fee: big(tokFee), migrated: true,
            })
        } catch { /* skip unreadable positions */ }
    }
    return out
}

async function dammClaimIxs(conn, wallet, positionAddr) {
    const d = await damm(conn)
    if (!d) throw new Error(`pool ${positionAddr} not found`)
    const mine = (await d.amm.getPositionsByUser(wallet)).find((p) => p.position.toBase58() === positionAddr)
    if (!mine) throw new Error(`you don't own LP position ${positionAddr}`)
    const ps = await d.amm.fetchPoolState(mine.positionState.pool)
    const fee = d.m.getUnClaimLpFee(ps, mine.positionState)
    const tx = await d.amm.claimPositionFee({
        owner: wallet, pool: mine.positionState.pool, position: mine.position,
        positionNftAccount: mine.positionNftAccount,
        tokenAVault: ps.tokenAVault, tokenBVault: ps.tokenBVault, tokenAMint: ps.tokenAMint, tokenBMint: ps.tokenBMint,
        tokenAProgram: d.m.getTokenProgram(ps.tokenAFlag), tokenBProgram: d.m.getTokenProgram(ps.tokenBFlag),
    })
    const aSol = ps.tokenAMint.toBase58() === WSOL
    return { ixs: [...tx.instructions], quote: big(aSol ? fee.feeTokenA : fee.feeTokenB) }
}
const MAX_POOLS = 12

const big = (v) => (v && typeof v.toString === 'function' ? v.toString() : String(v || 0))
const isPos = (v) => !!v && typeof v.gtn === 'function' && v.gtn(0)

async function feeClaimerOf(client, config) {
    if (!config) return ''
    try {
        const cfg = await client.state.getPoolConfig(config)
        return cfg?.feeClaimer ? cfg.feeClaimer.toBase58() : ''
    } catch { return '' }
}

async function listPools(client, role, wallet, config) {
    const raw = role === 'partner'
        ? await client.state.getPoolsByConfig(config)
        : await client.state.getPoolsByCreator(wallet)
    const out = []
    for (const p of raw) {
        const s = p.account.poolState || p.account
        const quote = role === 'partner' ? s.partnerQuoteFee : s.creatorQuoteFee
        const base = role === 'partner' ? s.partnerBaseFee : s.creatorBaseFee
        if (!isPos(quote) && !isPos(base)) continue
        out.push({
            pool: p.publicKey.toBase58(),
            mint: s.baseMint.toBase58(),
            config: s.config.toBase58(),
            quote_fee: big(quote),
            base_fee: big(base),
            migrated: Number(s.isMigrated || 0) === 1,
        })
    }
    out.sort((a, b) => (BigInt(b.quote_fee) > BigInt(a.quote_fee) ? 1 : BigInt(b.quote_fee) < BigInt(a.quote_fee) ? -1 : 0))
    return out
}

// One claim for one pool, as a plain instruction list.
async function claimIxs(conn, client, role, wallet, poolAddr) {
    const pool = new PublicKey(poolAddr)
    const vp = await client.state.getPool(pool)
    if (!vp) return dammClaimIxs(conn, wallet, poolAddr) // not a curve pool: a graduated LP position
    const s = vp.poolState || vp
    const baseFee = role === 'partner' ? s.partnerBaseFee : s.creatorBaseFee
    const quoteFee = role === 'partner' ? s.partnerQuoteFee : s.creatorQuoteFee
    if (role === 'creator' && !s.creator.equals(wallet)) throw new Error(`you are not the creator of pool ${poolAddr}`)
    const maxBase = isPos(baseFee) ? U64_MAX : new BN(0)
    const common = { payer: wallet, pool, maxBaseAmount: maxBase, maxQuoteAmount: U64_MAX }
    const tx = role === 'partner'
        ? await client.partner.claimPartnerTradingFee({ ...common, feeClaimer: wallet })
        : await client.creator.claimCreatorTradingFee({ ...common, creator: wallet })
    const ixs = [...tx.instructions]
    // The SDK always opens a token account for the launched token so a base-token fee has
    // somewhere to land. If the wallet didn't have one and there is no base fee, close it
    // again in the same transaction so the ~0.002 SOL rent comes straight back.
    if (!isPos(baseFee)) {
        const create = ixs.find((ix) => ix.programId.equals(ASSOCIATED_TOKEN_PROGRAM_ID) && ix.keys[3]?.pubkey.equals(s.baseMint))
        if (create) {
            const ata = create.keys[1].pubkey
            const tokenProgram = create.keys[5].pubkey
            if (!(await conn.getAccountInfo(ata))) ixs.push(createCloseAccountInstruction(ata, wallet, wallet, [], tokenProgram))
        }
    }
    return { ixs, quote: big(quoteFee) }
}

function fits(ixs, wallet, blockhash) {
    const t = new Transaction({ feePayer: wallet, recentBlockhash: blockhash })
    t.add(...ixs)
    try { return t.serialize({ requireAllSignatures: false, verifySignatures: false }).length <= 1232 } catch { return false }
}

try {
    const inp = JSON.parse(fs.readFileSync(0, 'utf8'))
    const conn = new Connection(inp.rpc, 'confirmed')
    const client = new DynamicBondingCurveClient(conn, 'confirmed')
    const role = inp.role === 'partner' ? 'partner' : 'creator'
    const wallet = new PublicKey(inp.wallet)
    const config = inp.config ? new PublicKey(inp.config) : null
    if (role === 'partner' && !config) throw new Error('METEORA_CONFIG missing')
    const claimer = role === 'partner' ? await feeClaimerOf(client, config) : wallet.toBase58()

    if (inp.action === 'list') {
        const pools = [...await listPools(client, role, wallet, config), ...await listDamm(conn, wallet)]
        pools.sort((a, b) => (BigInt(b.quote_fee) > BigInt(a.quote_fee) ? 1 : BigInt(b.quote_fee) < BigInt(a.quote_fee) ? -1 : 0))
        const total = pools.reduce((a, p) => a + BigInt(p.quote_fee), 0n)
        process.stdout.write(JSON.stringify({
            role, wallet: wallet.toBase58(), fee_claimer: claimer,
            wallet_can_claim: claimer === wallet.toBase58(), pools, total_quote: total.toString(),
            damm_supported: !!(await damm(conn)),
        }))
    } else if (inp.action === 'build') {
        if (role === 'partner' && claimer !== wallet.toBase58()) {
            throw new Error(`Only the platform fee wallet (${claimer.slice(0, 4)}…${claimer.slice(-4)}) can claim platform fees. Connect that wallet.`)
        }
        const pools = (Array.isArray(inp.pools) ? inp.pools : []).map(String).slice(0, MAX_POOLS)
        if (!pools.length) throw new Error('No pools to claim')
        const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash('confirmed')
        const groups = [] // [{ixs, pools, quote}]
        for (const p of pools) {
            const c = await claimIxs(conn, client, role, wallet, p)
            const last = groups[groups.length - 1]
            if (last && fits([...last.ixs, ...c.ixs], wallet, blockhash)) {
                last.ixs.push(...c.ixs); last.pools.push(p); last.quote += BigInt(c.quote)
            } else {
                if (!fits(c.ixs, wallet, blockhash)) throw new Error(`claim for ${p} is too large for one transaction`)
                groups.push({ ixs: [...c.ixs], pools: [p], quote: BigInt(c.quote) })
            }
        }
        const txs = []
        for (const g of groups) {
            const t = new Transaction({ feePayer: wallet, recentBlockhash: blockhash })
            t.add(...g.ixs)
            const raw = t.serialize({ requireAllSignatures: false, verifySignatures: false })
            const item = { tx_b64: Buffer.from(raw).toString('base64'), pools: g.pools, quote: g.quote.toString(), size: raw.length }
            if (inp.simulate) {
                const sim = await simulate(conn, t)
                item.sim_err = sim.err || null
                item.sim_logs = (sim.logs || []).slice(-6)
            }
            txs.push(item)
        }
        process.stdout.write(JSON.stringify({ role, txs, last_valid_block_height: lastValidBlockHeight }))
    } else {
        throw new Error('unknown action')
    }
} catch (e) {
    process.stdout.write(JSON.stringify({ error: String(e && e.message ? e.message : e) }))
    process.exit(1)
}
