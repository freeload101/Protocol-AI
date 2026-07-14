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

# Log to file - never to stdout (stdout carries tool output to pi)
logging.basicConfig(
    filename='scrape_mcp.log',
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger('scrape-mcp')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BROWSER_PATH = os.path.join(SCRIPT_DIR, 'Chromium', 'Application', 'chrome.exe')
USER_DATA_DIR = os.path.join(SCRIPT_DIR, 'Default')

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
    config = nodriver.Config(
        USER_DATA_DIR,
        browser_executable_path=BROWSER_PATH,
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
