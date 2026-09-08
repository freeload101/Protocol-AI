import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import * as path from "node:path";

// Portable layout: everything lives next to this extension file.
//   kagi_search.py   <- invoked below (same folder)
//   CHROME_PORTABLE/ <- auto-installed by the .py if missing
declare const __dirname: string; // provided by jiti (verified)
const EXT_DIR = __dirname;
const SCRIPT_PATH = path.join(EXT_DIR, "kagi_search.py");
const PYTHON = process.env.PI_PYTHON || "python";

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "kagi_search",
    label: "Kagi Search",
    description:
      "Performs a Kagi search with query cleaning, two-pass search w/ " +
      "global dedup, relevance-filtered page fetching, noise stripping, " +
      "snippet extraction w/ context lines, and relevance-ranked output. " +
      "Returns Quick Answer summary → ranked reference snippets.\n\n" +
      "▸ NOTE: Only one search runs at a time — do not fire multiple searches in parallel. Wait for each result before issuing the next.\n\n" +
      "▸ ERROR TRIAGE PROTOCOL:\n" +
      "Classify every error **before** attempting a fix.\n\n" +
      "▸ FIX LOCALLY (¬search): syntax errors, missing files, basic perms, trivial 'cmd not found', git conflicts/merges.\n\n" +
      "▸ SEARCH FIRST (`kagi_search` required): dependency conflicts (ERESOLVE, peer deps, version mismatches), platform-specific errors (Win32 codes, macOS Gatekeeper, Linux seccomp), hex/status codes (`0x80070005`, `STATUS_*`), errors w/ <3 training-data examples, 3+ interdependent packages, native module build failures (`node-gyp`, `wheel`, `cargo`), fixes where 'obvious' path risks breaking other deps, user already tried `--force` or `--legacy-peer-deps`.\n\n" +
      "▸ DECISION RULE: uncertain? → SEARCH FIRST. 10s search > 20min hallucinated fix.\n\n" +
      "▸ SEARCH PROTOCOL:\n" +
      "1. Paste **full error output** as `search_query` (tool auto-strips PIDs/versions)\n" +
      "2. Read Quick Answer first — if comprehensive ⇒ apply that fix\n" +
      "3. Fetch ref pages only if Quick Answer is thin/vague\n" +
      "4. **Always cite** the source of your fix.",
    parameters: Type.Object({
      search_query: Type.String({
        description:
          "The raw error string or search query to send to Kagi. " +
          "Machine-unique identifiers (PIDs, timestamps, hex codes, " +
          "log-level prefixes) are automatically stripped before searching.",
      }),
      max_refs: Type.Optional(
        Type.Integer({
          description:
            "Max reference pages to fetch across both search passes. Default: 5",
          default: 5,
        })
      ),
      context_lines: Type.Optional(
        Type.Integer({
          description:
            "Lines of context before/after a matching line in snippet " +
            "extraction. Default: 15",
          default: 15,
        })
      ),
      max_chars: Type.Optional(
        Type.Integer({
          description:
            "Max chars per reference snippet after smart truncation. " +
            "Default: 3000",
          default: 3000,
        })
      ),
      verbose: Type.Optional(
        Type.Boolean({
          description: "Enable verbose logging to kagi_mcp.log.",
          default: false,
        })
      ),
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const args = [SCRIPT_PATH, params.search_query];

      if (params.max_refs !== undefined) {
        args.push("--max-refs", String(params.max_refs));
      }
      if (params.context_lines !== undefined) {
        args.push("--context-lines", String(params.context_lines));
      }
      if (params.max_chars !== undefined) {
        args.push("--max-chars", String(params.max_chars));
      }
      if (params.verbose) {
        args.push("--verbose");
      }

      const result = await pi.exec(PYTHON, args, { signal, cwd: EXT_DIR });

      const output = result.stdout || result.stderr || "No output returned.";

      // Safety-net truncation at paragraph boundary if output is massive.
      // The Python side already applies smart_truncate per reference, but
      // this catches pathological cases where concatenated output exceeds
      // the transport limit. We try to break at a paragraph boundary
      // rather than mid-word.
      const MAX_CHARS = 200_000;
      let text: string;

      if (output.length > MAX_CHARS) {
        const hardLimit = MAX_CHARS + 5_000;
        let cut = output.lastIndexOf("\n\n", hardLimit);
        if (cut < MAX_CHARS * 0.5) {
          // No good paragraph break — try sentence break
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
