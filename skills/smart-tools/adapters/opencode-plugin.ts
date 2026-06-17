/**
 * smart-tools opencode plugin — hard-block slow disk-trawling.
 *
 * Install: symlink/copy to ~/.config/opencode/plugin/smart-tools.ts
 * (install.sh does this). It defers to the shared bin/guard.py so the policy
 * lives in exactly one place. Note: opencode's tool.execute.before does NOT
 * fire for sub-agent tool calls — the shell guard (bin/smart-tools.sh) is the
 * real safety net for those.
 */
import { execFileSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

const GUARD = join(homedir(), ".agents/skills/smart-tools/bin/guard.py");

export const SmartTools = async () => ({
  "tool.execute.before": async (input: any, output: any) => {
    const tool = String(input?.tool ?? "");
    if (!/bash|shell|exec/i.test(tool)) return;
    const command =
      output?.args?.command ?? output?.args?.cmd ?? input?.args?.command ?? "";
    if (!command) return;

    try {
      execFileSync("python3", [GUARD], {
        input: JSON.stringify({ tool_name: "Bash", tool_input: { command } }),
        stdio: ["pipe", "ignore", "pipe"],
      });
    } catch (e: any) {
      // guard.py exits 2 and prints the reason on stderr when it blocks.
      const reason = (e?.stderr?.toString() || "smart-tools: blocked slow command").trim();
      throw new Error(reason);
    }
  },
});

export default SmartTools;
