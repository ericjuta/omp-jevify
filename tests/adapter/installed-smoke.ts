import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

// Deliberately separate from `bun test`: packages the candidate and starts a fresh SDK-loader process.
const root = path.resolve(import.meta.dir, "../..");
const temporary = await mkdtemp(path.join(tmpdir(), "jevify-installed-smoke-"));
async function run(command: string[], cwd: string): Promise<string> {
  const child = Bun.spawn(command, { cwd, stdout: "pipe", stderr: "pipe", env: {
    PATH: process.env.PATH, HOME: temporary, TMPDIR: temporary,
    PI_CODING_AGENT_DIR: path.join(temporary, "isolated-host"),
  } });
  const [stdout, stderr, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
  if (code !== 0) throw new Error(`Provider-free smoke child exited ${code}: ${stderr}`);
  return stdout;
}
try {
  await run([process.execPath, "pm", "pack", "--destination", temporary], root);
  const archive = (await readdir(temporary)).find(name => name.endsWith(".tgz"));
  if (!archive) throw new Error("Package archive missing");
  await run(["tar", "-xzf", path.join(temporary, archive), "-C", temporary], root);
  const output = await run([process.execPath, "run", path.join(import.meta.dir, "installed-worker.ts"), path.join(temporary, "package"), temporary, path.join(root, "evals/preparation.py")], temporary);
  console.log(output.trim());
} finally { await rm(temporary, { recursive: true, force: true }); }
