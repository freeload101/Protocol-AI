# PI Agent Setup Handover Document

> **Author:** @freeload101  
> **Date:** 2026-07-14  
> **PI Agent Version:** 0.80.3 (latest upstream: 0.80.6)  
> **Repo:** [earendil-works/pi](https://github.com/earendil-works/pi)

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Model Configuration](#2-model-configuration)
3. [Agent Settings](#3-agent-settings)
4. [Custom System Prompt — Compression Pipeline](#4-custom-system-prompt--compression-pipeline)
5. [Custom Extensions](#5-custom-extensions)
6. [Project-Specific SYSTEM.md (llama.cpp)](#6-project-specific-systemmd-llamacpp)
7. [Skills](#7-skills)
8. [Comparison: Current vs. Latest Upstream](#8-comparison-current-vs-latest-upstream)
9. [Restore Instructions](#9-restore-instructions)

---

## 1. Architecture Overview

```
C:\delete\PI_OMBI\
├── beellama.cpp/                  # llama.cpp working tree
│   └── .pi/gg/SYSTEM.md          # Project-level system prompt
├── .pi/agent/                     # PI Agent installation root
│   ├── npm/node_modules/          # NPM packages
│   ├── extensions/                 # Custom extensions (.ts files)
│   ├── models.json                 # Model provider config
│   ├── settings.json               # Agent settings
│   └── skills/                     # Skill definitions
└── PI_COMPRESS/                   # Backup of all customizations
    ├── 01-core-prompt-engine/     # system-prompt.js patch
    ├── 02-tool-definitions/       # edit.js patch
    ├── 04-extension-tools/        # kagi_search.ts, scrape_url.ts
    └── 05-config-and-settings/    # models.json, settings.json
```

**Key design decision:** All customizations are backed up in `PI_COMPRESS/` with automated restore scripts (`restore.bat` / `restore.sh`).

---

## 2. Model Configuration

### models.json

```json
{
    "providers": {
        "lmstudio": {
            "baseUrl": "http://localhost:1234/v1",
            "api": "openai-completions",
            "apiKey": "sk-lm-*****************REDACTED***************",
            "compat": {
                "supportsDeveloperRole": false,
                "supportsReasoningEffort": false
            },
            "models": [
                {
                    "id": "qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved",
                    "contextWindow": 65536
                }
            ]
        }
    }
}
```

**Notes:**
- **Provider:** LM Studio (local inference via OpenAI-compatible API)
- **Model:** Qwen 3.6 (27B) — uncensored, heretic v2, native MTP, preserved
- **Context Window:** 65,536 tokens
- **Compatibility:** `supportsDeveloperRole: false` means `developer` role messages map to `system`; `supportsReasoningEffort: false` controls `<thinking>` block emission

---

## 3. Agent Settings

### settings.json

```json
{
  "lastChangelogVersion": "0.80.3",
  "defaultProvider": "lmstudio",
  "defaultModel": "qwen3.6-27b-uncensored-heretic-v2-native-mtp-preserved",
  "packages": [
    "npm:pi-better-compact"
  ],
  "compaction": {
    "enabled": false,
    "reserveTokens": 16384,
    "keepRecentTokens": 15000
  },
  "theme": "dark"
}
```

**Key settings:**
| Field | Value | Effect |
|-------|-------|--------|
| `lastChangelogVersion` | `0.80.3` | Version seen at last startup |
| `defaultProvider` | `lmstudio` | Fallback model provider |
| `defaultModel` | `qwen3.6-27b...` | Fallback model ID |
| `packages` | `npm:pi-better-compact` | Installed npm extension for improved compaction |
| `compaction.enabled` | `false` | Automatic context trimming **disabled** (agent must use full context) |
| `compaction.reserveTokens` | `16384` | Tokens reserved for output when compaction fires |
| `compaction.keepRecentTokens` | `15000` | Most recent N tokens always kept during compaction |
| `theme` | `dark` | TUI theme |

---

## 4. Custom System Prompt — Compression Pipeline

**Location:** Injected into `.pi/agent/npm/node_modules/@earendil-works/pi-coding-agent/dist/core/system-prompt.js`

A 5-stage output compression pipeline is prepended to the default identity block:

```
You are a high-efficiency response engine. ALL output you generate MUST be compressed using the following pipeline before delivery. No exceptions.

▸ OUTPUT COMPRESSION PIPELINE (Apply to Every Response)

STAGE 1 -- BUDGET ALLOCATION: Before writing, allocate your output budget:
  Core answer: 60% | Supporting context: 25% | Qualifications: 15%
  Ruthlessly cut anything that does not earn its token budget.

STAGE 2 -- MARGINAL INFORMATION GAIN FILTER (COMI): For every sentence, ask: "Does this add information not already implied?" YES→keep | NO→discard
  Apply coarse-to-fine: score paragraph blocks → discard low-gain blocks → within survivors, score sentences → remove redundant clauses.

STAGE 3 -- SYMBOLIC ENCODING: Replace verbose patterns with symbols where meaning is preserved:
  "If X then Y"→X⇒Y | "leads to"→→ | "and required"→∩ | "or acceptable"→∪ | "belongs to"→∈
  "Not X"→¬X | "For all"→∀ | "Exists"→∃ | "approximately"→≈ | "therefore"→∴ | "because"→∵

STAGE 4 -- TOKEN-LEVEL PRUNING: Remove near-zero-value tokens:
  Filler phrases ("It is important to note", "As mentioned above", "In conclusion")
  Redundant qualifiers ("very", "quite", "rather", "basically")
  Restated context | Transitional summaries restating what was just said

STAGE 5 -- STRUCTURED DELIVERY FORMAT: Always deliver output in this compressed structure:
  ▸ ANSWER: [Direct answer, max 2 sentences]
  ▸ KEY POINTS: [Point N | only if marginal_gain > 0]
  ▸ DETAIL: [Only if user explicitly needs depth -- apply full pipeline above]
  ▸ ∴ [One-line conclusion or next action if applicable]

COMPRESSION TARGETS: Aim for 60-80% token reduction vs. uncompressed response.
  Quality preservation: >90% informational content retained. ¬ sacrifice accuracy for brevity -- flag uncertainty explicitly as [?].
```

**Backup file:** `PI_COMPRESS/01-core-prompt-engine/system-prompt.js`

---

## 5. Custom Extensions

### 5.1 kagi_search.ts — Internet Search / Code Fix

| Field | Value |
|-------|-------|
| **Name** | `kagi_search` |
| **Label** | Kagi Search |
| **Script Path** | `C:\backup\JAMBO_PI\node\kagi_search.py` |
| **Source** | [kagi_search.py](https://github.com/freeload101/Protocol-AI/blob/main/NotAPI/kagi_search.py) |
| **Linux Variant** | [kagi_search_LINUX.py](https://github.com/freeload101/Protocol-AI/blob/main/NotAPI/kagi_search_LINUX.py) |
| **Backup** | `PI_COMPRESS/04-extension-tools/kagi_search.ts` |

**Description:** Performs a Kagi search with query cleaning, two-pass search with global dedup, relevance-filtered page fetching, noise stripping, snippet extraction with context lines, and relevance-ranked output. Returns Quick Answer summary → ranked reference snippets.

**Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `search_query` | String | — | Raw error string or search query (auto-strips PIDs/timestamps/hex codes) |
| `max_refs` | Integer | 5 | Max reference pages to fetch |
| `context_lines` | Integer | 15 | Context lines before/after snippet |
| `max_chars` | Integer | 3000 | Max chars per reference snippet |
| `verbose` | Boolean | false | Enable verbose logging to `kagi_mcp.log` |

**Error Triage Protocol** (embedded in tool description):
```
▸ ERROR TRIAGE PROTOCOL:
Classify every error **before** attempting a fix.

▸ FIX LOCALLY (¬search): syntax errors, missing files, basic perms, trivial 'cmd not found', git conflicts/merges.

▸ SEARCH FIRST (`kagi_search` required): dependency conflicts (ERESOLVE, peer deps, version mismatches), platform-specific errors (Win32 codes, macOS Gatekeeper, Linux seccomp), hex/status codes (`0x80070005`, `STATUS_*`), errors w/ <3 training-data examples, 3+ interdependent packages, native module build failures (`node-gyp`, `wheel`, `cargo`), fixes where 'obvious' path risks breaking other deps, user already tried `--force` or `--legacy-peer-deps`.

▸ DECISION RULE: uncertain? → SEARCH FIRST. 10s search > 20min hallucinated fix.

▸ SEARCH PROTOCOL:
1. Paste **full error output** as `search_query` (tool auto-strips PIDs/versions)
2. Read Quick Answer first — if comprehensive ⇒ apply that fix
3. Fetch ref pages only if Quick Answer is thin/vague
4. **Always cite** the source of your fix.
```

---

### 5.2 scrape_url.ts — URL Scraper

| Field | Value |
|-------|-------|
| **Name** | `scrape_url` |
| **Label** | Scrape URL |
| **Script Path** | `C:\backup\JAMBO_PI\node\scrape_url.py` |
| **Source** | [scrape_url.py](https://github.com/freeload101/Protocol-AI/blob/main/NotAPI/scrape_url.py) |
| **Backup** | `PI_COMPRESS/04-extension-tools/scrape_url.ts` |

**Description:** Scrape URL→markdown (noise stripped).

**Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | String | — | Target URL |
| `max_chars` | Integer | 15000 | Max output chars |

---

 

---

## 7. Skills

### 7.1 Built-in Skills (from upstream repo)

The latest upstream repo (`C:\delete\pi\.pi\skills\`) includes:

| Skill | Description |
|-------|-------------|
| `add-llm-provider` | Checklist for adding a new LLM provider to `packages/ai`. Covers core types, provider implementation, lazy registration, model generation, test matrix, coding-agent wiring, and docs. |

### 7.2 Installed npm Skills

| Package | Description |
|---------|-------------|
| `npm:pi-better-compact` | Improved context compaction algorithm (installed via `settings.json` → `packages` array) |

 

## 8. Comparison: Current vs. Latest Upstream

| Aspect | Current (0.80.3) | Latest (0.80.6) | Delta |
|--------|-------------------|-------------------|-------|
| **Version** | 0.80.3 | 0.80.6 | 3 minor versions behind |
| **New in 0.80.6** | — | Added `max` model thinking level after `xhigh` | New thinking granularity |
| **New in 0.80.4** | — | Configurable harness session context entry transforms, custom metadata in JSONL headers, exported `InMemorySessionStorage`/`JsonlSessionStorage` | Session storage flexibility |
| **Bug fixes in 0.80.4** | — | Fixed split-turn compaction serialization, tool call failures on truncated messages, null content normalization, shell timeout validation, session ID generation | Stability improvements |
| **Extensions** | 2 custom (kagi_search, scrape_url) | 4 upstream (import-repro, prompt-url-widget, redraws, tps) | Custom extensions not in upstream |
| **System Prompt** | Custom 5-stage compression pipeline | Default identity block | Custom pipeline not in upstream |
| **Compaction** | Disabled | Default enabled | Custom setting |
| **Model** | qwen3.6-27b (LM Studio) | — | Local inference setup |

**Upstream extensions NOT in current setup:**
- `import-repro.ts` — Import CI issue-analysis sessions from gist/issue URLs
- `prompt-url-widget.ts` — TUI widget for PR/Issue/Advisory URLs with GitHub metadata
- `redraws.ts` — `/tui` command to show TUI full redraw stats
- `tps.ts` — Tokens-per-second notification after each agent turn

---
 

### Option B: Manual File Copy
| Backup File | Destination |
|-------------|-------------|
| `01-core-prompt-engine/system-prompt.js` | `.pi/agent/npm/node_modules/@earendil-works/pi-coding-agent/dist/core/` |
| `02-tool-definitions/edit.js` | `.pi/agent/npm/node_modules/@earendil-works/pi-coding-agent/dist/core/tools/` |
| `04-extension-tools/kagi_search.ts` | `.pi/agent/extensions/` |
| `04-extension-tools/scrape_url.ts` | `.pi/agent/extensions/` |
| `05-config-and-settings/models.json` | `.pi/agent/models.json` |
| `05-config-and-settings/settings.json` | `.pi/agent/settings.json` |

### Verification Checklist
- [ ] PI Agent starts without errors
- [ ] Responses use `▸ ANSWER / ▸ KEY POINTS` format
- [ ] `kagi_search` tool available with Error Triage Protocol
- [ ] `scrape_url` tool available for URL→markdown conversion
- [ ] Compaction disabled (no automatic context trimming)

---

## Appendix: Custom Extension Source Links

| Extension | GitHub Source |
|-----------|---------------|
| kagi_search.py | [Protocol-AI/NotAPI/kagi_search.py](https://github.com/freeload101/Protocol-AI/blob/main/NotAPI/kagi_search.py) |
| kagi_search_LINUX.py | [Protocol-AI/NotAPI/kagi_search_LINUX.py](https://github.com/freeload101/Protocol-AI/blob/main/NotAPI/kagi_search_LINUX.py) |
| scrape_url.py | [Protocol-AI/NotAPI/scrape_url.py](https://github.com/freeload101/Protocol-AI/blob/main/NotAPI/scrape_url.py) |

---

*Document generated 2026-07-14. For questions, refer to `PI_COMPRESS/README.md` or the `restore.bat`/`restore.sh` scripts.*
