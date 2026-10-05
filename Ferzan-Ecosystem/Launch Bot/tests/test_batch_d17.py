import asyncio, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tron_launch as tl  # noqa: E402


class FakeProc:
    """A helper process that never answers."""
    def __init__(self, hang=True, out=b'{"ok": true}\n'):
        self.hang, self.out, self.returncode, self.killed = hang, out, None, 0
    async def communicate(self):
        if self.hang and not self.killed:
            await asyncio.sleep(3600)
        return self.out, b""
    async def wait(self):
        self.returncode = self.returncode if self.returncode is not None else -9
        return self.returncode
    def kill(self):
        self.killed += 1; self.returncode = -9


class TronRun(unittest.TestCase):
    def go(self, proc, timeout=0.05, grace=0.05, after=0.4):
        async def main():
            async def fake_exec(*a, **k): return proc
            with mock.patch.object(tl.asyncio, "create_subprocess_exec", fake_exec), mock.patch.object(tl, "REAP_GRACE_S", grace):
                res = await tl.run("send", {}, timeout=timeout)
                await asyncio.sleep(after)       # let the background reaper act
                return res
        return asyncio.run(main())

    def test_a_stuck_helper_is_killed_after_the_grace_period(self):
        p = FakeProc(); res = self.go(p)
        self.assertTrue(res["pending"])                  # caller still told "pending", as before
        self.assertEqual(p.killed, 1)

    def test_a_helper_that_finishes_within_grace_is_not_killed(self):
        p = FakeProc()
        async def late():
            await asyncio.sleep(0.1); p.hang = False
        async def main():
            async def fake_exec(*a, **k): return p
            with mock.patch.object(tl.asyncio, "create_subprocess_exec", fake_exec), mock.patch.object(tl, "REAP_GRACE_S", 5):
                asyncio.get_running_loop().create_task(late())
                p.communicate_real = p.communicate
                async def comm():
                    while p.hang: await asyncio.sleep(0.02)
                    return p.out, b""
                p.communicate = comm
                res = await tl.run("send", {}, timeout=0.05)
                await asyncio.sleep(0.4)
                return res
        res = asyncio.run(main())
        self.assertTrue(res["pending"]); self.assertEqual(p.killed, 0)

    def test_normal_answer_unchanged(self):
        res = self.go(FakeProc(hang=False), timeout=2)
        self.assertEqual(res, {"ok": True})

    def test_startup_failure_reports_cleanly(self):
        async def main():
            async def boom(*a, **k): raise OSError("nope")
            with mock.patch.object(tl.asyncio, "create_subprocess_exec", boom):
                return await tl.run("send", {})
        res = asyncio.run(main())
        self.assertFalse(res["ok"]); self.assertIn("could not start", res["error"])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class KeyFiles(unittest.TestCase):
    """common.mjs run for real under node, with a stand-in for @solana/web3.js and a temp key folder."""
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "keys").mkdir()
        mod = self.d / "node_modules/@solana/web3.js"; mod.mkdir(parents=True)
        (mod / "package.json").write_text('{"name":"@solana/web3.js","type":"module","main":"index.js","exports":"./index.js"}')
        (mod / "index.js").write_text(
            "import { randomBytes } from 'crypto'\n"
            "export class Keypair { constructor(b){ this.secretKey = b } static generate(){ return new Keypair(new Uint8Array(randomBytes(64))) }"
            " static fromSecretKey(b){ return new Keypair(b) } }\nexport class VersionedTransaction {}\n")
        src = (ROOT / "dbc/common.mjs").read_text().replace("'/opt/ferzan/dbc-keys'", f"'{self.d}/keys'")
        (self.d / "common.mjs").write_text(src)
        (self.d / "package.json").write_text('{"type":"module"}')

    def node(self, code):
        (self.d / "t.mjs").write_text("import * as c from './common.mjs'\n" + code)
        return subprocess.run(["node", "t.mjs"], cwd=self.d, capture_output=True, text=True, timeout=30)

    def test_load_key_never_creates(self):
        r = self.node("try { c.loadKey('fee-keeper.json'); console.log('LOADED') } catch (e) { console.log('ERR ' + e.message) }")
        self.assertIn("refusing to make a new one", r.stdout); self.assertFalse((self.d / "keys/fee-keeper.json").exists())

    def test_setup_scripts_can_create_once_and_reload_the_same_key(self):
        r = self.node("const a=c.loadOrCreateKey('k.json'), b=c.loadOrCreateKey('k.json'); console.log(Buffer.from(a.secretKey).equals(Buffer.from(b.secretKey)))")
        self.assertEqual(r.stdout.strip(), "true", r.stderr)
        self.assertEqual(oct((self.d / "keys/k.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual([p.name for p in (self.d / "keys").iterdir()], ["k.json"])    # no temp file left behind

    def test_damaged_key_file_is_never_replaced(self):
        for body in ("not json", "[1,2,3]", ""):
            (self.d / "keys/bad.json").write_text(body)
            for fn in ("loadKey", "loadOrCreateKey"):
                r = self.node(f"try {{ c.{fn}('bad.json'); console.log('LOADED') }} catch (e) {{ console.log('ERR ' + e.message) }}")
                self.assertIn("refusing to replace", r.stdout, (body, fn))
            self.assertEqual((self.d / "keys/bad.json").read_text(), body)

    def test_two_creators_racing_end_up_with_the_same_key(self):
        code = "const k=c.loadOrCreateKey('race.json'); console.log(Buffer.from(k.secretKey).toString('hex'))"
        (self.d / "t.mjs").write_text("import * as c from './common.mjs'\n" + code)
        procs = [subprocess.Popen(["node", "t.mjs"], cwd=self.d, stdout=subprocess.PIPE, text=True) for _ in range(6)]
        outs = {p.communicate()[0].strip() for p in procs}
        self.assertEqual(len(outs), 1, outs)
        saved = __import__("json").loads((self.d / "keys/race.json").read_text())
        self.assertEqual(bytes(saved).hex(), outs.pop())


class Wiring(unittest.TestCase):
    def test_runtime_scripts_never_create_keys(self):
        fly = (ROOT / "dbc/flywheel.mjs").read_text()
        self.assertNotIn("loadOrCreateKey", fly)
        launch = (ROOT / "dbc/ferzan_launch.mjs").read_text()
        self.assertIn("loadKey('ferzan-launcher.json')", launch)
        self.assertIn("loadKey('ferzan-flagship-config.json')", launch)
        self.assertIn("inp.mode === 'send' ? loadKey('ferzan-mint.json')", launch)

    def test_stale_test_and_doc_fixed(self):
        t = (ROOT / "test/LaunchToken.test.js").read_text()
        self.assertNotIn("renounceOwnership()", t); self.assertNotIn("token.owner()", t)
        self.assertNotIn("/opt/ferzan/app/launch\n", (ROOT / "DEPLOY_MAINNET.md").read_text())


if __name__ == "__main__":
    unittest.main()
