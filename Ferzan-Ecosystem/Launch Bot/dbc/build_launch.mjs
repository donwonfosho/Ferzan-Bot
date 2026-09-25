// Per launch: build ONE unsigned tx = create DBC pool (+ creator dev buy) + Ferzan launch fee.
// Reads JSON on stdin, prints JSON on stdout. The creator's wallet signs + sends it.
//   {creator, name, symbol, uri, devBuyLamports, config, rpc, treasury, feeLamports, simulate?}
import fs from 'fs'
import BN from 'bn.js'
import { Connection, Keypair, PublicKey, SystemProgram } from '@solana/web3.js'
import { NATIVE_MINT } from '@solana/spl-token'
import { DynamicBondingCurveClient, deriveDbcPoolAddress } from '@meteora-ag/dynamic-bonding-curve-sdk'
import { simulate } from './common.mjs'

try {
    const inp = JSON.parse(fs.readFileSync(0, 'utf8'))
    const conn = new Connection(inp.rpc, 'confirmed')
    const client = new DynamicBondingCurveClient(conn, 'confirmed')
    const creator = new PublicKey(inp.creator)
    const config = new PublicKey(inp.config)
    const baseMint = Keypair.generate()
    const devBuy = new BN(String(Math.max(0, Math.floor(Number(inp.devBuyLamports || 0)))))
    const fee = Math.max(0, Math.floor(Number(inp.feeLamports || 0)))
    const RENT = 30_000_000 // ~0.03 SOL: new token + pool accounts + network fees
    const needed = Number(devBuy.toString()) + fee + RENT
    const sol = (l) => (l / 1e9).toFixed(3).replace(/\.?0+$/, '')
    const costText = `You'll pay about ${sol(needed)} SOL: ` +
        (devBuy.gtn(0) ? `${sol(Number(devBuy.toString()))} SOL dev buy + ` : '') +
        `${sol(fee)} SOL launch fee + ~0.03 SOL network rent/fees.`
    if (!inp.skipBalanceCheck) {
        const bal = await conn.getBalance(creator)
        if (bal < needed) {
            throw new Error(`This launch needs about ${sol(needed)} SOL ` +
                `(${devBuy.gtn(0) ? sol(Number(devBuy.toString())) + ' dev buy + ' : ''}${sol(fee)} launch fee + ~0.03 rent/fees). ` +
                `Your wallet has ${sol(bal)} SOL — add SOL or lower the dev buy.`)
        }
    }

    const tx = await client.creator.createPoolWithFirstBuy({
        createPoolParam: {
            baseMint: baseMint.publicKey, config,
            name: String(inp.name).slice(0, 32), symbol: String(inp.symbol).slice(0, 10), uri: inp.uri || '',
            payer: creator, poolCreator: creator,
        },
        // Same transaction as the pool creation, so nothing can buy before the creator.
        firstBuyParam: devBuy.gtn(0)
            ? { buyer: creator, receiver: creator, buyAmount: devBuy, minimumAmountOut: new BN(0), referralTokenAccount: null }
            : undefined,
    })
    if (inp.treasury && fee > 0) {
        tx.add(SystemProgram.transfer({ fromPubkey: creator, toPubkey: new PublicKey(inp.treasury), lamports: fee }))
    }
    tx.feePayer = creator
    tx.recentBlockhash = (await conn.getLatestBlockhash('confirmed')).blockhash
    tx.partialSign(baseMint) // the new token's mint key; the creator's wallet adds its signature
    const raw = tx.serialize({ requireAllSignatures: false, verifySignatures: false })
    const out = {
        tx_hex: Buffer.from(raw).toString('hex'),
        mint: baseMint.publicKey.toBase58(),
        pool: deriveDbcPoolAddress(NATIVE_MINT, baseMint.publicKey, config).toBase58(),
        size: raw.length,
        cost_text: costText,
        needed_lamports: needed,
    }
    if (inp.simulate) {
        const sim = await simulate(conn, tx)
        out.sim_err = sim.err || null
        out.sim_logs = (sim.logs || []).slice(-8)
    }
    process.stdout.write(JSON.stringify(out))
} catch (e) {
    process.stdout.write(JSON.stringify({ error: String(e && e.message ? e.message : e) }))
    process.exit(1)
}
