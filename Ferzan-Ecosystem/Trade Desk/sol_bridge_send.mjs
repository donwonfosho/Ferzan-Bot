#!/usr/bin/env node
import { Connection, VersionedTransaction, Keypair } from "@solana/web3.js";
import bs58 from "bs58";

const rpc = process.argv[2];
const b64 = process.argv[3];
const secret = process.env.FERZAN_SOL_KEY || "";
if (!rpc || !b64 || !secret) {
  console.error("rpc, tx, and FERZAN_SOL_KEY required");
  process.exit(1);
}

let kp;
try {
  kp = Keypair.fromSecretKey(bs58.decode(secret));
} catch {
  kp = Keypair.fromSecretKey(Buffer.from(secret, "base64"));
}

const conn = new Connection(rpc, "confirmed");
const tx = VersionedTransaction.deserialize(Buffer.from(b64, "base64"));
const latest = await conn.getLatestBlockhash("confirmed");
tx.message.recentBlockhash = latest.blockhash;
tx.sign([kp]);
const sig = await conn.sendRawTransaction(tx.serialize(), {
  skipPreflight: false,
  maxRetries: 3,
});
process.stdout.write(sig);
