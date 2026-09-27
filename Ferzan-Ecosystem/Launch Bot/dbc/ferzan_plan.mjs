// FERZAN flagship + fee-wallet configs for Meteora DBC.
//   node ferzan_plan.mjs          -> PLAN ONLY: build both configs, print every number, simulate. Sends nothing.
//   node ferzan_plan.mjs send     -> (a later batch turns this on after you approve the plan)
// Keys are created in /opt/ferzan/dbc-keys (outside git); only public addresses are printed.
import fs from 'fs'
import { Connection, PublicKey, LAMPORTS_PER_SOL } from '@solana/web3.js'
import { NATIVE_MINT } from '@solana/spl-token'
import * as DBC from '@meteora-ag/dynamic-bonding-curve-sdk'
import { loadEnv, loadOrCreateKey, simulate, KEYDIR } from './common.mjs'

const { DynamicBondingCurveClient, buildCurveWithMarketCap, ActivationType, BaseFeeMode, CollectFeeMode,
    MigrationFeeOption, MigrationOption, TokenDecimal, TokenType, TokenAuthorityOption } = DBC
const mode = process.argv[2] || 'plan'
if (mode !== 'plan') { console.log('This batch only plans. Nothing sent.'); process.exit(1) }
const env = loadEnv()
const rpc = env.SOLANA_RPC_URL || 'https://api.mainnet-beta.solana.com'
const treasury = new PublicKey((env.PLATFORM_TREASURY_SOL || env.TREASURY_SOL || '').trim())
const conn = new Connection(rpc, 'confirmed')
const client = new DynamicBondingCurveClient(conn, 'confirmed')
const payer = loadOrCreateKey('partner-payer.json')
const keeper = loadOrCreateKey('fee-keeper.json')          // claims Ferzan's fees daily (automation)
const stdCfg = loadOrCreateKey('ferzan-config-v2.json')     // everyday launches, fees to the keeper
const flagCfg = loadOrCreateKey('ferzan-flagship-config.json') // FERZAN only
const launcher = loadOrCreateKey('ferzan-launcher.json')    // creates the FERZAN pool, then hands creator to Squads

const common = (fee, lockedVesting, initialMarketCap, migrationMarketCap) => { try { return buildCurveWithMarketCap({
    token: { tokenType: TokenType.SPLToken, tokenBaseDecimal: TokenDecimal.SIX, tokenQuoteDecimal: TokenDecimal.NINE,
        tokenAuthorityOption: TokenAuthorityOption.Immutable, totalTokenSupply: 1_000_000_000, leftover: 10000 },
    fee,
    migration: { migrationOption: MigrationOption.MET_DAMM_V2, migrationFeeOption: MigrationFeeOption.FixedBps100,
        migrationFee: { feePercentage: 0, creatorFeePercentage: 0 } },
    liquidityDistribution: { partnerLiquidityPercentage: 0, partnerPermanentLockedLiquidityPercentage: 50,
        creatorLiquidityPercentage: 0, creatorPermanentLockedLiquidityPercentage: 50 },
    lockedVesting,
    activationType: ActivationType.Timestamp,
    initialMarketCap, migrationMarketCap,
}) } catch (e) { console.log('CURVE BUILD FAILED:', e && e.message ? e.message : e); process.exit(1) } }
const NO_VEST = { totalLockedVestingAmount: 0, numberOfVestingPeriod: 0, cliffUnlockAmount: 0, totalVestingDuration: 0, cliffDurationFromMigrationTime: 0 }

// 1) Everyday launches: exactly today's economics; only the fee claimer changes (to the keeper wallet).
const std = common({
    baseFeeParams: { baseFeeMode: BaseFeeMode.FeeSchedulerLinear,
        feeSchedulerParam: { startingFeeBps: 5000, endingFeeBps: 100, numberOfPeriod: 60, totalDuration: 60 } },
    dynamicFeeEnabled: false, collectFeeMode: CollectFeeMode.QuoteToken, creatorTradingFeePercentage: 50,
    poolCreationFee: 0, enableFirstSwapWithMinFee: true,
}, NO_VEST, 28, 400)

