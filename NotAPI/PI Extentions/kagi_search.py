#!/usr/bin/env python3
"""kagi_search.py - Standalone Kagi search tool for pi agent (no MCP).

Refactored with token reduction and noise filtering:
  1. Query cleaning strips PIDs, timestamps, hex codes, log-level prefixes
  2. Two-pass search with global dedup and budget cap
  3. Pre-fetch relevance filter (relaxed for short queries)
  4. HTML tag stripping at conversion time + post-conversion noise removal
  5. Snippet extraction (leading + trailing context around matches)
  6. Smart truncation at paragraph/sentence boundaries
  7. Relevance-ranked output
"""

import asyncio
import random
import os
import json
import logging
import re
import subprocess
import time
import traceback
import sys
import argparse
import shutil

# ---------------------------------------------------------------------------
# Auto-install missing third-party deps.
# pkgs: list of (import_name, pip_requirement).
# pip output -> deps_install.log (stdout is sacred - it carries tool
# output to pi).
# ---------------------------------------------------------------------------
def _ensure_deps(pkgs):
    import importlib
    missing = []
    for imp, req in pkgs:
        try:
            importlib.import_module(imp)
        except ImportError:
            missing.append((imp, req))
    if not missing:
        return
    dep_log = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'deps_install.log')
    reqs = [req for _, req in missing]

    def _pip(extra):
        cmd = [sys.executable, '-m', 'pip', 'install',
               '--disable-pip-version-check', *extra, *reqs]
        with open(dep_log, 'a', encoding='utf-8') as lf:
            lf.write(f'\n=== pip install {reqs} {extra} '
                     f'{time.strftime("%Y-%m-%d %H:%M:%S")} ===\n')
            r = subprocess.run(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
            out = (r.stdout or b'').decode('utf-8', 'replace')
            lf.write(out)
            lf.flush()
        return r, out

    r, out = _pip([])
    if r.returncode != 0 and 'No module named pip' in out:
        subprocess.run([sys.executable, '-m', 'ensurepip', '--upgrade'],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        r, out = _pip([])
    if r.returncode != 0:
        r, out = _pip(['--user'])
    if r.returncode != 0:
        raise RuntimeError(
            f'Failed to install missing deps {reqs}. Try manually: '
            f'{sys.executable} -m pip install {" ".join(reqs)}\n'
            f'--- pip output tail ---\n{out[-2000:]}')
    for imp, _ in missing:
        try:
            importlib.import_module(imp)
        except ImportError as e:
            raise RuntimeError(
                f'Dep {imp!r} still missing after pip install: {e}. '
                f'Log: {dep_log}')


_ensure_deps([('nodriver', 'nodriver==0.47.0'),
              ('markdownify', 'markdownify==1.2.3')])

import nodriver  # noqa: E402  (import after _ensure_deps)
from markdownify import markdownify as md  # noqa: E402

# ---------------------------------------------------------------------------
# Portable layout - everything lives next to this script:
#   <SCRIPT_DIR>/CHROME_PORTABLE/   auto-installed if missing (_ensure_chrome)
#   <SCRIPT_DIR>/results/           per-run reference snippets
#   <SCRIPT_DIR>/kagi_mcp.log       debug log
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, 'results')
USER_DATA_DIR = os.path.join(SCRIPT_DIR, 'Default')
CHROME_PORTABLE_DIR = os.path.join(SCRIPT_DIR, 'CHROME_PORTABLE')
# Portable Chrome installer is inlined below (_install_portable_chrome) -
# no external installer file needed.

# Kagi search API token (edit here to rotate)
KAGI_TOKEN = 'REDACTEDREDACTEDREDACTEDREDACTEDREDACTEDREDACTED'
KAGI_SEARCH_URL = 'https://kagi.com/search?token=' + KAGI_TOKEN + '&q='

# Log to file - never to stdout (stdout carries tool output to pi)
logging.basicConfig(
    filename=os.path.join(SCRIPT_DIR, 'kagi_mcp.log'),
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger('kagi-mcp')


def _find_chrome():
    """Locate chrome.exe inside a CHROME_PORTABLE tree.

    Checks the script folder first, then the current working directory
    (so running this script directly from a folder that already has
    CHROME_PORTABLE works without a second install).
    """
    bases = [CHROME_PORTABLE_DIR]
    cwd_base = os.path.join(os.getcwd(), 'CHROME_PORTABLE')
    if os.path.normcase(os.path.abspath(cwd_base)) != os.path.normcase(
        os.path.abspath(CHROME_PORTABLE_DIR)
    ):
        bases.append(cwd_base)
    for base in bases:
        # Preferred: PAF layout (real chrome.exe, not the PAF launcher)
        preferred = os.path.join(base, 'App', 'Chromium', 'chrome.exe')
        if os.path.isfile(preferred):
            return preferred
        # Fallback: any chrome.exe anywhere under the portable tree
        for root, _dirs, files in os.walk(base):
            if 'chrome.exe' in files:
                return os.path.join(root, 'chrome.exe')
    return None


# ---------------------------------------------------------------------------
# Inline portable-Chrome installer
# (code inlined from install_portable_chrome.py - no external file needed)
#
# Downloads Chromium Portable from PortableApps.com and drives the PAF
# wizard via PowerShell UI Automation:
#   [language] OK -> [welcome] Next > -> [license] I Agree
#   -> [destination] type path + Install -> [done] Finish
# ---------------------------------------------------------------------------
INSTALLER_APP_PAGE = 'https://portableapps.com/apps/internet/chromium-portable'
_UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}


def _scrape_download_url(app_page):
    """Return (real_download_url, filename) of the latest .paf.exe build."""
    import urllib.request
    req = urllib.request.Request(app_page, headers=_UA)
    html = urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'ignore')
    # Primary download = first /downloading/ link whose file is a .paf.exe (skip Legacy builds)
    dl_href = fname = None
    pat = re.compile(r'href="(/downloading/\?[^\"]*?f=([A-Za-z0-9_.\-]+\.paf\.exe))"')
    for m in pat.finditer(html):
        url, f = m.group(1), m.group(2)
        if 'Legacy' in f:
            continue
        dl_href = 'https://portableapps.com' + url.replace(' ', '%20')
        fname = f
        break
    if not dl_href:
        raise RuntimeError('No .paf.exe download link found on page: ' + app_page)
    # The /downloading/ page is HTML that JS-redirects (window.location) to the real binary URL
    dhtml = urllib.request.urlopen(dl_href, timeout=30).read().decode('utf-8', 'ignore')
    m = re.search(r'window\.location\s*=\s*"([^"]+)"', dhtml)
    if not m:
        raise RuntimeError('Could not find redirect target in /downloading/ page')
    real = m.group(1)
    if real.startswith('/'):
        real = 'https://portableapps.com' + real
    return real, fname


