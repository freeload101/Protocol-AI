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

Uses Playwright (async API) for browser automation on Linux.
"""

import asyncio
import random
import os
import json
import logging
import re
import traceback
import sys
import argparse
import shutil

from playwright.async_api import async_playwright
from markdownify import markdownify as md

# Requirements: playwright markdownify (playwright browsers installed via `playwright install chromium`)

# Linux browser detection is handled by Playwright automatically.
# Just ensure you've run: playwright install chromium

# Log to file - never to stdout (stdout carries tool output to pi)
logging.basicConfig(
    filename='kagi_mcp.log',
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger('kagi-mcp')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, 'results')
USER_DATA_DIR = os.path.join(SCRIPT_DIR, '.kagi_user_data')

# ---------------------------------------------------------------------------
# Constants for the refactored pipeline
# ---------------------------------------------------------------------------
NOISE_STRIP_TAGS = [
    'nav', 'footer', 'header', 'aside', 'script', 'style',
    'noscript', 'iframe', 'form', 'svg',
]

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


def _find_matches(lines: list, tokens: list, context_lines: int) -> set:
    """Return line indices within context_lines of any matching line.

    Dynamic threshold: fewer tokens -> lower bar.
    For 1-2 tokens, hits >= 1 is enough.
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
    markdown_text: str, query: str, context_lines: int = 15
) -> str:
    """Extract only lines within context_lines of a matching line."""
    lines = markdown_text.split('\n')
    strict_tokens = [
        t.lower() for t in re.split(r'[\s_\-:./\\]+', query) if len(t) > 3
    ]

    match_indices = _find_matches(lines, strict_tokens, context_lines)

    if len(match_indices) < 3:
        broad_tokens = _expand_tokens(strict_tokens)
        if broad_tokens != strict_tokens:
            broad_matches = _find_matches(lines, broad_tokens, context_lines)
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
# Browser helpers (Playwright async API)
# ---------------------------------------------------------------------------
async def launch_browser():
    """Launch a Chromium browser via Playwright and return the context."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=['--disable-gpu'],
    )
    context = await browser.new_context(
        locale='en-US',
        viewport={'width': 1280, 'height': 720},
    )
    return browser, context


async def wait_for_qa_ready(page, max_wait=30, interval=1.5):
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
        raw = await page.evaluate(count_js)
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
    context,
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

    # Open search in a NEW page to avoid destroying previous state
    search_page = await context.new_page()
    await search_page.goto(
        'https://kagi.com/search?token=-3nL2Xv-RTREDACTEDRTREDACTEDRTREDACTEDRTREDACTEDRTREDACTEDRTREDACTEDhOs&q=' + query
    )
    await asyncio.sleep(random.randint(1, 2))

    # Click Quick Answer button
    try:
        qa_button = search_page.locator('text="Quick Answer"').first
        if await qa_button.is_visible():
            await qa_button.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await qa_button.click()
            log.info('Clicked Quick Answer button')
        else:
            log.warning('Quick Answer button not found or not visible')
    except Exception as e:
        log.warning('Could not interact with Quick Answer button: %s', e)

    # Wait for QA to fully load
    await wait_for_qa_ready(search_page)

    # ------------------------------------------------------------------
    # Extract links (QA citations + SR titles) with source tags + snippets
    # ------------------------------------------------------------------
    try:
        raw = await search_page.evaluate(_JS_EXTRACT_LINKS)
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
            qa_text = await search_page.evaluate(_JS_EXTRACT_QA_TEXT)
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
            html_content = await search_page.evaluate(_JS_EXTRACT_QA_HTML)
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
    # PHASE 1: Open ALL reference pages and navigate concurrently
    # ------------------------------------------------------------------
    open_pages = []  # list of (link, page) tuples

    # Create all pages first
    for link in links_to_fetch:
        try:
            new_page = await context.new_page()
            open_pages.append((link, new_page))
            log.info('Created page for: %s', link['href'][:80])
        except Exception as e:
            log.error(
                'Failed to create page for %s: %s',
                link['href'][:80], e,
            )

    # Navigate all pages in parallel using asyncio.gather
    async def _navigate(page, href):
        try:
            await page.goto(href)
        except Exception as e:
            log.error('Failed to navigate to %s: %s', href[:80], e)

    tasks = [_navigate(pg, lnk['href']) for lnk, pg in open_pages]
    await asyncio.gather(*tasks, return_exceptions=True)

    # Let pages settle after navigation completes
    log.info(
        'All %d pages navigated, waiting for content to load...',
        len(open_pages),
    )
    await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # PHASE 2: Process each loaded page one at a time (extract + save)
    # ------------------------------------------------------------------
    for idx, (link, new_page) in enumerate(open_pages):
        try:
            page_html = await new_page.evaluate('document.body.innerHTML')

            # Convert HTML -> markdown with noise-tag stripping at source
            page_markdown = md(page_html, strip=NOISE_STRIP_TAGS)

            # Post-conversion textual noise stripping
            page_markdown = strip_noise(page_markdown)

            # Extract only relevant snippets with context
            snippets = extract_relevant_snippets(
                page_markdown, search_query_for_scoring, context_lines
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
                'Failed to process page %d (%s): %s\n%s',
                idx + 1, link['href'][:80], e, traceback.format_exc(),
            )
        finally:
            try:
                await new_page.close()
                log.debug('Closed reference page %d', idx + 1)
            except Exception:
                pass

    # Close the search page
    try:
        await search_page.close()
        log.info('Closed search page')
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

    browser, context = await launch_browser()

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
        # ----------------------------------------------------------------
        # PASS 1: search with cleaned primary query
        # ----------------------------------------------------------------
        qa_text, ref_counter = await _run_search_pass(
            browser=browser,
            context=context,
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
                context=context,
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
            await context.close()
            await browser.close()
            log.info('Browser closed')
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
