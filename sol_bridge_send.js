#!/usr/bin/env node
/**
 * Stamp a fresh blockhash, sign with the desk key, broadcast.
 * argv: <rpc> <txBase64>
 * env: FERZAN_SOL_KEY  base58 secret
 */
const { Connection, VersionedTransaction, Keypair } = require("@solana/web3.js");
const bs58 = require("bs58");

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
    const raw = Buffer.from(secret, "base64");
    kp = Keypair.fromSecretKey(raw);
  }
  const conn = new Connection(rpc, "confirmed");
  const raw = Buffer.from(b64, "base64");
  const tx = VersionedTransaction.deserialize(raw);
  const latest = await conn.getLatestBlockhash("confirmed");
  tx.message.recentBlockhash = latest.blockhash;
  tx.sign([kp]);
  const sig = await conn.sendRawTransaction(tx.serialize(), {
    skipPreflight: false,
    maxRetries: 3,
  });
  process.stdout.write(sig);
}

main().catch((err) => {
  process.stderr.write(String(err && err.message ? err.message : err));
  process.exit(1);
});