def _download_paf(url, dest_path, emit):
    """Download the PAF installer (progress reported via emit)."""
    import urllib.request
    emit(f'Downloading: {url}')
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=900) as r, open(dest_path, 'wb') as f:
        total = int(r.headers.get('Content-Length', 0) or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                emit(f'  {done//1048576}MB / {total//1048576}MB ({done*100//total}%)')
    emit('Download complete')


_PS_DRIVER = r'''
param([string]$Installer, [string]$Dest)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class Win32 {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)]
  public static extern IntPtr SendMessage(IntPtr h, int m, IntPtr w, string l);
  [DllImport("user32.dll")]
  public static extern IntPtr SendMessage(IntPtr h, int m, IntPtr w, IntPtr l);
}
"@
$WM_SETTEXT = 0x000C
$BM_CLICK   = 0x00F5

function Find-El([string]$name, [int]$procid) {
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $c = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty, $name)
  $el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $c)
  if ($el -and $el.Current.ProcessId -eq $procid) { return $el }
  return $null
}
function Click-Name([string]$name, [int]$procid) {
  $el = Find-El $name $procid
  if ($el) {
    [Win32]::SendMessage([IntPtr]$el.Current.NativeWindowHandle, $BM_CLICK,
                         [IntPtr]0, [IntPtr]0) | Out-Null
    Write-Output "CLICKED [$name]"
    return $true
  }
  return $false
}
function Find-Edit([int]$procid) {
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $c = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit)
  foreach ($el in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $c)) {
    if ($el.Current.ProcessId -eq $procid -and -not $el.Current.IsOffscreen) { return $el }
  }
  return $null
}

$p = Start-Process -FilePath $Installer -PassThru
$procid = $p.Id
Write-Output "Installer PID: $procid"

# --- Step 1: language dialog -> OK ----------------------------------------- #
for ($i = 0; $i -lt 30; $i++) {
  if (Click-Name "OK" $procid) { break }
  Start-Sleep -Milliseconds 500
}

# --- Steps 2-5: main wizard ------------------------------------------------ #
$destSet = $false
for ($i = 0; $i -lt 900; $i++) {          # up to ~7.5 min (covers online download)
  if ($p.HasExited) { break }
  if (-not $destSet) {
    if (Click-Name "Next >" $procid)  { Start-Sleep -Milliseconds 700; continue }
    if (Click-Name "I Agree" $procid) { Start-Sleep -Milliseconds 700; continue }
    if (Find-El "Install" $procid) {
      $e = Find-Edit $procid
      if ($e) {
        [Win32]::SendMessage([IntPtr]$e.Current.NativeWindowHandle, $WM_SETTEXT,
                             [IntPtr]0, $Dest) | Out-Null
        Write-Output "DEST SET: $Dest"
        $destSet = $true
        Start-Sleep -Milliseconds 400
        Click-Name "Install" $procid | Out-Null
        Start-Sleep -Milliseconds 700
        continue
      }
    }
  }
  if (Click-Name "Finish" $procid) { Start-Sleep -Milliseconds 400; continue }
  Start-Sleep -Milliseconds 500
}

$p.WaitForExit()
Write-Output "Installer exited: $($p.ExitCode)"
if ($p.ExitCode -eq 0) { exit 0 } else { exit 1 }
'''


def _run_paf_installer(installer_path, dest, emit):
    """Launch the PAF installer and drive the wizard via UI Automation."""
    import tempfile
    fd, ps_path = tempfile.mkstemp(suffix='.ps1', prefix='paf_drv_')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(_PS_DRIVER)
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-File', ps_path, '-Installer', installer_path, '-Dest', dest],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in (r.stdout or '').splitlines():
            emit(line)
        return r.returncode == 0
    finally:
        os.unlink(ps_path)


def _find_launcher(dest):
    """Verify the PAF launcher exists under dest (dest or dest\\<AppName>)."""
    from pathlib import Path
    dest = Path(dest)
    candidates = list(dest.glob('*.exe'))
    for sub in sorted(dest.iterdir()):
        if sub.is_dir():
            candidates += list(sub.glob('*.exe'))
    for exe in candidates:
        if 'portable' in exe.name.lower():
            return exe
    return None


def _install_portable_chrome(dest_dir, emit):
    """Full install: scrape URL -> download -> drive wizard -> verify.

    dest_dir must be an absolute path. Raises RuntimeError on failure.
    """
    import tempfile
    url, fname = _scrape_download_url(INSTALLER_APP_PAGE)
    emit(f'Latest binary: {fname}')
    emit(f'Download URL : {url}')
    tmpdir = tempfile.mkdtemp(prefix='paf_dl_')
    installer = os.path.join(tmpdir, fname)
    _download_paf(url, installer, emit)
    os.makedirs(dest_dir, exist_ok=True)
    emit('Launching installer (wizard will be auto-driven)...')
    ok = _run_paf_installer(installer, dest_dir, emit)
    launcher = _find_launcher(dest_dir)
    if ok and launcher:
        emit(f'SUCCESS: {launcher}')
        try:
            os.unlink(installer)          # tidy up the downloaded installer
        except OSError:
            pass
    else:
        emit(f'FAILED: installer ok={ok}, '
             f'launcher={"found" if launcher else "MISSING"} under {dest_dir}')
        emit(f'(installer kept at: {installer})')
        raise RuntimeError('PAF installer did not complete successfully')


def _ensure_chrome():
    """Return path to chrome.exe, installing portable Chrome if missing."""
    found = _find_chrome()
    if found:
        return found
    install_log = os.path.join(SCRIPT_DIR, 'chrome_install.log')
    dest = os.path.abspath(CHROME_PORTABLE_DIR)
    log.info('CHROME_PORTABLE missing - installing portable Chrome (dest=%s)', dest)
    with open(install_log, 'a', encoding='utf-8') as lf:
        lf.write(
            f'\n=== install started {time.strftime("%Y-%m-%d %H:%M:%S")} ===\n'
        )
        lf.flush()

        def emit(msg=''):
            lf.write(msg + '\n')
            lf.flush()
            log.info(msg)

        try:
            _install_portable_chrome(dest, emit)
        except Exception as e:
            tail = ''
            try:
                with open(install_log, 'r', encoding='utf-8', errors='replace') as f:
                    tail = ''.join(f.readlines()[-20:])
            except Exception:
                pass
            raise RuntimeError(
                f'Portable Chrome install failed: {e}. Log: {install_log}\n'
                f'--- tail ---\n{tail}'
            )
    found = _find_chrome()
    if not found:
        raise RuntimeError(
            'Installer finished but chrome.exe not found under '
            f'{CHROME_PORTABLE_DIR}. Log: {install_log}'
        )
    log.info('Portable Chrome installed: %s', found)
    return found

# ---------------------------------------------------------------------------
# Constants for the refactored pipeline
# ---------------------------------------------------------------------------
NOISE_STRIP_TAGS = [
    'nav', 'footer', 'header', 'aside', 'script', 'style',
    'noscript', 'iframe', 'form', 'svg',
]

# Optimized JS extraction: target semantic content containers, strip noise
# in-browser before transfer → 40-70% HTML reduction
_JS_EXTRACT_CONTENT = """
    (() => {
        const targets = ['article', 'main', '[role=main]', '#content', '.content'];
        for (const sel of targets) {
            const el = document.querySelector(sel);
            if (el && el.innerHTML.length > 500) {
                for (const tag of ['nav','footer','header','aside','script','style','noscript','iframe','form','svg']) {
                    el.querySelectorAll(tag).forEach(t => t.remove());
                }
                return el.innerHTML;
            }
        }
        return document.body.innerHTML; // fallback
    })()
"""

# DOM readiness check: replaces fixed asyncio.sleep(10)
_JS_PAGE_READY = """
    (() => {
        return document.readyState === 'complete' && document.body.offsetHeight > 0;
    })()
"""

TOKEN_EXPANSIONS = {
    'geolocation': ['location', 'gps', 'position', 'permission', 'access'],
    'invalid': ['denied', 'failed', 'error', 'unavailable'],
    'handle': ['descriptor', 'reference', 'pointer', 'resource'],
    'access': ['permission', 'denied', 'unauthorized'],
    'failed': ['error', 'failure', 'unable', 'cannot'],
    'win': ['windows', 'win32'],
    'resolve': ['fix', 'install', 'compatibility'],
    'dependency': ['package', 'module'],
    'permission': ['denied', 'unauthorized', 'forbidden'],
    'dictionary': ['dict', 'map', 'hashmap'],
    'iteration': ['iterate', 'loop', 'traverse'],
    'lifetime': ['scope', 'valid', 'borrow', 'ownership'],
}


# ---------------------------------------------------------------------------
# 0. QA Context Token Extraction
# ---------------------------------------------------------------------------
_QA_TOKEN_PATTERNS = [
    re.compile(r'\b[A-Z][a-zA-Z]{2,}\b'),       # CamelCase/class names
    re.compile(r'\b[a-z_]{3,}\s*\('),            # function calls
    re.compile(r'0x[0-9A-Fa-f]+'),                # hex codes
    re.compile(r'[a-zA-Z_]+\.[a-zA-Z_]+\.[a-zA-Z_]+'),  # module paths
    re.compile(r'#[A-Fa-f0-9]{6}'),               # color hex
]


def extract_qa_context_tokens(qa_text: str) -> list:
    """Parse Quick Answer text for technical terms to guide reference scraping.

    Returns list of tokens extracted from QA text. Additive (OR logic) with
    query tokens in _find_matches().
    """
    if not qa_text:
        return []
    tokens = set()
    for pattern in _QA_TOKEN_PATTERNS:
        for match in pattern.finditer(qa_text):
            token = match.group(0).strip().lower()
            if len(token) > 3:
                tokens.add(token)
    return list(tokens)


# ---------------------------------------------------------------------------
# 0b. Dynamic Context Window
# ---------------------------------------------------------------------------
def _dynamic_context(query: str, qa_tokens: list) -> int:
    """Query-aware context window sizing.

    Formula: clamp((len(query_tokens) + len(qa_tokens)) * 2, 3, 10)
    Expected savings: 30-50% snippet reduction for short queries.
    """
    query_tokens = [t for t in query.split() if len(t) > 3]
    total = len(query_tokens) + len(qa_tokens or [])
    return max(3, min(10, total * 2))


# ---------------------------------------------------------------------------
# 0c. Dynamic Page Readiness
# ---------------------------------------------------------------------------
async def wait_for_page_ready(tab, max_wait=10, interval=0.5):
    """Replace fixed asyncio.sleep(10) with DOM readiness polling.

    Polls every 0.5s, max 10s timeout. Expected savings: 3-7s per page.
    """
    elapsed = 0
    while elapsed < max_wait:
        await asyncio.sleep(interval)
        elapsed += interval
        try:
            ready = await tab.evaluate(_JS_PAGE_READY)
            if ready:
                log.debug('Page ready after %.1fs', elapsed)
                return True
        except Exception:
            pass
    log.warning('Page readiness timeout after %.1fs, proceeding', elapsed)
    return True


# ---------------------------------------------------------------------------
# 1. Query Cleaning
# ---------------------------------------------------------------------------
def extract_search_query(raw_error: str) -> tuple:
    """Strip machine-unique identifiers from a raw error string.

    Returns (primary_query, fallback_query).
    """
    cleaned = raw_error

    # Remove PIDs/timestamps like [27560:21648:0611/235433.800:
    cleaned = re.sub(r'\[[\d:./\s]+', '[', cleaned)
    # Remove line numbers like :169]
    cleaned = re.sub(r':\d+\]', ']', cleaned)
    # Extract hex error codes before removing them (keep for fallback)
    hex_match = re.search(r'0x[0-9A-Fa-f]+', cleaned)
    cleaned = re.sub(r'\s*\(0x[0-9A-Fa-f]+\)', '', cleaned)
    # Remove repeated prefixes like "npm ERR!"
    cleaned = re.sub(
        r'^(\w+\s+(ERR|ERROR|WARN|WARNING|INFO)[!:]?\s*)+',
        '',
        cleaned,
        flags=re.MULTILINE,
    )
    # Remove standalone INFO/ERROR/WARNING prefix
    cleaned = re.sub(r'(INFO|ERROR|WARNING):', '', cleaned)
    # Collapse whitespace
    cleaned = ' '.join(cleaned.split())

    fallback = None
    if hex_match:
        words = [w for w in cleaned.split() if len(w) > 3][:3]
        fallback = hex_match.group(0) + ' ' + ' '.join(words)

    return cleaned.strip() or raw_error, fallback


# ---------------------------------------------------------------------------
# 3. Pre-Fetch Relevance Filter
# ---------------------------------------------------------------------------
def is_likely_relevant(link: dict, query: str) -> bool:
    """Check if a link title + snippet contain at least one meaningful
    token from the search query.

    For short queries (<=2 meaningful tokens), accept all links.
    Kagi already ranked these results - our token overlap test is
    unreliable with so few tokens.
    """
    tokens = set(
        t.lower() for t in re.split(r'[\s_\-:./\\]+', query) if len(t) > 3
    )
    if len(tokens) <= 2:
        return True
    text = (link.get('text', '') + ' ' + link.get('snippet', '')).lower()
    hits = sum(1 for t in tokens if t in text)
    return hits >= 1


# ---------------------------------------------------------------------------
# 4. Noise Stripping (post-markdown textual noise)
# ---------------------------------------------------------------------------
_NOISE_PATTERN = re.compile(
    '|'.join([
        r'^\s*(home|about|blog|contact|login|sign up|newsletter|subscribe)\s*$',
        r'cookie|privacy policy|terms of (use|service)|accept|gdpr|consent',
        r'share (this|on)|follow us|twitter|facebook|linkedin',
        r'advertisement|sponsored|affiliate',
        r'(c)\s*\d{4}|all rights reserved|powered by',
        r'^\s*[\*\-]{3,}\s*$',
        # Language switcher lines: "* [Language](https://...?hl=xx)"
        r'^\s*\*\s*\[[^\]]+\]\(https?://[^\)]*hl=[a-z]{2}',
        # Any line that is ONLY a markdown link with no surrounding text
        r'^\s*\*\s*\[[^\]]+\]\(https?://[^\)]+\)\s*$',
        # Bare link-only lines
        r'^\s*<?https?://\S+>?\s*$',
        # Social media nav links
        r'(github|twitter|facebook|linkedin|youtube|reddit)\.com/(share|intent|login)',
        # "Was this helpful?" / feedback prompts
        r'was this (article|page|helpful)',
        r'rate this|give feedback|report a bug',
        # Table of contents / jump links
        r'^\s*\*\s*\[.*\]\(#[^\)]*\)\s*$',
        # Edit on GitHub / improve this page
        r'edit (on|this)|improve this (page|article)',
        # Skip to main content / accessibility
        r'skip to (main|content)',
    ]),
    re.IGNORECASE,
)


def strip_noise(markdown_text: str) -> str:
    """Remove boilerplate lines from converted markdown."""
    lines = markdown_text.split('\n')
    result = []
    # Track consecutive link-only lines to detect language switcher blocks
    consecutive_links = 0
    for line in lines:
        is_link_only = bool(re.match(
            r'^\s*\*\s*\[[^\]]+\]\(https?://[^\)]+\)\s*$', line
        ))
        if is_link_only:
            consecutive_links += 1
        else:
            consecutive_links = 0

        # Drop blocks of 3+ consecutive link-only lines (language lists, etc.)
        if consecutive_links >= 3:
            # Also blank out the preceding link lines in this block
            if len(result) >= 2:
                for back in range(1, min(3, len(result) + 1)):
                    prev = result[-back] if back <= len(result) else ''
                    if re.match(
                        r'^\s*\*\s*\[[^\]]+\]\(https?://[^\)]+\)\s*$',
                        prev
                    ):
                        result[-back] = ''
            result.append('')
            continue

        if not _NOISE_PATTERN.search(line) and line.strip():
            result.append(line)
        else:
            result.append('')

    return '\n'.join(line for line in result if line.strip() or line == '')


# ---------------------------------------------------------------------------
# 5. Snippet Extraction (Leading + Trailing Context)
# ---------------------------------------------------------------------------
def _expand_tokens(tokens: list) -> list:
    """Expand strict tokens with semantically related terms."""
    expanded = set(t.lower() for t in tokens if len(t) > 3)
    for token in tokens:
        token_lower = token.lower()
        for key, related in TOKEN_EXPANSIONS.items():
            if key in token_lower:
                expanded.update(t.lower() for t in related if len(t) > 3)
    return list(expanded)


def _find_matches(lines: list, tokens: list, context_lines: int, qa_tokens: list = None) -> set:
    """Return line indices within context_lines of any matching line.

    Dynamic threshold: fewer tokens → lower bar.
    For 1-2 tokens, hits >= 1 is enough.
    qa_tokens: additive (OR logic) — merged into match set.
    """
    if not tokens:
        return set()

    if len(tokens) <= 2:
        min_hits = 1
    elif len(tokens) <= 5:
        min_hits = max(1, len(tokens) // 3)
    else:
        min_hits = 2

    match_indices = set()
    for i, line in enumerate(lines):
        line_lower = line.lower()
        hits = sum(1 for t in tokens if t in line_lower)
        if hits >= min_hits or (hits == 1 and any(t.startswith('0x') for t in tokens)):
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            for idx in range(start, end):
                match_indices.add(idx)

    # QA-context tokens: additive boost (OR logic)
    if qa_tokens:
        for i, line in enumerate(lines):
            line_lower = line.lower()
            qa_hits = sum(1 for t in qa_tokens if t in line_lower)
            if qa_hits >= 1:
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)
                for idx in range(start, end):
                    match_indices.add(idx)

    return match_indices


def _build_snippet_output(lines: list, sorted_indices: list) -> str:
    """Assemble matched lines with ... gaps between non-adjacent runs."""
    result = []
    prev = -1
    for idx in sorted_indices:
        if prev != -1 and idx > prev + 1:
            result.append('...')
        result.append(lines[idx])
        prev = idx
    return '\n'.join(result)


def extract_relevant_snippets(
    markdown_text: str, query: str, context_lines: int = None, qa_tokens: list = None
) -> str:
    """Extract only lines within context_lines of a matching line.

    context_lines: if None, computed dynamically from query + qa_tokens.
    qa_tokens: technical terms from Quick Answer for additive matching.
    """
    lines = markdown_text.split('\n')
    strict_tokens = [
        t.lower() for t in re.split(r'[\s_\-:./\\]+', query) if len(t) > 3
    ]

    # Dynamic context window if not explicitly provided
    if context_lines is None:
        context_lines = _dynamic_context(query, qa_tokens)

    match_indices = _find_matches(lines, strict_tokens, context_lines, qa_tokens)

    if len(match_indices) < 3:
        broad_tokens = _expand_tokens(strict_tokens)
        if broad_tokens != strict_tokens:
            broad_matches = _find_matches(lines, broad_tokens, context_lines, qa_tokens)
            match_indices = match_indices | broad_matches

    if not match_indices:
        return '\n'.join(lines[:30])

    return _build_snippet_output(lines, sorted(match_indices))


# ---------------------------------------------------------------------------
# 6. Smart Truncation
# ---------------------------------------------------------------------------
def smart_truncate(
    text: str, max_chars: int = 3000, hard_limit: int = 3500
) -> str:
    """Truncate text at a natural boundary (paragraph > sentence > hard cut)."""
    if len(text) <= max_chars:
        return text
    cut = text.rfind('\n\n', 0, hard_limit)
    if cut > max_chars * 0.5:
        return text[:cut] + '\n\n[...truncated]'
    cut = text.rfind('. ', 0, hard_limit)
    if cut > max_chars * 0.5:
        return text[:cut + 1] + '\n\n[...truncated]'
    return text[:max_chars] + '\n\n[...truncated]'


# ---------------------------------------------------------------------------
# 7. Relevance Scoring
# ---------------------------------------------------------------------------
def relevance_score(content: str, query: str) -> float:
    """Score content relevance by token frequency, boosted for code blocks."""
    tokens = [
        t.lower() for t in re.split(r'[\s_\-:./\\]+', query) if len(t) > 3
    ]
    content_lower = content.lower()
    score = sum(content_lower.count(t) for t in tokens)
    fence = chr(96) * 3
    if fence in content or 'def ' in content or 'import ' in content:
        score *= 1.5
    return score


def relevance_score_simple(link: dict, query: str) -> float:
    """Quick relevance score for link ranking before fetch."""
    tokens = [
        t.lower() for t in re.split(r'[\s_\-:./\\]+', query) if len(t) > 3
    ]
    text = (link.get('text', '') + ' ' + link.get('snippet', '')).lower()
    return sum(text.count(t) for t in tokens)


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------
async def launch_browser():
    browser_path = _ensure_chrome()
    log.info('Using browser: %s', browser_path)
    return await nodriver.start(
        headless=False,
        browser_executable_path=browser_path,
        user_data_dir=USER_DATA_DIR,
        browser_args=[],
        lang='en-US',
        no_sandbox=True,
    )


async def wait_for_qa_ready(tab, max_wait=30, interval=1.5):
    """Wait until Quick Answer citation links stabilize (stop growing)."""
    elapsed = 0
    prev_count = -1
    stable_checks = 0

    while elapsed < max_wait:
        await asyncio.sleep(interval)
        elapsed += interval

        count_js = """
            (() => {
                const qaBox = document.querySelector('.qa-content') ||
                              document.querySelector('.qa-container-box');
                return qaBox ? qaBox.querySelectorAll('sup a[href]').length : 0;
            })()
        """
        raw = await tab.evaluate(count_js)
        count = (
            int(raw)
            if isinstance(raw, (int, float))
            else (int(json.loads(raw)) if isinstance(raw, str) else 0)
        )

        log.debug('[%.1fs] QA sup links: %d', elapsed, count)

        if count == prev_count and count > 0:
            stable_checks += 1
            if stable_checks >= 2:
                log.info(
                    'QA stabilized at %d citation links after %.1fs',
                    count, elapsed,
                )
                return True
        else:
            stable_checks = 0

        prev_count = count

    log.warning('QA did not stabilize within %ds, proceeding anyway', max_wait)
    return True


# ---------------------------------------------------------------------------
# JS extraction scripts (enhanced with snippet + source fields)
# ---------------------------------------------------------------------------
_JS_EXTRACT_LINKS = """
    (() => {
        const results = [];
        const seen = new Set();

        // 1) Grab <sup><a> reference links inside the Quick Answer reply box
        const qaBox = document.querySelector('.qa-content') ||
                      document.querySelector('.qa-container-box');
        if (qaBox) {
            const supLinks = qaBox.querySelectorAll('sup a[href]');
            supLinks.forEach(a => {
                let href = a.href;
                href = href.replace(/#\\\\:~:text=[^&]*(&[^&]*)?$/, '');
                if (!href || seen.has(href)) return;
                seen.add(href);
                results.push({
                    href,
                    text: a.innerText.trim() || String(results.length + 1),
                    snippet: '',
                    source: 'qa'
                });
            });
        }

        // 2) Grab title links from search result items (unique URLs only)
        const srLinks = document.querySelectorAll(
            '__sri-title-box a.__sri_title_link[href], ' +
            '_0_TITLE a._0_sri_title_link[href]'
        );
        srLinks.forEach(a => {
            let href = a.href;
            try {
                const url = new URL(href);
                if (url.searchParams.get('utm_source') === 'kagi') return;
            } catch(e) {}
            if (!href || seen.has(href)) return;
            seen.add(href);
            const parent = a.closest('.__sri') || a.closest('._0_sri');
            const snippetEl = parent
                ? parent.querySelector('.__sri_snippet, ._0_sri_snippet')
                : null;
            results.push({
                href,
                text: a.innerText.trim(),
                snippet: snippetEl ? snippetEl.innerText.trim().slice(0, 200) : '',
                source: 'sr'
            });
        });

        // 3) Fallback: try old selectors in case Kagi reverts
        const oldSelectors = [
            'div._0_qa_references_box ol li a',
            'div._0_qa_more_info_box ol li a',
            '.qa-content ol li a',
            'ol[data-ref-list] li a'
        ];
        for (const sel of oldSelectors) {
            const anchors = document.querySelectorAll(sel);
            anchors.forEach(a => {
                if (!a.href || seen.has(a.href)) return;
                seen.add(a.href);
                results.push({
                    href: a.href,
                    text: a.innerText.trim(),
                    snippet: '',
                    source: 'qa'
                });
            });
        }

        return JSON.stringify(results);
    })()
"""

_JS_EXTRACT_QA_TEXT = """
    (() => {
        const contentBox = document.querySelector('.qa-content')
                        || document.querySelector('.qa-container-box');
        return contentBox ? contentBox.innerText : 'No Quick Answer found';
    })()
"""

_JS_EXTRACT_QA_HTML = """
    (() => {
        const contentBox = document.querySelector('.qa-content')
                        || document.querySelector('.qa-container-box');
        return contentBox ? contentBox.innerHTML : '';
    })()
"""


# ---------------------------------------------------------------------------
# Single search pass
# ---------------------------------------------------------------------------
async def _run_search_pass(
    browser,
    query: str,
    qa_text,
    fetched_hrefs: set,
    ref_counter: int,
    budget: int,
    max_refs: int,
    context_lines: int,
    max_chars: int,
    search_query_for_scoring: str,
    is_first_pass: bool,
    current_run_ref_files: list,
):
    """Execute one search pass against Kagi.

    Returns (qa_text, updated_ref_counter).
    qa_text is only extracted on the first pass.
    """
    log.info('Search pass with query: %s', query)

    # Open search in a NEW tab to avoid destroying previous state
    search_tab = await browser.get(
        KAGI_SEARCH_URL + query, new_window=True
    )
    await asyncio.sleep(random.randint(1, 2))

    # Click Quick Answer button
    try:
        quick_answer_button = await search_tab.find(
            'Quick Answer', best_match=True
        )
        if quick_answer_button:
            await quick_answer_button.scroll_into_view()
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await quick_answer_button.click()
            log.info('Clicked Quick Answer button')
        else:
            log.warning('Quick Answer button not found')
    except Exception as e:
        log.warning('Could not interact with Quick Answer button: %s', e)

    # Wait for QA to fully load
    await wait_for_qa_ready(search_tab)

    # ------------------------------------------------------------------
    # Extract links (QA citations + SR titles) with source tags + snippets
    # ------------------------------------------------------------------
    try:
        raw = await search_tab.evaluate(_JS_EXTRACT_LINKS)
        links = json.loads(raw) if isinstance(raw, str) else raw
        log.info('Found %d reference links (before dedup/filter)', len(links))
    except Exception as e:
        log.error('JS extraction failed: %s', e)
        links = []

    # ------------------------------------------------------------------
    # Dedup against globally fetched hrefs
    # ------------------------------------------------------------------
    new_links = [l for l in links if l['href'] not in fetched_hrefs]
    log.info(
        'After global dedup: %d new links (already fetched: %d)',
        len(new_links), len(fetched_hrefs),
    )

    # ------------------------------------------------------------------
    # Pre-filter for relevance
    # ------------------------------------------------------------------
    relevant_links = [
        l for l in new_links
        if is_likely_relevant(l, search_query_for_scoring)
    ]
    filtered_out = len(new_links) - len(relevant_links)
    if filtered_out > 0:
        log.info(
            'After relevance filter: %d links (filtered out: %d)',
            len(relevant_links), filtered_out,
        )
    else:
        log.info(
            'After relevance filter: %d links (none filtered)',
            len(relevant_links),
        )

    # ------------------------------------------------------------------
    # Rank by relevance and cap at budget
    # ------------------------------------------------------------------
    relevant_links.sort(
        key=lambda l: relevance_score_simple(l, search_query_for_scoring),
        reverse=True,
    )
    fetch_budget = min(max_refs - ref_counter, budget)
    links_to_fetch = relevant_links[:fetch_budget]
    log.info(
        'Will fetch %d links (budget remaining: %d)',
        len(links_to_fetch), fetch_budget,
    )

    # ------------------------------------------------------------------
    # Extract QA text (only on first pass)
    # ------------------------------------------------------------------
    if is_first_pass and qa_text is None:
        try:
            qa_text = await search_tab.evaluate(_JS_EXTRACT_QA_TEXT)
            log.info('Extracted Quick Answer text')
        except Exception as e:
            log.error(
                'Failed to extract Quick Answer: %s\n%s',
                e, traceback.format_exc(),
            )
            qa_text = ''

    # ------------------------------------------------------------------
    # Save QA markdown to disk (only on first pass)
    # ------------------------------------------------------------------
    if is_first_pass:
        qa_filepath = os.path.join(RESULTS_DIR, 'quick_answer_output.md')
        try:
            html_content = await search_tab.evaluate(_JS_EXTRACT_QA_HTML)
            markdown_content = md(html_content, strip=NOISE_STRIP_TAGS)
            with open(qa_filepath, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            log.info('Saved Quick Answer content to quick_answer_output.md')
        except Exception as e:
            log.error(
                'Failed to export Quick Answer: %s\n%s',
                e, traceback.format_exc(),
            )

    # ------------------------------------------------------------------
    # PHASE 1: Open ALL reference tabs in parallel (like old script)
    # ------------------------------------------------------------------
    open_tabs = []  # list of (link, tab) tuples

    for link in links_to_fetch:
        try:
            tab = await browser.get(link['href'], new_window=True)
            open_tabs.append((link, tab))
            log.info('Opened tab for: %s', link['href'][:80])
        except Exception as e:
            log.error(
                'Failed to open tab for %s: %s',
                link['href'][:80], e,
            )

    # Let all tabs load simultaneously
    log.info(
        'Opened %d tabs in parallel, waiting for pages to load...',
        len(open_tabs),
    )
    # Dynamic readiness polling replaces fixed 10s sleep → saves 3-7s/page
    await asyncio.gather(*[
        wait_for_page_ready(tab) for _, tab in open_tabs
    ], return_exceptions=True)

    # ------------------------------------------------------------------
    # PHASE 2: Process each loaded tab one at a time (extract + save)
    # ------------------------------------------------------------------
    # Extract QA context tokens once for this pass
    _qa_tokens = extract_qa_context_tokens(qa_text) if qa_text else []

    for idx, (link, new_tab) in enumerate(open_tabs):
        try:
            # Targeted JS extraction: semantic containers only, noise stripped in-browser
            page_html = await new_tab.evaluate(_JS_EXTRACT_CONTENT)

            # Convert HTML -> markdown with noise-tag stripping at source
            page_markdown = md(page_html, strip=NOISE_STRIP_TAGS)

            # Post-conversion textual noise stripping
            page_markdown = strip_noise(page_markdown)

            # Extract only relevant snippets with context + QA-guided matching
            snippets = extract_relevant_snippets(
                page_markdown, search_query_for_scoring, context_lines, _qa_tokens
            )

            # Smart truncation at natural boundaries
            snippets = smart_truncate(snippets, max_chars=max_chars)

            # Clean filename from link text or URL
            raw_text = link.get('text') or ('ref_' + str(idx + 1))
            clean_text = ''.join(
                c for c in raw_text[:50]
                if c.isalnum() or c == ' '
            ).replace(' ', '_')
            clean_text = ''.join(
                c for c in clean_text if c.isalnum() or c == '_'
            )
            if not clean_text:
                clean_text = 'ref_' + str(idx + 1)

            ref_counter += 1
            filename = (
                'reference_' + str(ref_counter) + '_' + clean_text + '.md'
            )
            filepath = os.path.join(RESULTS_DIR, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(snippets)
            log.info('Saved snippet reference %d to %s', ref_counter, filepath)
            current_run_ref_files.append(filename)
            fetched_hrefs.add(link['href'])

        except Exception as e:
            log.error(
                'Failed to process tab %d (%s): %s\n%s',
                idx + 1, link['href'][:80], e, traceback.format_exc(),
            )
        finally:
            try:
                await new_tab.close()
                log.debug('Closed reference tab %d', idx + 1)
            except Exception:
                pass

    # Close the search tab
    try:
        await search_tab.close()
        log.info('Closed search tab')
    except Exception:
        pass

    await asyncio.sleep(1)

    return qa_text, ref_counter



# ---------------------------------------------------------------------------
# Main search entry point
# ---------------------------------------------------------------------------
async def run_search(
    search_query: str,
    max_refs: int = 5,
    context_lines: int = 15,
    max_chars: int = 3000,
    verbose: bool = False,
) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    log.info('Starting search for: %s', search_query)

    # ==============================================================
    # CRITICAL FIX: Clean the results directory BEFORE each run.
    # Old reference files from previous searches were leaking into
    # the output because os.listdir(RESULTS_DIR) picks up ALL .md
    # files regardless of which search created them.
    # ==============================================================
    if os.path.isdir(RESULTS_DIR):
        for old_file in os.listdir(RESULTS_DIR):
            old_path = os.path.join(RESULTS_DIR, old_file)
            try:
                if os.path.isfile(old_path):
                    os.remove(old_path)
                    log.debug('Removed stale result file: %s', old_path)
            except Exception as e:
                log.warning('Could not remove %s: %s', old_path, e)
    log.info('Cleaned results directory before search')

    # ------------------------------------------------------------------
    # 1. Query cleaning - strip machine-unique identifiers
    # ------------------------------------------------------------------
    primary_query, fallback_query = extract_search_query(search_query)
    log.info('Primary query: %s', primary_query)
    if fallback_query:
        log.info('Fallback query: %s', fallback_query)

    # Global dedup and budget state across passes
    fetched_hrefs = set()
    ref_counter = 0
    qa_text = None
    # Track ONLY files created in THIS run (not stale ones)
    current_run_ref_files = []

    try:
        browser = await launch_browser()
        # ----------------------------------------------------------------
        # PASS 1: search with cleaned primary query
        # ----------------------------------------------------------------
        qa_text, ref_counter = await _run_search_pass(
            browser=browser,
            query=primary_query,
            qa_text=None,
            fetched_hrefs=fetched_hrefs,
            ref_counter=ref_counter,
            budget=max_refs,
            max_refs=max_refs,
            context_lines=context_lines,
            max_chars=max_chars,
            search_query_for_scoring=primary_query,
            is_first_pass=True,
            current_run_ref_files=current_run_ref_files,
        )

        # ----------------------------------------------------------------
        # PASS 2 (if budget remains): search with fallback query
        # ----------------------------------------------------------------
        remaining_budget = max_refs - ref_counter
        if fallback_query and remaining_budget > 0:
            log.info(
                'Pass 2 with fallback query (budget remaining: %d)',
                remaining_budget,
            )
            qa_text, ref_counter = await _run_search_pass(
                browser=browser,
                query=fallback_query,
                qa_text=qa_text,
                fetched_hrefs=fetched_hrefs,
                ref_counter=ref_counter,
                budget=remaining_budget,
                max_refs=max_refs,
                context_lines=context_lines,
                max_chars=max_chars,
                search_query_for_scoring=primary_query,
                is_first_pass=False,
                current_run_ref_files=current_run_ref_files,
            )

        # ----------------------------------------------------------------
        # BUILD OUTPUT: QA text first, then references sorted by relevance
        # ==============================================================
        # FIX: Use current_run_ref_files (populated during this run)
        # instead of os.listdir(RESULTS_DIR) which would include
        # stale files from previous searches.
        # ==============================================================
        # ----------------------------------------------------------------
        output_parts = []
        if qa_text and qa_text != 'No Quick Answer found':
            output_parts.append('## Quick Answer\n\n' + qa_text)

        # Score and sort ONLY the files from this run
        scored_refs = []
        for ref_file in current_run_ref_files:
            ref_path = os.path.join(RESULTS_DIR, ref_file)
            if not os.path.exists(ref_path):
                continue
            with open(ref_path, 'r', encoding='utf-8') as f:
                content = f.read()
            score = relevance_score(content, primary_query)
            scored_refs.append((score, ref_file, content))

        # Sort by relevance (best first) - LLMs weight early content higher
        scored_refs.sort(key=lambda x: x[0], reverse=True)

        for score, ref_file, content in scored_refs:
            output_parts.append('## ' + ref_file + '\n\n' + content)

        if output_parts:
            return '\n\n---\n\n'.join(output_parts)
        return 'No content retrieved.'

    except Exception as e:
        err = 'Error: ' + str(e) + '\n' + traceback.format_exc()
        log.error(err)
        return err

    finally:
        try:
            browser.stop()
            log.info('Browser stopped')
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='Kagi Search - direct CLI tool for pi agent'
    )
    parser.add_argument(
        'search_query',
        help='The search query to send to Kagi.',
    )
    parser.add_argument(
        '--max-refs',
        type=int,
        default=5,
        help='Max reference pages to fetch across all passes. Default: 5',
    )
    parser.add_argument(
        '--context-lines',
        type=int,
        default=15,
        help='Lines of context before/after a match in snippet extraction. Default: 15',
    )
    parser.add_argument(
        '--max-chars',
        type=int,
        default=3000,
        help='Max chars per reference snippet after truncation. Default: 3000',
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging to kagi_mcp.log.',
    )
    args = parser.parse_args()

    log.info(
        'CLI called: kagi_search | query=%s | max_refs=%d | context_lines=%d | max_chars=%d',
        args.search_query, args.max_refs, args.context_lines, args.max_chars,
    )
    result = asyncio.run(
        run_search(
            args.search_query,
            max_refs=args.max_refs,
            context_lines=args.context_lines,
            max_chars=args.max_chars,
            verbose=args.verbose,
        )
    )
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(result)


if __name__ == '__main__':
    main()
