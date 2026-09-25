// One-time: create Ferzan's Meteora DBC partner config.
//   node create_config.mjs plan   -> show settings + simulate (sends nothing)
//   node create_config.mjs send   -> create it on-chain (once)
import { Connection, PublicKey, LAMPORTS_PER_SOL, sendAndConfirmTransaction } from '@solana/web3.js'
import { NATIVE_MINT } from '@solana/spl-token'
import {
    DynamicBondingCurveClient, buildCurveWithMarketCap,
    ActivationType, BaseFeeMode, CollectFeeMode, MigrationFeeOption, MigrationOption,
    TokenDecimal, TokenType, TokenAuthorityOption,
} from '@meteora-ag/dynamic-bonding-curve-sdk'
import { loadEnv, loadOrCreateKey, simulate, KEYDIR } from './common.mjs'

const mode = process.argv[2] || 'plan'
const env = loadEnv()
const rpc = env.SOLANA_RPC_URL || 'https://api.mainnet-beta.solana.com'
const treasuryStr = (env.PLATFORM_TREASURY_SOL || env.TREASURY_SOL || '').trim()
if (!treasuryStr) { console.log('ABORT: PLATFORM_TREASURY_SOL is not set in /opt/ferzan/.env'); process.exit(1) }
const treasury = new PublicKey(treasuryStr)
const conn = new Connection(rpc, 'confirmed')
const client = new DynamicBondingCurveClient(conn, 'confirmed')
const payer = loadOrCreateKey('partner-payer.json')
const config = loadOrCreateKey('ferzan-config.json')

// ---- Ferzan launch economics (decided 2026-09-24) ----
const curve = buildCurveWithMarketCap({
    token: {
        tokenType: TokenType.SPLToken,
        tokenBaseDecimal: TokenDecimal.SIX,
        tokenQuoteDecimal: TokenDecimal.NINE,
        tokenAuthorityOption: TokenAuthorityOption.Immutable, // no mint authority: supply can never grow
        totalTokenSupply: 1_000_000_000,
        leftover: 10000,
    },
    fee: {
        baseFeeParams: {
            baseFeeMode: BaseFeeMode.FeeSchedulerLinear,
            // anti-sniper: 50% at launch, easing to 1% over 60 seconds
            feeSchedulerParam: { startingFeeBps: 5000, endingFeeBps: 100, numberOfPeriod: 60, totalDuration: 60 },
        },
        dynamicFeeEnabled: false,
        collectFeeMode: CollectFeeMode.QuoteToken, // fees paid in SOL
        creatorTradingFeePercentage: 50, // creator gets half, Ferzan half
        poolCreationFee: 0, // Ferzan's launch fee is a separate transfer in the launch tx
        enableFirstSwapWithMinFee: true, // creator's dev buy skips the anti-sniper fee
    },
    migration: {
        migrationOption: MigrationOption.MET_DAMM_V2,
        migrationFeeOption: MigrationFeeOption.FixedBps100,
        migrationFee: { feePercentage: 0, creatorFeePercentage: 0 },
    },
    liquidityDistribution: {
        partnerLiquidityPercentage: 0,
        partnerPermanentLockedLiquidityPercentage: 50,
        creatorLiquidityPercentage: 0,
        creatorPermanentLockedLiquidityPercentage: 50,
    },
    lockedVesting: {
        totalLockedVestingAmount: 0, numberOfVestingPeriod: 0, cliffUnlockAmount: 0,
        totalVestingDuration: 0, cliffDurationFromMigrationTime: 0,
    },
    activationType: ActivationType.Timestamp,
    initialMarketCap: 28,
    migrationMarketCap: 400,
})

const bal = await conn.getBalance(payer.publicKey)
const existing = await conn.getAccountInfo(config.publicKey)
console.log('RPC            :', rpc.replace(/api-key=[^&]+/, 'api-key=***'))
console.log('Payer wallet   :', payer.publicKey.toBase58(), `(${(bal / LAMPORTS_PER_SOL).toFixed(4)} SOL)`)
console.log('Fee receiver   :', treasury.toBase58(), '(your treasury)')
console.log('Config address :', config.publicKey.toBase58())
console.log('Graduates at   :', (Number(curve.migrationQuoteThreshold.toString()) / LAMPORTS_PER_SOL).toFixed(2), 'SOL raised')
console.log('Keys saved in  :', KEYDIR)
if (existing) {
    console.log('\nThe config ALREADY EXISTS on-chain. Put this in /opt/ferzan/.env:')
    console.log(`METEORA_CONFIG=${config.publicKey.toBase58()}`)
    process.exit(0)
}
if (bal < 0.02 * LAMPORTS_PER_SOL) {
    console.log('\nNEXT: send ~0.05 SOL to the payer wallet above (it pays the one-time account rent), then run plan again.')
    process.exit(0)
}

const tx = await client.partner.createConfig({
    config: config.publicKey,
    feeClaimer: treasury,
    leftoverReceiver: treasury,
    payer: payer.publicKey,
    quoteMint: NATIVE_MINT,
    ...curve,
})
tx.feePayer = payer.publicKey
tx.recentBlockhash = (await conn.getLatestBlockhash('confirmed')).blockhash

const sim = await simulate(conn, tx)
if (sim.err) {
    console.log('\nSIMULATION FAILED - nothing was sent:', JSON.stringify(sim.err))
    console.log((sim.logs || []).slice(-12).join('\n'))
    process.exit(1)
}
console.log('\nSimulation OK.')
if (mode !== 'send') {
    console.log('Nothing sent. Run:  node create_config.mjs send')
    process.exit(0)
}
const sig = await sendAndConfirmTransaction(conn, tx, [payer, config], { commitment: 'confirmed' })
console.log('CREATED:', `https://solscan.io/tx/${sig}`)
console.log(`\nPut this in /opt/ferzan/.env:\nMETEORA_CONFIG=${config.publicKey.toBase58()}`)