// 2) FERZAN: 650M locked (50M at graduation, 600M monthly for 24 months), big buys pay more for 30 minutes.
const SCHED = { startingFeeBps: 9900, endingFeeBps: 100, numberOfPeriod: 60, totalDuration: 1800 }
const VEST = { totalLockedVestingAmount: 650_000_000, numberOfVestingPeriod: 24, cliffUnlockAmount: 50_000_000,
    totalVestingDuration: 24 * 30 * 86400, cliffDurationFromMigrationTime: 0 }
const flag = common({
    baseFeeParams: { baseFeeMode: BaseFeeMode.FeeSchedulerExponential, feeSchedulerParam: SCHED },
    dynamicFeeEnabled: true, collectFeeMode: CollectFeeMode.QuoteToken, creatorTradingFeePercentage: 50,
    poolCreationFee: 0, enableFirstSwapWithMinFee: false,
}, VEST, 50, 800)

const sol = (bn) => (Number(bn.toString()) / LAMPORTS_PER_SOL).toFixed(2)
console.log('Keys folder      :', KEYDIR, '(never printed, never in git)')
console.log('Treasury         :', treasury.toBase58())
console.log('Fee keeper wallet:', keeper.publicKey.toBase58(), '(new: claims fees daily, forwards to treasury)')
console.log('FERZAN launcher  :', launcher.publicKey.toBase58(), '(new: creates the pool, then hands creator to Squads)')
console.log('Payer            :', payer.publicKey.toBase58(), `(${(await conn.getBalance(payer.publicKey) / LAMPORTS_PER_SOL).toFixed(4)} SOL)`)
console.log('\n== Everyday launches (config v2) ==')
console.log('Address          :', stdCfg.publicKey.toBase58())
console.log('Graduates at     :', sol(std.migrationQuoteThreshold), 'SOL raised (should match today: about 84)')
console.log('\n== FERZAN flagship ==')
console.log('Address          :', flagCfg.publicKey.toBase58())
console.log('Graduates at     :', sol(flag.migrationQuoteThreshold), 'SOL raised')
console.log('Market cap       : starts ~50 SOL, graduates ~800 SOL')
console.log('Locked           : 650,000,000 FERZAN (50,000,000 at graduation, then 25,000,000 a month for 24 months)')
console.log('Early protection : fee starts at 99% and falls every 30 s to 1% at 30 minutes; dynamic fee on')
for (const m of [0, 1, 2, 5, 10, 15, 20, 25, 30]) {
    const n = Math.min(60, Math.floor(m * 2)); const f = 99 * Math.pow(1 / 99, n / 60)
    console.log(`  minute ${String(m).padStart(2)}: fee about ${f.toFixed(1)}%`)
}
const dist = (() => { for (const p of ['@meteora-ag/dynamic-bonding-curve-sdk/dist/index.js', '@meteora-ag/dynamic-bonding-curve-sdk/dist/index.cjs']) {
    try { return fs.readFileSync(new URL('./node_modules/' + p, import.meta.url), 'utf8') } catch {} } return '' })()
console.log('\n== SDK checks ==')
console.log('transferPoolCreator :', typeof client.creator?.transferPoolCreator === 'function' ? 'available' : 'MISSING')
console.log('fee modes           :', Object.keys(BaseFeeMode).filter((k) => isNaN(Number(k))).join(', '))
console.log('delayed start option:', /activationPoint/.test(dist) ? 'mentioned in SDK (checking use next batch)' : 'none: the pool starts trading when it is created')

async function sim(label, cfgKey, curve, feeClaimer) {
    const tx = await client.partner.createConfig({ config: cfgKey.publicKey, feeClaimer, leftoverReceiver: treasury,
        payer: payer.publicKey, quoteMint: NATIVE_MINT, ...curve })
    tx.feePayer = payer.publicKey
    tx.recentBlockhash = (await conn.getLatestBlockhash('confirmed')).blockhash
    const r = await simulate(conn, tx)
    console.log(`${label.padEnd(20)}:`, r.err ? 'FAILED ' + JSON.stringify(r.err) + ' | ' + (r.logs || []).slice(-3).join(' | ') : 'OK')
}
console.log('\n== Simulations (nothing sent) ==')
if (await conn.getAccountInfo(stdCfg.publicKey)) console.log('config v2           : already exists')
else await sim('config v2', stdCfg, std, keeper.publicKey)
if (await conn.getAccountInfo(flagCfg.publicKey)) console.log('flagship config     : already exists')
else await sim('flagship config', flagCfg, flag, keeper.publicKey)
console.log('\nPLAN ONLY. Nothing was sent.')
