#!/usr/bin/env node
const { Connection, VersionedTransaction, Keypair } = require("@solana/web3.js");
const bs58 = require("bs58");

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function main() {
  const rpc = process.argv[2];
  const b64 = process.argv[3];
  const secret = process.env.FERZAN_SOL_KEY || "";
  if (!rpc || !b64 || !secret) {
    throw new Error("rpc, tx, and FERZAN_SOL_KEY required");
  }
  let kp;
  try {
    kp = Keypair.fromSecretKey(bs58.decode(secret));
  } catch (e) {
    kp = Keypair.fromSecretKey(Buffer.from(secret, "base64"));
  }
  const conn = new Connection(rpc, "confirmed");
  const tx = VersionedTransaction.deserialize(Buffer.from(b64, "base64"));
  const latest = await conn.getLatestBlockhash("confirmed");
  tx.message.recentBlockhash = latest.blockhash;
  tx.sign([kp]);
  let sig;
  try {
    sig = await conn.sendRawTransaction(tx.serialize(), {
      skipPreflight: false,
      maxRetries: 3,
    });
  } catch (e) {
    const m = String(e && e.message ? e.message : e);
    if (m.includes("0x7dc") || m.includes("custom program error")) {
      throw new Error("Quote rejected on-chain. Get a new quote and send within 20s. Keep ~0.02 SOL extra for fees.");
    }
    throw e;
  }
  for (let i = 0; i < 30; i++) {
    const st = await conn.getSignatureStatuses([sig], { searchTransactionHistory: true });
    const row = (st && st.value && st.value[0]) || null;
    if (row && row.err) {
      throw new Error("On-chain fail: " + JSON.stringify(row.err));
    }
    if (row && (row.confirmationStatus === "confirmed" || row.confirmationStatus === "finalized")) {
      process.stdout.write(sig);
      return;
    }
    const height = await conn.getBlockHeight("confirmed");
    if (height > latest.lastValidBlockHeight) {
      throw new Error("Tx dropped (blockhash expired). Get a new quote and send immediately.");
    }
    await sleep(400);
  }
  throw new Error("No confirmation in 12s. Tx did not land. Get a new quote.");
}

main().catch((err) => {
  process.stderr.write(String(err && err.message ? err.message : err));
  process.exit(1);
});
