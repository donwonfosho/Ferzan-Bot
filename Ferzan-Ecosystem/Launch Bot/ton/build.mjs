// Compiles contracts/ton/ferzan_curve.fc and writes build/ferzan_curve.b64 (the code cell) + prints its hash.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { compileFunc } from "@ton-community/func-js";
import { Cell } from "@ton/core";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "contracts", "ton", "ferzan_curve.fc"), "utf8");
const r = await compileFunc({ targets: ["ferzan_curve.fc"], sources: { "ferzan_curve.fc": src } });
if (r.status === "error") {
  console.error("COMPILE ERROR:\n" + r.message);
  process.exit(1);
}
fs.mkdirSync(path.join(here, "build"), { recursive: true });
fs.writeFileSync(path.join(here, "build", "ferzan_curve.b64"), r.codeBoc);
const cell = Cell.fromBase64(r.codeBoc);
console.log("compiled ok");
console.log("code hash:", cell.hash().toString("hex"));
console.log("code size:", Buffer.from(r.codeBoc, "base64").length, "bytes");
