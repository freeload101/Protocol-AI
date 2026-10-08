# 🧜 Mermaid.js — Complete AI Skill Reference
 
## What This Does

Validates every ````mermaid` code block in a markdown file using two tiers:
1. **Fast** — `mermaid.parse()` (pure JS, no browser) — catches syntax errors
2. **Strict** — `@mermaid-js/mermaid-cli` (full chromium renderer) — catches rendering errors the parser misses

## Prerequisites

```bash
# One-time setup
npm install mermaid          # pure JS parser (fast tier)
npx @mermaid-js/mermaid-cli --version  # CLI renderer (strict tier)

# CLI needs a chromium binary. Point it at Playwright's:
export PUPPETEER_EXECUTABLE_PATH=$(find ~/.cache/ms-playwright -name chrome-headless-shell -type f | head -1)
```

## Usage

### Quick check (parser only, no browser)

```bash
node -e "
const mermaid = require('mermaid').default;
mermaid.initialize({ startOnLoad: false });
const fs = require('fs');
const blocks = fs.readFileSync(process.argv[1], 'utf8')
  .split('\`\`\`mermaid\n').slice(1)
  .map(b => b.split('\n\`\`\`')[0].trim());
blocks.forEach((d, i) => {
  try {
    const r = mermaid.parse(d);
    console.log(r && r.error ? '✗ Block ' + (i+1) + ': ' + r.error : '✓ Block ' + (i+1));
  } catch(e) { console.log('✗ Block ' + (i+1) + ': ' + e.message); }
});
" YOUR_FILE.md
```

### Strict check (full renderer)

```bash
export PUPPETEER_EXECUTABLE_PATH=/path/to/chrome-headless-shell

# Extract blocks to temp files
node -e "
const fs = require('fs');
const blocks = fs.readFileSync(process.argv[1], 'utf8')
  .match(/\\\`\`\`mermaid\\n([\\s\\S]*?)\\n\\\`\`\`/g) || [];
blocks.forEach((b, i) => {
  const d = b.replace(/\\\`\`\`mermaid\\n/, '').replace(/\\n\\\`\`\`/, '');
  fs.writeFileSync('/tmp/mm' + (i+1) + '.mmd', d.trim());
});
console.log(blocks.length + ' blocks extracted');
" YOUR_FILE.md

# Validate each one
for i in $(seq 1 $NUM_BLOCKS); do
  echo -n \"Block \$i: \"
  npx @mermaid-js/mermaid-cli -t neutral -i /tmp/mm\${i}.mmd -o /tmp/mm\${i}.svg -q 2>/dev/null \
    && echo OK || echo FAIL
done
```

## Common Errors & Fixes

| Error Pattern | Cause | Fix |
|---|---|---|
| `Note over A,B:` fails in sequenceDiagram | Comma = range syntax, not supported in mermaid 11.x | Use `Note right of A:` or replace with message arrow `A->>A: (note text)` |
| `{text}` in flowchart node label | Curly braces reserved for decision/diamond nodes | Replace `{name}` → `NAME` or use quotes `\"{name}\"` |
| `\r\n\t` or `\"` in node labels | Backslash = escape char, breaks parser | Replace with plain English: `Strip newlines to spaces` |
| `Parse error on line N: got 'NEWLINE'` | Missing arrow or malformed edge | Check for dangling lines, missing `-->` |
| `subgraph` without label | mermaid 11.x requires subgraph ID | Use `subgraph NAME[\"Display Name\"]` |
| `style` on undefined node | Node ID mismatch or typo | Verify node IDs match exactly (case-sensitive) |

## Known Gotchas

- **`mermaid.parse()` is lenient** — it passes diagrams that fail to render. Always run the strict CLI check for final validation.
- **`@mermaid-js/mermaid-cli` needs chromium** — not just any chrome. Use Playwright's `chrome-headless-shell` from `~/.cache/ms-playwright/`.
- **SequenceDiagram `Note` syntax changed** in mermaid 10→11. `Note over A,B:` → broken. `Note right of A:` → works.
- **Flowchart curly braces** `{}` create decision nodes. To display literal `{}` in a label, escape or avoid.
- **Arrow labels with pipes** `|text|` work in flowchart but NOT in sequenceDiagram messages.

## Validation Script (Reusable)

Save as `validate-mermaid.js` in any project:

```javascript
#!/usr/bin/env node
const fs = require('fs');
const mermaid = require('mermaid').default;
mermaid.initialize({ startOnLoad: false });

const file = process.argv[2] || 'README.md';
const content = fs.readFileSync(file, 'utf8');
const blocks = content.split('\`\`\`mermaid\n').slice(1);

let pass = 0, fail = 0;
for (let i = 0; i < blocks.length; i++) {
  const diagram = blocks[i].split('\n\`\`\`')[0].trim();
  try {
    const result = mermaid.parse(diagram);
    if (result && result.error) {
      console.log(\`✗ Block \${i+1}: \${result.error}\`);
      fail++;
    } else {
      console.log(\`✓ Block \${i+1}\`);
      pass++;
    }
  } catch (e) {
    console.log(\`✗ Block \${i+1}: \${e.message}\`);
    fail++;
  }
}
console.log(\`\n\${pass} passed, \${fail} failed out of \${blocks.length}\`);
process.exit(fail > 0 ? 1 : 0);
```

Run: `node validate-mermaid.js YOUR_FILE.md`

## Decision Flow

```
Markdown file with mermaid blocks
  │
  ├─ npm install mermaid
  │   └─ node validate-mermaid.js FILE.md
  │       ├─ All pass → likely OK (but parser is lenient)
  │       └─ Failures → fix syntax, re-run
  │
  └─ npx @mermaid-js/mermaid-cli (strict)
      ├─ All render → diagrams are production-ready
      └─ Failures → fix rendering issues (labels, Notes, braces)
```
