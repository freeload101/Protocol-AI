#!/usr/bin/env python3
"""scrape_url.py - URL scraper using headless Chrome + innerText extraction.

Uses nodriver to launch Chromium, navigates to the target URL, waits for
the page to load, then extracts clean text via document.body.innerText
plus heading structure for formatting. No HTML parsing or markdownify needed.
"""

import asyncio
import os
import sys
import argparse
import logging
import re
import subprocess
import time

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


_ensure_deps([('nodriver', 'nodriver==0.47.0')])

# ---------------------------------------------------------------------------
# Portable layout - everything lives next to this script:
#   <SCRIPT_DIR>/CHROME_PORTABLE/   auto-installed if missing (_ensure_chrome)
#   <SCRIPT_DIR>/ScrapeProfile/     dedicated Chrome profile (isolated from
#                                    kagi_search's 'Default' profile)
#   <SCRIPT_DIR>/scrape_mcp.log     debug log
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(SCRIPT_DIR, 'ScrapeProfile')
CHROME_PORTABLE_DIR = os.path.join(SCRIPT_DIR, 'CHROME_PORTABLE')
# Portable Chrome installer is inlined below (_install_portable_chrome) -
# no external installer file needed.

# Log to file - never to stdout (stdout carries tool output to pi)
logging.basicConfig(
    filename=os.path.join(SCRIPT_DIR, 'scrape_mcp.log'),
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger('scrape-mcp')


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

# Noise patterns to strip from extracted text
_NOISE_PATTERN = re.compile(
    '|'.join([
        r'^\s*(home|about|blog|contact|login|sign up|newsletter|subscribe)\s*$',
        r'cookie|privacy policy|terms of (use|service)|accept all',
        r'share (this|on)|follow us|twitter|facebook|linkedin|instagram',
        r'advertisement|sponsored|affiliate',
        r'(c|\u00a9)\s*\d{4}|all rights reserved|powered by',
    ]),
    re.IGNORECASE,
)


def _unwrap(val):
    """Unwrap nodriver response values.

    nodriver wraps responses as:
      - strings: {'type': 'string', 'value': 'text'}
      - objects (dicts): {'type': 'object', 'value': [['key', val], ...]}
      - lists: {'type': 'array', 'value': [items...]}
    """
    if isinstance(val, dict):
        if 'value' in val:
            v = val['value']
            # Object wrapped as list of [key, value] pairs
            if val.get('type') == 'object' and isinstance(v, list):
                d = {}
                for pair in v:
                    if isinstance(pair, (list, tuple)) and len(pair) == 2:
                        k = _unwrap(pair[0])
                        d[k] = _unwrap(pair[1])
                return d
            # Array
            elif val.get('type') == 'array' and isinstance(v, list):
                return [_unwrap(item) for item in v]
            else:
                return v
    return val


async def launch_browser():
    import nodriver
    browser_path = _ensure_chrome()
    log.info('Using browser: %s', browser_path)
    config = nodriver.Config(
        browser_executable_path=browser_path,
        user_data_dir=USER_DATA_DIR,
        lang='en-US',
        no_sandbox=True,
        browser_args=['--headless=new'],
    )
    browser = await nodriver.Browser.create(config)
    log.info('Browser launched successfully')
    return browser


async def scrape_url(url: str, max_chars: int = 15000) -> str:
    """Navigate to a URL and return clean text content."""
    log.info('Scraping URL: %s', url)

    browser = await launch_browser()
    try:
        tab = await browser.get(url)

        # Wait for page to load (network idle + buffer for JS rendering)
        await asyncio.sleep(3)

        # Get title
        raw_title = await tab.evaluate('document.title')
        title = _unwrap(raw_title) or 'Untitled Page'

        # Get heading structure for formatting
        headings_raw = await tab.evaluate('''
            Array.from(document.querySelectorAll('h1,h2,h3'))
                .map(h => ({tag: h.tagName, text: h.innerText.trim()}))
        ''')
        headings = [_unwrap(h) for h in (headings_raw or [])]

        # Get body text (innerText gives clean readable text, no HTML tags)
        raw_body = await tab.evaluate('document.body.innerText')
        body_text = _unwrap(raw_body) or ''

        log.info('Got title=%s, %d headings, %d chars of body', title, len(headings), len(body_text))

        # Build heading map for smart formatting
        heading_set = set()
        for h in headings:
            if isinstance(h, dict):
                heading_set.add(h.get('text', '').lower())
            elif isinstance(h, str):
                heading_set.add(h.lower())

        # Process body text line by line
        lines = body_text.split('\n')
        result_lines = []
        prev_blank = False

        for line in lines:
            stripped = line.strip()

            if not stripped:
                if not prev_blank:
                    result_lines.append('')
                prev_blank = True
                continue

            # Skip noise lines
            if _NOISE_PATTERN.search(stripped):
                continue

            # Detect heading-like text and format with markdown headers
            is_heading = False
            for h in headings:
                h_text = h.get('text', '') if isinstance(h, dict) else h
                tag = 'h3'  # default
                if isinstance(h, dict):
                    tag = (h.get('tag') or 'H3').lower()

                if stripped.lower().startswith(h_text.lower()) and len(h_text) > 3:
                    prefix = '#' * (2 if tag == 'h1' else 3 if tag == 'h2' else 4)
                    result_lines.append(f'\n{prefix} {stripped}\n')
                    is_heading = True
                    break

            if not is_heading:
                result_lines.append(stripped)
            prev_blank = False

        text = '\n'.join(result_lines)

        # Truncate if too long — cut at paragraph boundary
        if len(text) > max_chars:
            cut = text.rfind('\n\n', 0, max_chars + 500)
            if cut > max_chars * 0.5:
                text = text[:cut] + '\n\n[...truncated]'
            else:
                text = text[:max_chars] + '\n\n[...truncated]'

        return f'# {title}\n\n{text}'

    except Exception as e:
        log.error('Scrape failed: %s', e, exc_info=True)
        return f'Error scraping {url}: {e}'
    finally:
        try:
            browser.stop()
            log.info('Browser stopped')
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description='URL scraper using headless Chrome')
    parser.add_argument('url', help='The URL to scrape.')
    parser.add_argument(
        '--max-chars',
        type=int,
        default=15000,
        help='Max characters in output. Default: 15000',
    )
    args = parser.parse_args()

    log.info('CLI called: scrape_url | url=%s | max_chars=%d', args.url, args.max_chars)
    result = asyncio.run(scrape_url(args.url, max_chars=args.max_chars))
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(result)


if __name__ == '__main__':
    main()
