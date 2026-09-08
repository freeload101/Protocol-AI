import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import * as path from "node:path";

// Portable layout: everything lives next to this extension file.
//   scrape_url.py    <- invoked below (same folder)
//   CHROME_PORTABLE/ <- auto-installed by the .py if missing
declare const __dirname: string; // provided by jiti (verified)
const EXT_DIR = __dirname;
const SCRIPT_PATH = path.join(EXT_DIR, "scrape_url.py");
const PYTHON = process.env.PI_PYTHON || "python";

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "scrape_url",
    label: "Scrape URL",
    description: "Scrape URL→markdown (noise stripped).",
    parameters: Type.Object({
      url: Type.String({ description: "URL" }),
      max_chars: Type.Optional(
        Type.Integer({
          description: "Max output chars. Default: 15000",
          default: 15000,
        })
      ),
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const args = [SCRIPT_PATH, params.url];

      if (params.max_chars !== undefined) {
        args.push("--max-chars", String(params.max_chars));
      }

      const result = await pi.exec(PYTHON, args, { signal, cwd: EXT_DIR });

      const output = result.stdout || result.stderr || "No output returned.";

      // Safety-net truncation at paragraph boundary.
      const MAX_CHARS = 200_000;
      let text: string;

      if (output.length > MAX_CHARS) {
        const hardLimit = MAX_CHARS + 5_000;
        let cut = output.lastIndexOf("\n\n", hardLimit);
        if (cut < MAX_CHARS * 0.5) {
          cut = output.lastIndexOf(". ", hardLimit);
        }
        if (cut >= MAX_CHARS * 0.5) {
          text = output.slice(0, cut) + "\n\n[...truncated]";
        } else {
          text = output.slice(0, MAX_CHARS) + "\n\n[...truncated]";
        }
      } else {
        text = output;
      }

      return {
        content: [{ type: "text", text }],
        details: { exitCode: result.code },
      };
    },
  });
}
