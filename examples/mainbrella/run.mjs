// Keep credentials in the child environment, never in command arguments or output.
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const argv = process.argv.slice(2);
const local = argv[0] === "--local";
if (local) argv.shift();
const key = local ? process.env.LOCAL_MAINBRELLA_API_KEY : process.env.MAINBRELLA_API_KEY;
if (!key) throw new Error(local ? "LOCAL_MAINBRELLA_API_KEY is required" : "MAINBRELLA_API_KEY is required");
const child = spawn(process.env.MAINBRELLA_PROTOTYPE_PYTHON || "python3", ["verify.py", ...argv], {
  cwd: fileURLToPath(new URL(".", import.meta.url)),
  stdio: "inherit",
  env: { ...process.env, MAINBRELLA_API_KEY: key,
    MAINBRELLA_API_URL: local ? "http://localhost:8787" : (process.env.MAINBRELLA_API_URL || "https://api.mainbrella.com") },
});
child.on("error", () => { console.error("Unable to start prototype Python environment"); process.exitCode = 1; });
child.on("exit", (code) => { process.exitCode = code ?? 1; });
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill(signal));
