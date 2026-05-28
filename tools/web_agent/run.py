"""Deterministic browser-driving CLI primitives for web-agent.

Five entry points across F012-F017 of the web-agent-vendor plan:

* ``web-navigate <url>`` — open a URL in a fresh or existing session and
  return the page title plus a markdown-rendered accessibility tree.
* ``web-click <selector>`` — click an element addressed by CSS, XPath
  (``xpath=...``), or ARIA (``role=button[name=Submit]``).
* ``web-type <selector> <text>`` — focus the element and type text,
  optionally clearing first or pressing Enter to submit.
* ``web-extract`` — extract page (or selector-subtree) content as
  markdown, plain text, or HTML.
* ``web-screenshot`` — capture a viewport or full-page PNG via CDP.
* ``web-session-record-compile`` — collect step PNGs recorded by an
  opt-in recording session and compile them into an animated GIF.

Each entry point is a thin argparse wrapper over the vendored
``BrowserSession`` plumbing (``src/web_agent/browser/session.py``) — we
attach over CDP to a Chromium spawned by ``SessionManager.start()`` and
keep reusing that browser across calls. CDP plumbing (mouse events, key
dispatch, DOM querying) lives in the vendored watchdogs; here we only
translate argparse → vendored calls → JSON.

Selector resolution and "type back the value we read" both go through
``Runtime.evaluate`` — a minimal JS shim that handles all three selector
flavours and returns a backend node id we can address via CDP. This is
deliberately less rich than the upstream agent loop (no occlusion checks,
no smart waiting, no compound-component handling) — primitives are
deterministic and run against ``file://`` fixtures, where the simple
``document.querySelector`` / ``document.evaluate`` paths are sufficient.

Marker policy: per ``.claude/rules/log-markers.md``, the closed
``ALLOWED_CATEGORIES`` set is ``BEHAVE/PYTEST/STAGE/BOUNDARY/CHECKPOINT``.
The web-agent-vendor plan's proposed ``[WEB:*]`` markers are invalid; we
use ``BOUNDARY`` (via ``@log_call('BOUNDARY')``) instead. F018 of the
plan is the place to revisit if richer markers are needed inside the
allowed grammar.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from src.utils.log_util import getLogger, log_call
from src.web_agent.sessions import (
    Session,
    SessionCrashedError,
    SessionManager,
    SessionNotFoundError,
)

logger = getLogger(__name__)

# CDP / asyncio guards — static file:// fixtures finish in seconds; these
# caps prevent a wedged WebSocket or shutdown drain from burning the full
# behave subprocess timeout (120s) under parallel pre-push load.
_RUNTIME_EVAL_TIMEOUT_S = 30.0
_EXTRACT_TOTAL_TIMEOUT_S = 90.0
_CDP_STOP_TIMEOUT_S = 5.0


# ── JS shims used by every primitive ───────────────────────────────────


# ``_JS_RESOLVE_SELECTOR`` returns the first element matching ``selector``,
# under one of three flavours:
#   * ``css:`` (default) — ``document.querySelector(...)``
#   * ``xpath:`` — ``document.evaluate(...)`` (FIRST_ORDERED_NODE_SNAPSHOT)
#   * ``role:`` — walk the document, match ``aria-role`` or implicit role
#     plus accessible name (``aria-label``/text content). Sufficient for
#     ``role=button[name=Submit]``-style addressing in tests.
#
# The function returns ``null`` when nothing matches; callers raise a
# ``RuntimeError`` to surface as the JSON error envelope. We deliberately
# keep this small — the vendored ``DOMTreeSerializer`` does richer
# resolution but couples the result to an ``EnhancedDOMTreeNode`` graph
# that is overkill for the deterministic-fixture primitives.
_JS_RESOLVE_SELECTOR = r"""
(function(rawSelector) {
  function parseSelector(s) {
    if (s.startsWith('xpath=')) return { kind: 'xpath', value: s.slice(6) };
    if (s.startsWith('role=')) {
      // role=button[name=Submit]  →  { role: 'button', name: 'Submit' }
      var rest = s.slice(5);
      var m = rest.match(/^([a-zA-Z]+)(?:\[name=([^\]]+)\])?$/);
      if (!m) return { kind: 'role', value: { role: rest, name: null } };
      return { kind: 'role', value: { role: m[1], name: m[2] || null } };
    }
    return { kind: 'css', value: s };
  }
  var sel = parseSelector(rawSelector);
  var el = null;
  if (sel.kind === 'css') {
    el = document.querySelector(sel.value);
  } else if (sel.kind === 'xpath') {
    var res = document.evaluate(
      sel.value,
      document,
      null,
      XPathResult.FIRST_ORDERED_NODE_TYPE,
      null
    );
    el = res ? res.singleNodeValue : null;
  } else if (sel.kind === 'role') {
    var role = sel.value.role;
    var name = sel.value.name;
    var IMPLICIT_ROLES = {
      'button': 'BUTTON',
      'link': 'A',
      'textbox': 'INPUT|TEXTAREA',
      'checkbox': 'INPUT',
      'radio': 'INPUT',
    };
    var all = document.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      var node = all[i];
      var nodeRole = node.getAttribute('role');
      var matchesRole = false;
      if (nodeRole === role) {
        matchesRole = true;
      } else if (!nodeRole && IMPLICIT_ROLES[role]) {
        matchesRole = IMPLICIT_ROLES[role].split('|').indexOf(node.tagName) >= 0;
      }
      if (!matchesRole) continue;
      if (name === null) { el = node; break; }
      var label = node.getAttribute('aria-label') || node.textContent || '';
      if (label.trim() === name.trim()) { el = node; break; }
    }
  }
  if (!el) return null;
  // Tag the element so the click/type CDP calls can find it again via
  // a unique id even if the DOM otherwise lacks one.
  if (!el.hasAttribute('data-cowork-resolved')) {
    el.setAttribute('data-cowork-resolved', 'cowork-' + Math.random().toString(36).slice(2));
  }
  var rect = el.getBoundingClientRect();
  return {
    tag: el.tagName,
    id: el.id || null,
    cowork_id: el.getAttribute('data-cowork-resolved'),
    text: (el.textContent || '').slice(0, 200),
    rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
  };
})(%s)
""".strip()


# ``_JS_DISPATCH_CLICK`` simulates the dispatch chain a real click would
# fire (mousedown/mouseup/click). Using ``el.click()`` directly works for
# most form controls and pure-JS toggle buttons, which is all the
# fixtures need; falling back to a synthetic event-dispatch chain would
# be necessary for elements that listen specifically to mousedown/mouseup.
_JS_DISPATCH_CLICK = r"""
(function(coworkId) {
  var el = document.querySelector('[data-cowork-resolved="' + coworkId + '"]');
  if (!el) return { ok: false, error: 'element-vanished' };
  el.click();
  return { ok: true, current_url: window.location.href };
})(%s)
""".strip()


# ``_JS_TYPE_INTO`` focuses the element, optionally clears its current
# value, then sets the value and dispatches input/change events so that
# any onchange handler fires. Returns the post-type ``current_value`` so
# the CLI can echo back what the page actually saw.
_JS_TYPE_INTO = r"""
(function(coworkId, text, clearFirst) {
  var el = document.querySelector('[data-cowork-resolved="' + coworkId + '"]');
  if (!el) return { ok: false, error: 'element-vanished' };
  if (typeof el.focus === 'function') el.focus();
  if (clearFirst) {
    if ('value' in el) {
      el.value = '';
    } else {
      el.textContent = '';
    }
  }
  if ('value' in el) {
    el.value = (clearFirst ? '' : (el.value || '')) + text;
  } else {
    el.textContent = (clearFirst ? '' : (el.textContent || '')) + text;
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  var current = ('value' in el) ? el.value : el.textContent;
  return { ok: true, current_value: current };
})(%s, %s, %s)
""".strip()


# ``_JS_SIMPLE_MARKDOWN`` produces the deterministic markdown snapshot
# returned in ``accessibility_tree``. It walks the document, emitting
# headings and visible interactive elements as markdown lines, then
# appends a body-text section for static content. See
# ``_accessibility_markdown`` for the rationale.
_JS_SIMPLE_MARKDOWN = r"""
(function() {
  function isVisible(el) {
    if (!el) return false;
    var style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    var rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    return true;
  }
  var lines = [];
  // Page title
  if (document.title) {
    lines.push('Page title: ' + document.title);
    lines.push('');
  }
  // Headings
  for (var level = 1; level <= 6; level++) {
    var heads = document.querySelectorAll('h' + level);
    for (var i = 0; i < heads.length; i++) {
      if (!isVisible(heads[i])) continue;
      lines.push('#'.repeat(level) + ' ' + (heads[i].textContent || '').trim());
    }
  }
  if (lines.length > 0) lines.push('');
  // Interactive elements
  var interactive = document.querySelectorAll(
    'button, input, textarea, select, a[href]'
  );
  for (var j = 0; j < interactive.length; j++) {
    var el = interactive[j];
    if (!isVisible(el)) continue;
    var tag = el.tagName.toLowerCase();
    var label = (el.getAttribute('aria-label')
                 || el.getAttribute('placeholder')
                 || el.textContent
                 || el.value
                 || '').toString().trim().slice(0, 80);
    var id = el.id ? ('#' + el.id) : '';
    lines.push('- [' + tag + id + '] ' + label);
  }
  if (lines.length > 0) lines.push('');
  // Body text — visible plain content. Critical for round-tripping
  // markers like 'MARKER_VISIBLE' that aren't on interactive elements.
  if (document.body) {
    lines.push('--- body text ---');
    lines.push(document.body.innerText || '');
  }
  return lines.join('\n');
})()
""".strip()


# ``_JS_SIMPLE_MARKDOWN_FOR_SELECTOR`` is the selector-subtree variant used
# by ``web-extract`` (F015). The walk is identical to ``_JS_SIMPLE_MARKDOWN``
# but rooted at the resolved element rather than ``document``. Selector
# resolution reuses the ``data-cowork-resolved`` tag set by the shared
# ``_JS_RESOLVE_SELECTOR`` shim, so the click/type cache discipline carries
# over: extract-after-click never re-runs the selector. Empty subtree
# (resolved element with no headings/interactives/text) returns just the
# ``--- body text ---`` block holding whatever ``innerText`` reports.
_JS_SIMPLE_MARKDOWN_FOR_SELECTOR = r"""
(function(coworkId) {
  var root = document.querySelector('[data-cowork-resolved="' + coworkId + '"]');
  if (!root) return null;
  function isVisible(el) {
    if (!el) return false;
    var style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    var rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    return true;
  }
  var lines = [];
  // Headings inside the subtree
  for (var level = 1; level <= 6; level++) {
    var heads = root.querySelectorAll('h' + level);
    for (var i = 0; i < heads.length; i++) {
      if (!isVisible(heads[i])) continue;
      lines.push('#'.repeat(level) + ' ' + (heads[i].textContent || '').trim());
    }
  }
  if (lines.length > 0) lines.push('');
  // Interactive elements inside the subtree
  var interactive = root.querySelectorAll(
    'button, input, textarea, select, a[href]'
  );
  for (var j = 0; j < interactive.length; j++) {
    var el = interactive[j];
    if (!isVisible(el)) continue;
    var tag = el.tagName.toLowerCase();
    var label = (el.getAttribute('aria-label')
                 || el.getAttribute('placeholder')
                 || el.textContent
                 || el.value
                 || '').toString().trim().slice(0, 80);
    var id = el.id ? ('#' + el.id) : '';
    lines.push('- [' + tag + id + '] ' + label);
  }
  if (lines.length > 0) lines.push('');
  lines.push('--- body text ---');
  lines.push(root.innerText || '');
  return lines.join('\n');
})(%s)
""".strip()


# Plain-text and HTML fetchers for ``web-extract`` --format text / html.
# Both reuse the resolved-id tag so the selector resolution path is
# unified across the three formats. ``null`` selector means whole-page —
# resolved at the Python layer to avoid duplicating fallback logic in JS.
_JS_INNERTEXT_FOR_SELECTOR = r"""
(function(coworkId) {
  var root = document.querySelector('[data-cowork-resolved="' + coworkId + '"]');
  if (!root) return null;
  return (root.innerText || '').trim();
})(%s)
""".strip()


_JS_OUTERHTML_FOR_SELECTOR = r"""
(function(coworkId) {
  var root = document.querySelector('[data-cowork-resolved="' + coworkId + '"]');
  if (!root) return null;
  return root.outerHTML || '';
})(%s)
""".strip()


_JS_BODY_INNERTEXT = r"""
(function() {
  return (document.body ? document.body.innerText : '').trim();
})()
""".strip()


_JS_BODY_OUTERHTML = r"""
(function() {
  return document.documentElement ? document.documentElement.outerHTML : '';
})()
""".strip()


# ``_JS_TABS_CLICK_AND_CAPTURE`` drives the ``--tabs`` flag for F004. The
# shim resolves all trigger elements via ``document.querySelectorAll``,
# then for each trigger: click it, poll for the panel selector to appear
# (with a configurable settle / poll budget), and capture the panel's
# ``outerHTML``. Doing the whole loop inside a single
# ``Runtime.evaluate`` keeps round-trips down — for 3 panels we make one
# CDP call instead of ~9.
#
# Pre-rendered hidden panels: the click is harmless (no-op for visible
# content) and the panel is found on the first poll. Lazy panels appear
# after the click; the poll loop waits up to ``settle_ms`` for them.
#
# Returns ``{trigger_count, panels: [{index, html, found}]}`` so the
# Python side can distinguish "trigger present but panel never
# materialised" (found:false) from "panel content captured" (found:true).
_JS_TABS_CLICK_AND_CAPTURE = r"""
(async function(triggerSelector, panelSelector, settleMs, pollMs) {
  function sleep(ms) { return new Promise(function(r) { setTimeout(r, ms); }); }
  var triggers = document.querySelectorAll(triggerSelector);
  var out = { trigger_count: triggers.length, panels: [] };
  for (var i = 0; i < triggers.length; i++) {
    var trig = triggers[i];
    try {
      trig.click();
    } catch (e) {
      // best-effort: surface as un-found and move on
      out.panels.push({ index: i, html: '', found: false, error: String(e) });
      continue;
    }
    // Poll for the panel to appear. The Nth panel is matched by Nth-of-
    // querySelectorAll order so pre-rendered + lazy fixtures both work.
    var deadline = Date.now() + settleMs;
    var panelEl = null;
    while (Date.now() < deadline) {
      var panels = document.querySelectorAll(panelSelector);
      if (panels.length > i) {
        panelEl = panels[i];
        if (panelEl && panelEl.innerHTML && panelEl.innerHTML.length > 0) {
          break;
        }
      }
      await sleep(pollMs);
    }
    if (panelEl) {
      out.panels.push({
        index: i,
        html: panelEl.outerHTML || '',
        found: true,
      });
    } else {
      out.panels.push({ index: i, html: '', found: false });
    }
  }
  return out;
})(%s, %s, %s, %s)
""".strip()


# ``_JS_VIRTUAL_LIST_HARVEST`` drives the ``--virtual-list`` flag for
# F005. The scroll-accumulate-dedupe algorithm (ported from the retired
# content-scrape package during the web-agent merge) runs as a single
# CDP-evaluated shim: one round-trip per pass; the loop continues until
# ``max_idle_passes`` consecutive iterations add zero new keys OR
# ``max_iterations`` is hit.
#
# Key resolution: read the configured ``key_attr`` from the row element.
# Fallback: first 40 chars of ``innerText`` (Python 3.7+ dict insertion
# order preserves first-seen order, mirroring the original).
#
# Returns ``{rows: [{key, text}], passes, terminated}`` so the Python
# side can surface the iteration counts in the JSON envelope.
_JS_VIRTUAL_LIST_HARVEST = r"""
(async function(containerSelector, rowSelector, keyAttr, settleMs, maxIters, maxIdle) {
  function sleep(ms) { return new Promise(function(r) { setTimeout(r, ms); }); }
  var container = document.querySelector(containerSelector);
  if (!container) {
    return { rows: [], passes: 0, terminated: 'no-container' };
  }
  var seen = new Map();
  var idle = 0;
  var passes = 0;
  for (var iter = 0; iter < maxIters; iter++) {
    passes = iter + 1;
    var before = seen.size;
    var rows = container.querySelectorAll(rowSelector);
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var key = row.getAttribute(keyAttr);
      if (!key) {
        var t = (row.innerText || '').slice(0, 40);
        if (!t) continue;
        key = t;
      }
      if (!seen.has(key)) {
        seen.set(key, row.innerText || '');
      }
    }
    // Scroll forward by one viewport height of the container.
    try {
      container.scrollTop = container.scrollTop + container.clientHeight;
    } catch (e) {
      // hostile page; bail out with what we have
      break;
    }
    await sleep(settleMs);
    if (seen.size === before) {
      idle += 1;
      if (idle >= maxIdle) {
        var rowsOut = [];
        seen.forEach(function(v, k) { rowsOut.push({ key: k, text: v }); });
        return { rows: rowsOut, passes: passes, terminated: 'idle' };
      }
    } else {
      idle = 0;
    }
  }
  var rowsOut2 = [];
  seen.forEach(function(v, k) { rowsOut2.push({ key: k, text: v }); });
  return { rows: rowsOut2, passes: passes, terminated: 'max-iterations' };
})(%s, %s, %s, %s, %s, %s)
""".strip()


# ── JSON helpers ───────────────────────────────────────────────────────


_HARD_EXIT_DISABLED_ENV = "COWORK_WEB_AGENT_NO_HARD_EXIT"


def _hard_exit(code: int) -> None:
    """Flush stdout/stderr and ``os._exit`` with the given code.

    A clean ``sys.exit`` blocks the CLI for ~60s on Windows because the
    BrowserSession we attached to leaves a watchdog graph (auto-reconnect
    task, event-bus consumers, CDP WebSocket reader) attached to the
    asyncio loop. ``asyncio.run()`` returns once our ``_navigate_core``
    coroutine completes, but the underlying loop's
    ``run_until_complete()`` still has those tasks pending, and on the
    Windows proactor loop the shutdown drain hangs.

    The Chromium process is NOT a child of this Python process's loop —
    it was spawned by the launcher subprocess via OS-level CreateProcess
    — so ``os._exit`` is safe: Chromium keeps running for the next
    ``attach()`` call. The local in-process bookkeeping (event bus,
    asyncio tasks) is torn down by the OS along with the process.

    Behave step defs invoke the CLI in-process via
    ``contextlib.redirect_stdout``-style helpers — they cannot tolerate
    ``os._exit`` because it would terminate the test runner. The
    ``COWORK_WEB_AGENT_NO_HARD_EXIT`` env var (set by behave's
    ``before_scenario`` / the in-process step helpers) downgrades this
    to ``sys.exit``, which raises ``SystemExit`` cleanly.
    """
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        sys.stderr.flush()
    except Exception:
        pass

    if os.environ.get(_HARD_EXIT_DISABLED_ENV):
        sys.exit(code)
    import os as _os

    _os._exit(code)


def _emit_json(payload: dict, *, exit_code: int = 0) -> None:
    """Print payload as JSON, signal completion to the wrapper, ``sys.exit``.

    See ``_signal_done_and_exit`` for the wrapper-thread story. Unit
    tests bypass the wrapper entirely (the module-level handles are
    None), so this still raises a clean ``SystemExit`` testable via
    ``pytest.raises``.
    """
    sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    _signal_done_and_exit(exit_code)


def _emit_error(
    err: BaseException,
    *,
    use_json: bool,
    extra: Optional[dict] = None,
) -> None:
    """Emit a structured error envelope; signal completion; ``sys.exit(1)``."""
    if use_json:
        envelope = {
            "success": False,
            "error": str(err),
            "error_type": type(err).__name__,
        }
        if extra:
            envelope.update(extra)
        sys.stderr.write(json.dumps(envelope, indent=2, default=str) + "\n")
    else:
        sys.stderr.write(f"{type(err).__name__}: {err}\n")
    _signal_done_and_exit(1)


# ── Browser-attach plumbing ────────────────────────────────────────────


def _attach_browser_session(cdp_endpoint: str):
    """Attach a fresh ``BrowserSession`` over CDP to an existing Chromium.

    Imported lazily — the vendored ``BrowserSession`` pulls in cdp-use,
    bubus, etc., and we don't want CLI ``--help`` invocations paying
    that import cost.
    """
    from src.web_agent.browser import BrowserSession

    session = BrowserSession(cdp_url=cdp_endpoint)
    return session


async def _start_session(session: Any) -> None:
    """Drive ``BrowserSession.start()`` to completion (CDP attach + watchdogs)."""
    await session.start()


async def _navigate(session: Any, url: str) -> None:
    """Drive ``BrowserSession.navigate_to(url)`` to completion."""
    await session.navigate_to(url)


async def _resolve_selector(session: Any, selector: str) -> dict:
    """Execute the JS shim and return the resolved element descriptor.

    Raises ``ValueError`` when the selector matches nothing.
    """
    script = _JS_RESOLVE_SELECTOR % json.dumps(selector)
    result = await _runtime_eval(session, script)
    if result is None:
        raise ValueError(f"Selector did not match any element: {selector!r}")
    return result


async def _runtime_eval(session: Any, script: str) -> Any:
    """Evaluate a JS expression and return the unwrapped Python value.

    Wraps ``Runtime.evaluate`` with ``returnByValue=True`` so the CDP
    response carries a JSON-able value (not a remote-object handle).
    Raises ``RuntimeError`` if the page-side evaluation throws.
    """
    cdp_session = await session.get_or_create_cdp_session(focus=True)

    async def _send() -> Any:
        response = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={
                "expression": script,
                "returnByValue": True,
                "awaitPromise": True,
            },
            session_id=cdp_session.session_id,
        )
        if "exceptionDetails" in response:
            details = response["exceptionDetails"]
            text = (
                details.get("exception", {}).get("description")
                or details.get("text")
                or "Runtime.evaluate raised"
            )
            raise RuntimeError(f"page-side evaluation failed: {text}")
        return response.get("result", {}).get("value")

    try:
        return await asyncio.wait_for(_send(), timeout=_RUNTIME_EVAL_TIMEOUT_S)
    except TimeoutError as exc:
        raise RuntimeError(
            f"Runtime.evaluate timed out after {_RUNTIME_EVAL_TIMEOUT_S}s"
        ) from exc


async def _accessibility_markdown(session: Any) -> str:
    """Return a deterministic markdown rendering of the current page.

    The vendored ``BrowserSession.get_state_as_text()`` is tuned for the
    upstream LLM agent loop — it filters out non-interactive elements
    (headings, paragraphs, plain divs) and returns ``"Empty DOM tree"``
    for any page lacking buttons/inputs/links. Our primitive-CLI test
    contract requires the snapshot to include things like H1 text and
    static markers (``MARKER_VISIBLE``), so we render via a small JS
    shim that walks the DOM and emits headings + visible interactive
    elements + the body's text content as markdown.

    The shim is deliberately minimal: ``H1..H6`` map to ``# .. ######``,
    visible buttons/inputs/textareas/anchors get a tagged line each,
    and the trailing ``--- body text ---`` block carries the raw
    ``innerText`` so static-page assertions (``"DETERMINISTIC FIXTURE
    BODY"``, ``"Welcome to the Web Agent fixture"``) round-trip cleanly.

    Notes:

    * We DO NOT reuse ``DOMTreeSerializer`` — it pruned all our test
      pages to nothing during F012's first end-to-end run. F015's
      ``web-extract`` will need a richer extractor (probably
      ``dom.markdown_extractor.extract_clean_markdown``), but for
      F012/F013/F014 the simple shim below is the right granularity.
    * Visibility check: skip elements with ``display:none`` /
      ``visibility:hidden``. Critical so that ``MARKER_VISIBLE`` only
      appears AFTER the F013 click toggles ``display`` to ``block``.
    """
    return await _runtime_eval(session, _JS_SIMPLE_MARKDOWN)


async def _press_enter_via_cdp(session: Any) -> None:
    """Dispatch a real Enter keypress through CDP so form handlers fire.

    Using ``Runtime.evaluate`` with ``KeyboardEvent`` does NOT trigger
    form submission (synthetic events have ``isTrusted=false`` and most
    browsers ignore them for default actions). The vendored
    ``Input.dispatchKeyEvent`` flow does fire the trusted Enter — this
    is the same path ``DefaultActionWatchdog._type_to_page`` uses for
    typed text. Reused here verbatim for Enter only.
    """
    cdp_session = await session.get_or_create_cdp_session(focus=True)
    # rawKeyDown + char + keyUp triplet — minimum to trigger a "real"
    # Enter. Mirrors the watchdog's per-character typing routine.
    await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
        params={
            "type": "rawKeyDown",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
            "key": "Enter",
            "code": "Enter",
            "text": "\r",
            "unmodifiedText": "\r",
        },
        session_id=cdp_session.session_id,
    )
    await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
        params={
            "type": "char",
            "key": "Enter",
            "code": "Enter",
            "text": "\r",
            "unmodifiedText": "\r",
        },
        session_id=cdp_session.session_id,
    )
    await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
        params={
            "type": "keyUp",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
            "key": "Enter",
            "code": "Enter",
        },
        session_id=cdp_session.session_id,
    )


async def _current_url_and_title(session: Any) -> tuple[str, str]:
    """Read URL + title without going through the heavy state-summary path."""
    url = await session.get_current_page_url()
    title = await session.get_current_page_title()
    return url, title


# ── Argparse builders ──────────────────────────────────────────────────


def _build_navigate_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web-navigate",
        description="Open a URL in a fresh or existing browser session.",
    )
    p.add_argument("url", help="URL to navigate to (http(s):// or file://).")
    p.add_argument(
        "--session-id",
        default=None,
        help="Existing session id; default is to start a new session.",
    )
    p.add_argument(
        "--new-session",
        action="store_true",
        help="Force a new session even if --session-id is given.",
    )
    p.add_argument(
        "--timeout-s",
        type=float,
        default=30.0,
        help="Per-navigation wall-clock timeout (seconds).",
    )
    p.add_argument(
        "--record",
        action="store_true",
        help=(
            "Mark a new session for stepwise screenshot recording (F017). "
            "Each subsequent primitive captures a viewport PNG to "
            "files/screenshots/<sid>/<step>.png. Use web-session-record-compile "
            "to assemble the GIF. No-op when attaching to an existing session."
        ),
    )
    _add_auth_aware_flags(p, default=True)
    p.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Emit a JSON envelope on stdout / stderr.",
    )
    return p


def _add_auth_aware_flags(p: argparse.ArgumentParser, *, default: bool) -> None:
    """Register the mutually-exclusive --auth-aware / --no-auth-aware pair.

    F007: when ``default=True`` the verb runs auth-wall detection after a
    successful action and emits a stderr notice + records a failure
    pattern if any signal fires. ``--no-auth-aware`` opts out. When
    ``default=False`` (web-click, web-extract) the verb skips detection
    by default to preserve mid-flow speed; ``--auth-aware`` opts in.
    """
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--auth-aware",
        dest="auth_aware",
        action="store_true",
        default=default,
        help=(
            "Run auth-wall detection after the action completes. On "
            "match, emit a stderr notice and record a failure pattern "
            "for the domain. Default: ON for web-navigate, OFF for "
            "web-click / web-extract."
        ),
    )
    group.add_argument(
        "--no-auth-aware",
        dest="auth_aware",
        action="store_false",
        help="Suppress auth-wall detection for this invocation.",
    )


def _build_click_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web-click",
        description="Click an element addressed by CSS / XPath / ARIA selector.",
    )
    p.add_argument(
        "selector",
        help="CSS selector (default), 'xpath=…', or 'role=button[name=Submit]'.",
    )
    p.add_argument(
        "--session-id",
        required=True,
        help="Active session id (web-click never starts a new session).",
    )
    p.add_argument(
        "--timeout-s",
        type=float,
        default=10.0,
        help="Per-click wall-clock timeout (seconds).",
    )
    p.add_argument(
        "--intent",
        default=None,
        help=(
            "F004: human label for this click action (e.g. 'login_submit'). "
            "When provided, a successful click records a candidate recipe in "
            "web_agent_recipe. Omitting this flag skips capture entirely — "
            "no implicit intent guessing."
        ),
    )
    p.add_argument(
        "--domain",
        default=None,
        help=(
            "F004: site domain for the recipe (e.g. 'example.com'). "
            "When omitted, derived from the session's current_url via urlparse. "
            "Only relevant when --intent is also passed."
        ),
    )
    _add_auth_aware_flags(p, default=False)
    p.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Emit a JSON envelope on stdout / stderr.",
    )
    return p


def _build_type_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web-type",
        description="Focus an element and type text, optionally pressing Enter.",
    )
    p.add_argument("selector", help="CSS / xpath= / role= selector for the field.")
    p.add_argument("text", help="Text to type.")
    p.add_argument(
        "--session-id",
        required=True,
        help="Active session id (web-type never starts a new session).",
    )
    p.add_argument(
        "--clear-first",
        action="store_true",
        help="Clear the field before typing.",
    )
    p.add_argument(
        "--press-enter",
        action="store_true",
        help="Send a CDP Enter keypress after typing.",
    )
    p.add_argument(
        "--intent",
        default=None,
        help=(
            "F004: human label for this type action (e.g. 'enter_username'). "
            "When provided, a successful type records a candidate recipe in "
            "web_agent_recipe. Omitting this flag skips capture entirely — "
            "no implicit intent guessing."
        ),
    )
    p.add_argument(
        "--domain",
        default=None,
        help=(
            "F004: site domain for the recipe (e.g. 'example.com'). "
            "When omitted, derived from the session's current_url via urlparse. "
            "Only relevant when --intent is also passed."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Emit a JSON envelope on stdout / stderr.",
    )
    return p


def _build_extract_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web-extract",
        description=(
            "Extract page content (or a selector subtree) as markdown / text / html."
        ),
    )
    p.add_argument(
        "--session-id",
        required=True,
        help="Active session id (web-extract never starts a new session).",
    )
    p.add_argument(
        "--selector",
        default=None,
        help=(
            "Optional CSS / xpath= / role= selector. With no selector the "
            "entire visible page is extracted."
        ),
    )
    p.add_argument(
        "--format",
        choices=("markdown", "text", "html"),
        default="markdown",
        help=(
            "Output format. ``markdown`` (default) reuses the same shim as "
            "web-navigate's accessibility_tree. ``text`` returns trimmed "
            "innerText. ``html`` returns outerHTML."
        ),
    )
    p.add_argument(
        "--extract-mode",
        choices=("tree", "article"),
        default="tree",
        dest="extract_mode",
        help=(
            "Extraction mode. ``tree`` (default) walks the accessibility "
            "tree / selector subtree and renders via the existing format "
            "shim (see --format). ``article`` fetches the live document "
            "HTML via DOM.getOuterHTML and runs it through trafilatura to "
            "strip nav / footer / aside chrome; --selector and --format "
            "are ignored in this mode (output is always markdown)."
        ),
    )
    p.add_argument(
        "--tabs",
        action="append",
        default=None,
        metavar="TRIGGER:PANEL",
        help=(
            "Repeatable. For each ``trigger_selector:panel_selector`` spec, "
            "click every element matching ``trigger_selector`` and capture "
            "the matching panel's outerHTML, converted to markdown. "
            "Captures pre-rendered hidden panels and lazy click-driven "
            "panels alike (the click is a no-op for already-visible "
            "content). Empty trigger list falls back to the single-extract "
            "path. Overrides --extract-mode / --selector / --format."
        ),
    )
    p.add_argument(
        "--tabs-settle-ms",
        type=int,
        default=1500,
        dest="tabs_settle_ms",
        help=(
            "Maximum ms to wait for a tab panel to appear after clicking "
            "its trigger (default: 1500)."
        ),
    )
    p.add_argument(
        "--tabs-poll-ms",
        type=int,
        default=100,
        dest="tabs_poll_ms",
        help=(
            "Poll interval ms while waiting for a tab panel to appear (default: 100)."
        ),
    )
    p.add_argument(
        "--virtual-list",
        default=None,
        dest="virtual_list",
        metavar="CONTAINER:ROW:KEY_ATTR",
        help=(
            "Harvest a virtualized list. Format: "
            "``container_selector:row_selector:key_attr``. Scrolls the "
            "container forward by clientHeight per pass, dedupes rows by "
            "``key_attr`` (fallback: first 40 chars of innerText), stops "
            "after --vl-max-idle consecutive idle passes or "
            "--vl-max-iterations. Returns a JSON list of {key, text} in "
            "insertion order. Overrides --extract-mode / --selector / "
            "--format / --tabs."
        ),
    )
    p.add_argument(
        "--vl-settle-ms",
        type=int,
        default=200,
        dest="vl_settle_ms",
        help="ms to wait after each scroll for the virtualizer to render (default: 200).",
    )
    p.add_argument(
        "--vl-max-iterations",
        type=int,
        default=100,
        dest="vl_max_iterations",
        help="Hard cap on scroll iterations (default: 100).",
    )
    p.add_argument(
        "--vl-max-idle",
        type=int,
        default=3,
        dest="vl_max_idle",
        help="Stop after this many consecutive passes without a new key (default: 3).",
    )
    _add_auth_aware_flags(p, default=False)
    p.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Emit a JSON envelope on stdout / stderr.",
    )
    return p


def _build_screenshot_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web-screenshot",
        description=(
            "Capture a viewport-only or full-page PNG via CDP and write to disk."
        ),
    )
    p.add_argument(
        "--session-id",
        required=True,
        help="Active session id (web-screenshot never starts a new session).",
    )
    p.add_argument(
        "--path",
        default=None,
        help=(
            "Output path. Defaults to "
            "files/screenshots/web_agent_<session_id>_<UTC>.png."
        ),
    )
    p.add_argument(
        "--full-page",
        action="store_true",
        help=(
            "Capture the full scrollable page (CDP captureBeyondViewport=True) "
            "instead of just the viewport."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Emit a JSON envelope on stdout / stderr.",
    )
    return p


def _build_record_compile_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web-session-record-compile",
        description=(
            "Compile per-step PNGs from a recording session into an animated GIF."
        ),
    )
    p.add_argument(
        "--session-id",
        required=True,
        help="Recording session id whose PNGs should be compiled.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Emit a JSON envelope on stdout / stderr.",
    )
    return p


def _build_session_close_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web-session-close",
        description=(
            "Close an active web-agent session: terminate Chromium, mark "
            "the row's status, emit the F018 web-session-end marker, and "
            "fire F019 memory integration."
        ),
    )
    p.add_argument(
        "session_id",
        help="Session id to close (positional).",
    )
    p.add_argument(
        "--status",
        default="closed",
        choices=("closed", "crashed", "done", "fail"),
        help=(
            "Terminal status to record. ``closed`` (default) and ``done`` "
            "fire memory at importance 5; ``crashed`` and ``fail`` at "
            "importance 6."
        ),
    )
    p.add_argument(
        "--failure-mode",
        default=None,
        dest="failure_mode",
        choices=(
            "captcha",
            "auth_wall",
            "anti_bot",
            "broken_dom",
            "rate_limited",
            "network_timeout",
            "other",
        ),
        help=(
            "F006: failure mode to record when --status is 'fail' or 'crashed'. "
            "Only recorded when the session has at least one navigated URL. "
            "Must be one of the constrained list."
        ),
    )
    p.add_argument(
        "--with-judge",
        action="store_true",
        dest="with_judge",
        default=False,
        help=(
            "F007: after closing, dispatch the web-judge subagent to verify "
            "the session actually met its goal. The verdict is persisted to "
            "web_agent_recipe.verdict_json for all recipes in this session. "
            "If the judge returns satisfied=false with confidence>=0.8, all "
            "recipes for the session are deleted. Skippable via "
            "WEB_AGENT_SKIP_JUDGE=1 (used by tests)."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Emit a JSON envelope on stdout / stderr.",
    )
    return p


def _build_session_list_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="web-session-list",
        description=(
            "List recorded web-agent sessions, filtered by status. Reads "
            "the registry only — no Chromium attach."
        ),
    )
    p.add_argument(
        "--status",
        default="active",
        choices=("active", "closed", "crashed", "all"),
        help="Filter by status (default: active). Use ``all`` for every row.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Emit a JSON envelope on stdout / stderr.",
    )
    return p


# ── Async cores ────────────────────────────────────────────────────────


def _record_step_marker_safe(
    mgr: Optional[Any],
    session_id: str,
    action: str,
) -> None:
    """Best-effort F018 ``[BOUNDARY:web-session-step:*]`` emission.

    Wraps ``SessionManager.record_step_marker`` so the per-session
    counter is bumped atomically (BEGIN IMMEDIATE inside the manager)
    and the marker text is logged at INFO level. Failures are warned
    but never propagate — the marker is observability scaffolding and
    must never break the user-visible primitive.
    """
    if mgr is None:
        return
    try:
        mgr.record_step_marker(session_id, action)
    except Exception as exc:  # noqa: BLE001 — best-effort marker
        logger.warning(
            f"web-session-step marker failed for {session_id}/{action}: "
            f"{type(exc).__name__}: {exc}"
        )


def _record_current_url_safe(
    mgr: Optional[Any],
    session_id: str,
    url: Optional[str],
) -> None:
    """Best-effort F025 ``current_url`` stash into ``metadata_json``.

    Wraps ``SessionManager.record_current_url`` so the latest known URL
    survives into ``close()``'s WhatsApp message body. Failures are
    warned but never propagate — the URL stash is observability
    scaffolding (it tells Pete which page the long-running session
    finished on), never load-bearing.
    """
    if mgr is None or not url:
        return
    try:
        mgr.record_current_url(session_id, url)
    except Exception as exc:  # noqa: BLE001 — best-effort marker
        logger.warning(
            f"current_url stash failed for {session_id}: {type(exc).__name__}: {exc}"
        )


def _capture_recipe_if_intent(
    *,
    session_id: str,
    selector: str,
    intent: Optional[str],
    domain: Optional[str],
    current_url: Optional[str],
    mgr: Optional[Any],
) -> None:
    """F004: Record a candidate recipe when --intent is explicitly provided.

    Only fires when ``intent`` is not None. Derives ``domain`` from
    ``current_url`` if not supplied explicitly. Scrubs the selector against
    ``WEB_AGENT_SENSITIVE_DATA`` (JSON array env var) and skips capture if
    any sensitive value appears as a substring. Failures are warned but never
    propagate — recipe capture is best-effort observability scaffolding.
    """
    if intent is None:
        return  # No intent → no capture (no implicit guessing)

    # Derive domain from current_url if not provided
    resolved_domain = domain
    if not resolved_domain and current_url:
        try:
            parsed = urlparse(current_url)
            resolved_domain = parsed.netloc
        except Exception as exc:
            logger.warning("F004 could not parse current_url %r: %s", current_url, exc)

    if not resolved_domain:
        logger.warning(
            "F004 cannot capture recipe for intent=%r: no domain available "
            "(pass --domain or navigate to a page first)",
            intent,
        )
        return

    # Sensitive-data scrubbing
    sensitive_raw = os.environ.get("WEB_AGENT_SENSITIVE_DATA", "[]")
    try:
        sensitive_values: list[str] = json.loads(sensitive_raw)
    except (ValueError, TypeError):
        sensitive_values = []

    if any(v in selector for v in sensitive_values if v):
        logger.debug("F004 Skipping recipe capture: selector contains sensitive data")
        return

    try:
        from src.web_agent.recipes import RecipeStore

        store = RecipeStore()
        store.upsert_recipe(
            domain=resolved_domain,
            intent=intent,
            selector=selector,
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort capture
        logger.warning(
            "F004 recipe capture failed for %s/%s: %s: %s",
            resolved_domain,
            intent,
            type(exc).__name__,
            exc,
        )


async def _run_auth_wall_check(
    *,
    browser: Any,
    session_id: str,
    auth_aware: bool,
) -> Optional[str]:
    """F007: detect an auth wall and report it (stderr + failure record).

    No-op when ``auth_aware`` is ``False``. When ``True``:

    * Calls :func:`detect_auth_wall` on the live browser session.
    * If a signal fires:
        * Emits a one-line warning to stderr naming the signal and the
          recovery command (``web-await-auth --session-id <sid>``).
        * Records a ``mode='auth_wall'`` failure pattern for the
          domain via :class:`RecipeStore`. Domain is derived from the
          session's current URL; an empty netloc (e.g. ``about:blank``)
          skips the failure record but still emits the stderr notice.

    Returns:
        The matching signal name (e.g. ``"password_field"``) or ``None``
        if no signal fired or detection was disabled.

    Best-effort: any exception during detection or recording is logged
    at WARNING and swallowed — auth-wall is informational, not blocking.
    """
    if not auth_aware:
        return None

    try:
        from src.web_agent.browser.watchdogs.auth_wall_watchdog import (
            detect_auth_wall,
            register_response_tracker,
        )

        # Idempotent: only registers once per session. The listener
        # populates session._last_main_frame_status as CDP events
        # arrive — late enough to inform NEXT calls but not this one
        # unless a prior verb already attached.
        register_response_tracker(browser)

        signal = await detect_auth_wall(browser)
    except Exception as exc:  # noqa: BLE001 — best-effort detection
        logger.warning(
            "F007 auth-wall detection failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return None

    if signal is None:
        return None

    # Resolve the domain for the failure record.
    try:
        current_url = await browser.get_current_page_url()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "F007 auth-wall: could not read current URL (%s: %s)",
            type(exc).__name__,
            exc,
        )
        current_url = ""

    domain = ""
    if current_url:
        try:
            domain = (urlparse(current_url).netloc or "").lower()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "F007 auth-wall: urlparse(%r) raised %s: %s",
                current_url,
                type(exc).__name__,
                exc,
            )

    # Stderr notice: explicit so a human / agent reading the output
    # spots it immediately even with --json silencing stdout.
    sys.stderr.write(
        f"AUTH WALL DETECTED ({signal}) — run: "
        f"uv run web-await-auth --session-id {session_id}\n"
    )

    # Record the failure pattern if we have a domain.
    if domain:
        try:
            from src.web_agent.recipes import RecipeStore

            store = RecipeStore()
            store.record_failure(domain=domain, mode="auth_wall")
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "F007 auth-wall: record_failure(%r) raised %s: %s",
                domain,
                type(exc).__name__,
                exc,
            )

    return signal


async def _navigate_core(
    *,
    sess_meta: Session,
    url: str,
    timeout_s: float,
    fresh_browser: Optional[Any] = None,
    mgr: Optional[Any] = None,
    auth_aware: bool = True,
) -> dict:
    """Attach to an existing session row and navigate; return JSON-able payload.

    Session creation / attach happens OUTSIDE the event loop in the
    public ``navigate`` entry point — ``SessionManager.start()`` itself
    calls ``asyncio.run()`` to spawn Chromium, so we cannot nest it
    inside our own ``asyncio.run(_navigate_core(...))`` call.

    When ``fresh_browser`` is provided (the new-session path), reuse
    it directly — this avoids the cost of a second CDP attach AND
    sidesteps a Windows race where the Chromium subprocess sometimes
    rejects WebSocket connections from a second event loop spun up
    immediately after the launcher loop tore down.

    ``mgr`` (a SessionManager) is threaded in so the F017 recording
    hook can persist the per-step counter via SessionManager. Optional —
    omitting it disables recording (the unit-test path).
    """
    if fresh_browser is not None:
        browser = fresh_browser
        already_started = True
    else:
        browser = _attach_browser_session(sess_meta.cdp_endpoint)
        already_started = False
    try:
        if not already_started:
            await asyncio.wait_for(_start_session(browser), timeout=timeout_s)
        await asyncio.wait_for(_navigate(browser, url), timeout=timeout_s)
        current_url, page_title = await _current_url_and_title(browser)
        accessibility = await _accessibility_markdown(browser)
        # F007: detect auth wall AFTER the navigation lands but BEFORE
        # the session detaches, so the CDP session is still live for
        # the password-field DOM query.
        auth_wall_signal = await _run_auth_wall_check(
            browser=browser,
            session_id=sess_meta.session_id,
            auth_aware=auth_aware,
        )
        if mgr is not None:
            await _maybe_record_step(browser, sess_meta, mgr=mgr)
        # F018: stamp the step marker AFTER the main action lands so a
        # failed navigation never produces a misleading "step n: navigate"
        # marker for an action that didn't reach this point.
        _record_step_marker_safe(mgr, sess_meta.session_id, "navigate")
        # F025: stash latest URL so close()'s WhatsApp message body can
        # report where the session finished.
        _record_current_url_safe(mgr, sess_meta.session_id, current_url)
    finally:
        await _detach_quietly(browser)

    return {
        "session_id": sess_meta.session_id,
        "current_url": current_url,
        "page_title": page_title,
        "accessibility_tree": accessibility,
        "auth_wall": auth_wall_signal,
    }


async def _click_core(
    *,
    sess_meta: Session,
    selector: str,
    timeout_s: float,
    mgr: Optional[Any] = None,
    auth_aware: bool = False,
) -> dict:
    browser = _attach_browser_session(sess_meta.cdp_endpoint)
    try:
        await asyncio.wait_for(_start_session(browser), timeout=timeout_s)
        # _resolve_selector tags the element with data-cowork-resolved.
        resolved = await _resolve_selector(browser, selector)
        cowork_id = resolved["cowork_id"]
        click_script = _JS_DISPATCH_CLICK % json.dumps(cowork_id)
        click_result = await _runtime_eval(browser, click_script)
        if not click_result or not click_result.get("ok"):
            raise RuntimeError(
                f"click failed: {click_result.get('error') if click_result else 'no result'}"
            )
        # Settle: give any synchronous DOM mutation a moment to land
        # before snapshotting state. The fixtures are deterministic so
        # this is effectively immediate, but we yield once for safety.
        await asyncio.sleep(0.05)
        post_click_url, _ = await _current_url_and_title(browser)
        accessibility = await _accessibility_markdown(browser)
        # F007: auth-wall check (OFF by default for click — opt-in via
        # --auth-aware to preserve mid-flow speed).
        auth_wall_signal = await _run_auth_wall_check(
            browser=browser,
            session_id=sess_meta.session_id,
            auth_aware=auth_aware,
        )
        if mgr is not None:
            await _maybe_record_step(browser, sess_meta, mgr=mgr)
        _record_step_marker_safe(mgr, sess_meta.session_id, "click")
        # F025: stash post-click URL so close()'s WhatsApp message
        # body reflects the page the session ended up on.
        _record_current_url_safe(mgr, sess_meta.session_id, post_click_url)
    finally:
        await _detach_quietly(browser)

    return {
        "session_id": sess_meta.session_id,
        "success": True,
        "post_click_url": post_click_url,
        "accessibility_tree": accessibility,
        "auth_wall": auth_wall_signal,
    }


async def _type_core(
    *,
    sess_meta: Session,
    selector: str,
    text: str,
    clear_first: bool,
    press_enter: bool,
    mgr: Optional[Any] = None,
) -> dict:
    browser = _attach_browser_session(sess_meta.cdp_endpoint)
    try:
        await asyncio.wait_for(_start_session(browser), timeout=10.0)
        resolved = await _resolve_selector(browser, selector)
        cowork_id = resolved["cowork_id"]
        type_script = _JS_TYPE_INTO % (
            json.dumps(cowork_id),
            json.dumps(text),
            json.dumps(clear_first),
        )
        type_result = await _runtime_eval(browser, type_script)
        if not type_result or not type_result.get("ok"):
            raise RuntimeError(
                f"type failed: {type_result.get('error') if type_result else 'no result'}"
            )
        # Read back the current value FROM the DOM after typing — the
        # primitive's contract is "echo what the page actually saw".
        current_value = type_result.get("current_value", "")
        if press_enter:
            await _press_enter_via_cdp(browser)
            # Yield once so the form's submit handler can mutate the DOM
            # before subsequent calls inspect it.
            await asyncio.sleep(0.05)
        if mgr is not None:
            await _maybe_record_step(browser, sess_meta, mgr=mgr)
        _record_step_marker_safe(mgr, sess_meta.session_id, "type")
    finally:
        await _detach_quietly(browser)

    return {
        "session_id": sess_meta.session_id,
        "success": True,
        "current_value": current_value,
    }


async def _capture_screenshot_bytes(
    browser: Any,
    *,
    full_page: bool = False,
) -> bytes:
    """Capture a PNG via CDP ``Page.captureScreenshot`` and return raw bytes.

    Mirrors the vendored ``BrowserSession.take_screenshot`` plumbing
    inline so the recording hook (``_maybe_record_step``) and
    ``web-screenshot`` can share one CDP code path. Why inline rather
    than calling ``browser.take_screenshot``: ``take_screenshot`` is
    decorated by ``@observe_debug`` and writes to disk if a path is
    given; we want a lean bytes-returning helper for the recording
    path which composes its own filename.
    """
    import base64

    cdp_session = await browser.get_or_create_cdp_session(focus=True)
    params: dict[str, Any] = {
        "format": "png",
        "captureBeyondViewport": full_page,
    }
    result = await cdp_session.cdp_client.send.Page.captureScreenshot(
        params=params,  # type: ignore[arg-type]
        session_id=cdp_session.session_id,
    )
    if not result or "data" not in result:
        raise RuntimeError("Page.captureScreenshot returned no data")
    return base64.b64decode(result["data"])


def _parse_tabs_spec(spec: str) -> tuple[str, str]:
    """Parse a ``--tabs`` argument into ``(trigger_selector, panel_selector)``.

    The spec must contain exactly one ``:`` separator. Selectors may
    themselves contain colons (e.g. ``a[href*="https:"]``) — but the
    fixtures and Skill-recommended forms use simple ``.tab:.panel`` shapes,
    so the single-split rule is a deliberate trade-off for friendlier
    error messages over baroque flexibility.

    Raises
    ------
    ValueError
        If the spec does not contain exactly one ``:`` separator, or if
        either selector is empty.
    """
    if spec.count(":") != 1:
        raise ValueError(
            f"--tabs expects exactly one ':' separator, got {spec!r}. "
            "Format: 'trigger_selector:panel_selector'"
        )
    trigger, panel = spec.split(":", 1)
    if not trigger or not panel:
        raise ValueError(
            f"--tabs trigger and panel selectors must both be non-empty, got {spec!r}"
        )
    return trigger, panel


def _parse_virtual_list_spec(spec: str) -> tuple[str, str, str]:
    """Parse a ``--virtual-list`` argument.

    Format: ``container_selector:row_selector:key_attr``. ``key_attr``
    defaults to ``data-id`` when omitted (i.e. two-part input). Mirrors
    the original virtual-list spec contract carried over during the
    web-agent merge so existing recipes port over cleanly.

    Raises
    ------
    ValueError
        If the spec has fewer than two colon-separated fields, or
        either container/row selector is empty.
    """
    parts = spec.split(":", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(
            "--virtual-list expects 'container_selector:row_selector[:key_attr]'"
        )
    container = parts[0]
    row = parts[1]
    key_attr = parts[2] if len(parts) > 2 and parts[2] else "data-id"
    return container, row, key_attr


def _html_to_markdown(html: str) -> str:
    """Convert an HTML fragment to markdown via the same library used by
    ``src/web_agent/dom/markdown_extractor.py``.

    Lazy import so CLI ``--help`` doesn't pay the markdownify cost.
    """
    from markdownify import markdownify as md  # type: ignore[import-not-found]

    return md(
        html,
        heading_style="ATX",
        strip=["script", "style"],
        bullets="-",
        code_language="",
        escape_asterisks=False,
        escape_underscores=False,
        escape_misc=False,
        autolinks=False,
        default_title=False,
    )


async def _extract_core(
    *,
    sess_meta: Session,
    selector: Optional[str],
    fmt: str,
    extract_mode: str = "tree",
    tabs_specs: Optional[list[str]] = None,
    tabs_settle_ms: int = 1500,
    tabs_poll_ms: int = 100,
    virtual_list_spec: Optional[str] = None,
    vl_settle_ms: int = 200,
    vl_max_iterations: int = 100,
    vl_max_idle: int = 3,
    timeout_s: float = 10.0,
    mgr: Optional[Any] = None,
    auth_aware: bool = False,
) -> dict:
    """Attach, optionally resolve a selector subtree, return formatted content.

    No selector → whole-page extraction. With a selector, resolution
    reuses the same ``_JS_RESOLVE_SELECTOR`` shim that click/type use,
    so the same CSS / xpath= / role= prefixes are recognised.

    ``extract_mode='article'`` short-circuits the tree path: we fetch the
    full document via ``BrowserSession.page_html()`` (a CDP
    ``DOM.getOuterHTML`` on the root), then run trafilatura over it via
    :func:`src.web_agent.extract.article.extract_article`. ``selector``
    and ``fmt`` are ignored in this mode; output is always markdown.

    ``virtual_list_spec`` (F005) takes priority over ``tabs_specs`` and
    ``extract_mode``: when set, the function dispatches the scroll-
    accumulate-dedupe loop and returns the JSON-shaped envelope.

    ``tabs_specs`` (F004) is the second branch: a non-empty list of
    ``trigger:panel`` specs drives the click+poll+capture loop and
    returns concatenated per-panel markdown. An empty resolved trigger
    list silently falls through to the single-extract path (per
    F004 acceptance).
    """
    browser = _attach_browser_session(sess_meta.cdp_endpoint)
    try:
        await asyncio.wait_for(_start_session(browser), timeout=timeout_s)

        if virtual_list_spec is not None:
            # F005 — virtual list harvest.
            container, row, key_attr = _parse_virtual_list_spec(virtual_list_spec)
            script = _JS_VIRTUAL_LIST_HARVEST % (
                json.dumps(container),
                json.dumps(row),
                json.dumps(key_attr),
                json.dumps(vl_settle_ms),
                json.dumps(vl_max_iterations),
                json.dumps(vl_max_idle),
            )
            result = await _runtime_eval(browser, script)
            rows = (result or {}).get("rows", [])
            auth_wall_signal = await _run_auth_wall_check(
                browser=browser,
                session_id=sess_meta.session_id,
                auth_aware=auth_aware,
            )
            _record_step_marker_safe(mgr, sess_meta.session_id, "extract")
            return {
                "session_id": sess_meta.session_id,
                "format": "json",
                "selector": None,
                "extract_mode": "virtual_list",
                "virtual_list": rows,
                "passes": (result or {}).get("passes", 0),
                "terminated": (result or {}).get("terminated", "unknown"),
                "auth_wall": auth_wall_signal,
            }

        if tabs_specs:
            # F004 — click triggers, poll for panels, accumulate markdown.
            sections: list[str] = []
            total_found = 0
            total_triggers = 0
            for spec in tabs_specs:
                trigger, panel = _parse_tabs_spec(spec)
                script = _JS_TABS_CLICK_AND_CAPTURE % (
                    json.dumps(trigger),
                    json.dumps(panel),
                    json.dumps(tabs_settle_ms),
                    json.dumps(tabs_poll_ms),
                )
                result = await _runtime_eval(browser, script)
                trigger_count = (result or {}).get("trigger_count", 0)
                total_triggers += trigger_count
                panels = (result or {}).get("panels", [])
                for panel_payload in panels:
                    if panel_payload.get("found") and panel_payload.get("html"):
                        sections.append(
                            _html_to_markdown(panel_payload["html"]).strip()
                        )
                        total_found += 1

            if total_triggers == 0:
                # Graceful fallthrough: no triggers matched any spec. Run
                # the existing single-extract markdown path (default fmt
                # / no selector) and tag the mode so callers can detect
                # the fallthrough.
                content = await _runtime_eval(browser, _JS_SIMPLE_MARKDOWN)
                if content is None:
                    content = ""
                auth_wall_signal = await _run_auth_wall_check(
                    browser=browser,
                    session_id=sess_meta.session_id,
                    auth_aware=auth_aware,
                )
                _record_step_marker_safe(mgr, sess_meta.session_id, "extract")
                return {
                    "session_id": sess_meta.session_id,
                    "format": "markdown",
                    "selector": None,
                    "extract_mode": "tabs",
                    "content": content,
                    "trigger_count": 0,
                    "panels_captured": 0,
                    "auth_wall": auth_wall_signal,
                }

            combined = "\n\n".join(sections)
            auth_wall_signal = await _run_auth_wall_check(
                browser=browser,
                session_id=sess_meta.session_id,
                auth_aware=auth_aware,
            )
            _record_step_marker_safe(mgr, sess_meta.session_id, "extract")
            return {
                "session_id": sess_meta.session_id,
                "format": "markdown",
                "selector": None,
                "extract_mode": "tabs",
                "content": combined,
                "trigger_count": total_triggers,
                "panels_captured": total_found,
                "auth_wall": auth_wall_signal,
            }

        if extract_mode == "article":
            # Article mode: full document HTML → trafilatura → markdown.
            # Imports lazy so CLI --help doesn't pay the trafilatura cost.
            from src.web_agent.extract.article import extract_article

            current_url, _ = await _current_url_and_title(browser)
            html = await browser.page_html()
            content = extract_article(html, url=current_url)
            auth_wall_signal = await _run_auth_wall_check(
                browser=browser,
                session_id=sess_meta.session_id,
                auth_aware=auth_aware,
            )
            _record_step_marker_safe(mgr, sess_meta.session_id, "extract")
            return {
                "session_id": sess_meta.session_id,
                "format": "markdown",
                "selector": None,
                "extract_mode": "article",
                "content": content,
                "auth_wall": auth_wall_signal,
            }

        cowork_id: Optional[str]
        if selector:
            resolved = await _resolve_selector(browser, selector)
            cowork_id = resolved["cowork_id"]
        else:
            cowork_id = None

        if fmt == "markdown":
            if cowork_id is None:
                content = await _runtime_eval(browser, _JS_SIMPLE_MARKDOWN)
            else:
                script = _JS_SIMPLE_MARKDOWN_FOR_SELECTOR % json.dumps(cowork_id)
                content = await _runtime_eval(browser, script)
        elif fmt == "text":
            if cowork_id is None:
                content = await _runtime_eval(browser, _JS_BODY_INNERTEXT)
            else:
                script = _JS_INNERTEXT_FOR_SELECTOR % json.dumps(cowork_id)
                content = await _runtime_eval(browser, script)
        elif fmt == "html":
            if cowork_id is None:
                content = await _runtime_eval(browser, _JS_BODY_OUTERHTML)
            else:
                script = _JS_OUTERHTML_FOR_SELECTOR % json.dumps(cowork_id)
                content = await _runtime_eval(browser, script)
        else:
            raise ValueError(f"unknown format: {fmt!r}")

        if content is None:
            raise RuntimeError(
                f"selector subtree extraction returned null (selector={selector!r})"
            )
        auth_wall_signal = await _run_auth_wall_check(
            browser=browser,
            session_id=sess_meta.session_id,
            auth_aware=auth_aware,
        )
        _record_step_marker_safe(mgr, sess_meta.session_id, "extract")
    finally:
        await _detach_quietly(browser)

    return {
        "session_id": sess_meta.session_id,
        "format": fmt,
        "selector": selector,
        "extract_mode": extract_mode,
        "content": content,
        "auth_wall": auth_wall_signal,
    }


async def _screenshot_core(
    *,
    sess_meta: Session,
    full_page: bool,
    out_path: Path,
    timeout_s: float = 30.0,
    mgr: Optional[Any] = None,
) -> dict:
    """Attach, capture a screenshot, write it to ``out_path``.

    Creates the parent directory if missing.
    """
    browser = _attach_browser_session(sess_meta.cdp_endpoint)
    try:
        await asyncio.wait_for(_start_session(browser), timeout=timeout_s)
        png_bytes = await _capture_screenshot_bytes(browser, full_page=full_page)
    finally:
        await _detach_quietly(browser)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(png_bytes)

    # F018: stamp the step marker after the PNG is on disk.
    _record_step_marker_safe(mgr, sess_meta.session_id, "screenshot")

    return {
        "session_id": sess_meta.session_id,
        "path": str(out_path.resolve()),
        "bytes": len(png_bytes),
        "full_page": full_page,
    }


async def _maybe_record_step(
    browser: Any,
    sess_meta: Session,
    *,
    mgr: Any,
) -> Optional[Path]:
    """If ``sess_meta`` is recording, capture a viewport PNG and bump the counter.

    Returns the on-disk path for the captured PNG (or None when the
    session isn't recording). Called by the navigate / click / type
    async cores after their main action lands. Failures are logged but
    do not propagate — recording is opt-in scaffolding for debugging
    and must never break the user-visible primitive.
    """
    if not sess_meta.recording:
        return None
    try:
        png_bytes = await _capture_screenshot_bytes(browser, full_page=False)
        return mgr.record_step(sess_meta.session_id, png_bytes)
    except Exception as exc:  # noqa: BLE001 — best-effort recording
        logger.warning(
            f"web-agent recording capture failed for {sess_meta.session_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        return None


async def _detach_quietly(browser: Any) -> None:
    """Best-effort detach — drain local state without killing Chromium.

    Three steps, each best-effort:

    1. Stop the BrowserSession's event bus. Drains pending tasks and
       shuts down the queue so consumers raise ``QueueShutDown``
       instead of waiting forever.
    2. Stop the root CDP client. Closes the local WebSocket so the
       reader task wakes up and exits.
    3. Cancel any ``_auto_reconnect`` task left dangling — without
       this, the task sits alive on the loop blocking
       ``asyncio.run()``'s shutdown drain.

    ``BrowserSession.kill()`` / ``.stop()`` would do all this AND
    terminate Chromium — which we do NOT want, because the whole
    point of SessionManager is keeping Chromium alive across CLI
    invocations. Hence the per-step manual teardown here.
    """
    try:
        await browser.event_bus.stop(clear=True, timeout=2)
    except Exception:  # noqa: BLE001 — best-effort
        pass
    try:
        client = getattr(browser, "_cdp_client_root", None)
        if client is not None:
            await asyncio.wait_for(client.stop(), timeout=_CDP_STOP_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — best-effort
        pass
    # Cancel any leftover tasks that reference this browser session,
    # so asyncio.run() can drain cleanly. The auto_reconnect task is
    # the main culprit — once event_bus is shut down it raises
    # QueueShutDown which surfaces as "Task exception was never
    # retrieved" but does NOT cancel the task itself.
    import asyncio as _asyncio

    for task in list(_asyncio.all_tasks()):
        if task is _asyncio.current_task():
            continue
        if not task.done():
            task.cancel()


# ── Public CLI entry points ────────────────────────────────────────────


def _run_cli_with_hard_exit(impl, argv: Optional[list[str]]) -> None:
    """Run a CLI ``impl`` directly. The impl is responsible for exiting.

    Each impl ends with ``_signal_done_and_exit(code)`` which writes
    the JSON envelope, then either ``sys.exit`` (when the env-var
    ``COWORK_WEB_AGENT_NO_HARD_EXIT`` is set, for unit tests / behave
    in-process callers) or ``os._exit`` (the production CLI path,
    which would otherwise hang for ~60s on Windows asyncio shutdown).

    This wrapper exists so subsequent batches can hook in pre/post
    behaviour (telemetry, metrics) without touching every impl.
    """
    impl(argv)


def _signal_done_and_exit(code: int) -> None:
    """Flush stdio, then exit with ``code``.

    The exit semantics are gated by ``COWORK_WEB_AGENT_NO_HARD_EXIT``:

    * Set (unit tests, behave step files): ``sys.exit(code)`` raises
      ``SystemExit`` for ``pytest.raises`` and the in-process step
      runner to catch.
    * Unset (production CLI): ``os._exit(code)`` terminates the
      process immediately, side-stepping asyncio's shutdown drain.

    Background: ``BrowserSession`` connected via CDP leaves a watchdog
    graph (auto-reconnect task, event-bus consumers, CDP WebSocket
    reader) attached to the asyncio loop. On the Windows proactor
    loop, ``asyncio.run()`` blocks for the full timeout waiting for
    that graph to drain. Chromium itself was spawned by the launcher
    subprocess via OS-level CreateProcess (NOT a child of this Python
    loop), so ``os._exit`` is safe — Chromium keeps running for the
    next ``attach()`` call.
    """
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        sys.stderr.flush()
    except Exception:
        pass
    if os.environ.get(_HARD_EXIT_DISABLED_ENV):
        sys.exit(code)
    import os as _os

    _os._exit(code)


def navigate(argv: Optional[list[str]] = None) -> None:
    """``web-navigate`` entry point — F012.

    Wrapped at the boundary by ``_run_cli_with_hard_exit`` so the
    process terminates via ``os._exit`` instead of falling through to
    asyncio's slow shutdown path. See ``_hard_exit`` for the rationale.
    """
    _run_cli_with_hard_exit(_navigate_impl, argv)


@log_call("BOUNDARY")
def _navigate_impl(argv: Optional[list[str]]) -> None:
    parser = _build_navigate_parser()
    args = parser.parse_args(argv)
    # Wrap the whole pipeline in a single asyncio.run() and exit
    # FROM INSIDE the coroutine — see _signal_done_and_exit. The
    # outer asyncio.run() will never return on Windows because the
    # BrowserSession watchdog graph keeps the loop alive.
    try:
        asyncio.run(_navigate_async(args))
    except SystemExit:
        # Re-raised when COWORK_WEB_AGENT_NO_HARD_EXIT is set (test
        # mode). Propagate so the caller (behave/_run_cli or the
        # unit tests) can assert on the exit code.
        raise
    except (SessionNotFoundError, SessionCrashedError) as exc:
        _emit_error(exc, use_json=args.use_json)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        _emit_error(exc, use_json=args.use_json)


async def _navigate_async(args: argparse.Namespace) -> None:
    """Resolve session, navigate, emit JSON, ``_signal_done_and_exit``.

    All work happens inside this single coroutine so that exit happens
    BEFORE asyncio.run()'s shutdown drain ever runs. ``os._exit`` from
    a coroutine kills the process cleanly even with a watchdog graph
    on the loop.
    """
    mgr = SessionManager()
    mgr.reap_stale()
    record_flag = bool(getattr(args, "record", False))
    if args.new_session or not args.session_id:
        sess_meta = mgr.start(record_screenshots=record_flag)
    else:
        # --record on attach is a no-op: recording is a property of the
        # session row, set at start time. Document this in --record's
        # help text rather than silently flipping mid-session.
        sess_meta = mgr.attach(args.session_id)
    payload = await _navigate_core(
        sess_meta=sess_meta,
        url=args.url,
        timeout_s=args.timeout_s,
        mgr=mgr,
        auth_aware=getattr(args, "auth_aware", True),
    )
    # Re-attach AFTER the async work to bump last_used_at.
    mgr.attach(sess_meta.session_id)

    if args.use_json:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(
            f"session_id: {payload['session_id']}\n"
            f"current_url: {payload['current_url']}\n"
            f"page_title: {payload['page_title']}\n\n"
            f"{payload['accessibility_tree']}\n"
        )
    _signal_done_and_exit(0)


def click(argv: Optional[list[str]] = None) -> None:
    """``web-click`` entry point — F013.

    Wrapped at the boundary by ``_run_cli_with_hard_exit`` — see
    ``navigate`` for rationale.
    """
    _run_cli_with_hard_exit(_click_impl, argv)


@log_call("BOUNDARY")
def _click_impl(argv: Optional[list[str]]) -> None:
    parser = _build_click_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(_click_async(args))
    except SystemExit:
        raise
    except (SessionNotFoundError, SessionCrashedError) as exc:
        _emit_error(exc, use_json=args.use_json)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        _emit_error(exc, use_json=args.use_json)


async def _click_async(args: argparse.Namespace) -> None:
    mgr = SessionManager()
    mgr.reap_stale()
    sess_meta = mgr.attach(args.session_id)
    payload = await _click_core(
        sess_meta=sess_meta,
        selector=args.selector,
        timeout_s=args.timeout_s,
        mgr=mgr,
        auth_aware=getattr(args, "auth_aware", False),
    )
    mgr.attach(args.session_id)

    # F004: capture recipe when --intent is explicitly provided
    _capture_recipe_if_intent(
        session_id=args.session_id,
        selector=args.selector,
        intent=getattr(args, "intent", None),
        domain=getattr(args, "domain", None),
        current_url=payload.get("post_click_url"),
        mgr=mgr,
    )

    if args.use_json:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(
            f"session_id: {payload['session_id']}\n"
            f"success: {payload['success']}\n"
            f"post_click_url: {payload['post_click_url']}\n\n"
            f"{payload['accessibility_tree']}\n"
        )
    _signal_done_and_exit(0)


def type_(argv: Optional[list[str]] = None) -> None:
    """``web-type`` entry point — F014.

    Suffixed underscore avoids collision with the Python ``type`` builtin.
    The poetry shortcut still surfaces as ``web-type``.
    """
    _run_cli_with_hard_exit(_type_impl, argv)


@log_call("BOUNDARY")
def _type_impl(argv: Optional[list[str]]) -> None:
    parser = _build_type_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(_type_async(args))
    except SystemExit:
        raise
    except (SessionNotFoundError, SessionCrashedError) as exc:
        _emit_error(exc, use_json=args.use_json)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        _emit_error(exc, use_json=args.use_json)


async def _type_async(args: argparse.Namespace) -> None:
    mgr = SessionManager()
    mgr.reap_stale()
    sess_meta = mgr.attach(args.session_id)
    payload = await _type_core(
        sess_meta=sess_meta,
        selector=args.selector,
        text=args.text,
        clear_first=args.clear_first,
        press_enter=args.press_enter,
        mgr=mgr,
    )
    mgr.attach(args.session_id)

    # F004: capture recipe when --intent is explicitly provided.
    # For type, we don't have a post-action URL in the payload; derive from
    # the session's stashed current_url (written by _record_current_url_safe
    # on the most recent navigate/click).
    _capture_recipe_if_intent(
        session_id=args.session_id,
        selector=args.selector,
        intent=getattr(args, "intent", None),
        domain=getattr(args, "domain", None),
        current_url=None,  # will fall back to --domain or fail gracefully
        mgr=mgr,
    )

    if args.use_json:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(
            f"session_id: {payload['session_id']}\n"
            f"success: {payload['success']}\n"
            f"current_value: {payload['current_value']}\n"
        )
    _signal_done_and_exit(0)


def extract(argv: Optional[list[str]] = None) -> None:
    """``web-extract`` entry point — F015.

    Wrapped at the boundary by ``_run_cli_with_hard_exit`` — see
    ``navigate`` for rationale.
    """
    _run_cli_with_hard_exit(_extract_impl, argv)


@log_call("BOUNDARY")
def _extract_impl(argv: Optional[list[str]]) -> None:
    parser = _build_extract_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(_extract_async(args))
    except SystemExit:
        raise
    except TimeoutError:
        _emit_error(
            RuntimeError(f"web-extract timed out after {_EXTRACT_TOTAL_TIMEOUT_S}s"),
            use_json=args.use_json,
        )
    except (SessionNotFoundError, SessionCrashedError) as exc:
        _emit_error(exc, use_json=args.use_json)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        _emit_error(exc, use_json=args.use_json)


async def _extract_async(args: argparse.Namespace) -> None:
    async def _body() -> None:
        await _extract_async_body(args)

    await asyncio.wait_for(_body(), timeout=_EXTRACT_TOTAL_TIMEOUT_S)


async def _extract_async_body(args: argparse.Namespace) -> None:
    mgr = SessionManager()
    mgr.reap_stale()
    sess_meta = mgr.attach(args.session_id)
    payload = await _extract_core(
        sess_meta=sess_meta,
        selector=args.selector,
        fmt=args.format,
        extract_mode=args.extract_mode,
        tabs_specs=args.tabs,
        tabs_settle_ms=args.tabs_settle_ms,
        tabs_poll_ms=args.tabs_poll_ms,
        virtual_list_spec=args.virtual_list,
        vl_settle_ms=args.vl_settle_ms,
        vl_max_iterations=args.vl_max_iterations,
        vl_max_idle=args.vl_max_idle,
        mgr=mgr,
        auth_aware=getattr(args, "auth_aware", False),
    )
    mgr.attach(args.session_id)

    if args.use_json:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        if payload["extract_mode"] == "virtual_list":
            sys.stdout.write(
                f"session_id: {payload['session_id']}\n"
                f"extract_mode: {payload['extract_mode']}\n"
                f"passes: {payload['passes']}\n"
                f"terminated: {payload['terminated']}\n\n"
                f"{json.dumps(payload['virtual_list'], indent=2, default=str)}\n"
            )
        else:
            sys.stdout.write(
                f"session_id: {payload['session_id']}\n"
                f"format: {payload['format']}\n"
                f"selector: {payload['selector']}\n"
                f"extract_mode: {payload['extract_mode']}\n\n"
                f"{payload['content']}\n"
            )
    _signal_done_and_exit(0)


def screenshot(argv: Optional[list[str]] = None) -> None:
    """``web-screenshot`` entry point — F016."""
    _run_cli_with_hard_exit(_screenshot_impl, argv)


@log_call("BOUNDARY")
def _screenshot_impl(argv: Optional[list[str]]) -> None:
    parser = _build_screenshot_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(_screenshot_async(args))
    except SystemExit:
        raise
    except (SessionNotFoundError, SessionCrashedError) as exc:
        _emit_error(exc, use_json=args.use_json)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        _emit_error(exc, use_json=args.use_json)


def _default_screenshot_path(session_id: str) -> Path:
    """Build the default ``files/screenshots/web_agent_<sid>_<UTC>.png`` path.

    UTC timestamp uses the compact ``YYYYMMDDTHHMMSSZ`` form so the
    filename round-trips through Windows + POSIX without quoting.
    """
    from datetime import datetime, timezone

    project_root = Path(__file__).resolve().parents[2]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return project_root / "files" / "screenshots" / f"web_agent_{session_id}_{ts}.png"


async def _screenshot_async(args: argparse.Namespace) -> None:
    mgr = SessionManager()
    mgr.reap_stale()
    sess_meta = mgr.attach(args.session_id)

    out_path = (
        Path(args.path).resolve()
        if args.path
        else _default_screenshot_path(sess_meta.session_id)
    )
    payload = await _screenshot_core(
        sess_meta=sess_meta,
        full_page=args.full_page,
        out_path=out_path,
        mgr=mgr,
    )
    mgr.attach(args.session_id)

    if args.use_json:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(
            f"session_id: {payload['session_id']}\n"
            f"path: {payload['path']}\n"
            f"bytes: {payload['bytes']}\n"
            f"full_page: {payload['full_page']}\n"
        )
    _signal_done_and_exit(0)


def record_compile(argv: Optional[list[str]] = None) -> None:
    """``web-session-record-compile`` entry point — F017.

    Compiles per-step PNGs from a recording session into an animated GIF
    via Pillow (the vendored ``agent/gif.py`` is a no-op stub since
    cowork's reframe deleted the autonomous agent loop).
    """
    _run_cli_with_hard_exit(_record_compile_impl, argv)


@log_call("BOUNDARY")
def _record_compile_impl(argv: Optional[list[str]]) -> None:
    parser = _build_record_compile_parser()
    args = parser.parse_args(argv)
    try:
        mgr = SessionManager()
        gif_path, frame_count = mgr.compile_gif(args.session_id)
    except SystemExit:
        raise
    except (SessionNotFoundError, SessionCrashedError) as exc:
        _emit_error(exc, use_json=args.use_json)
        return
    except Exception as exc:  # noqa: BLE001 — CLI surface
        _emit_error(exc, use_json=args.use_json)
        return

    # F018: stamp a record-compile step marker so the compile shows up
    # in the same per-session step-marker grep recipe as the primitives.
    _record_step_marker_safe(mgr, args.session_id, "record-compile")

    payload = {
        "session_id": args.session_id,
        "gif_path": str(gif_path.resolve()),
        "frame_count": frame_count,
    }
    if args.use_json:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(
            f"session_id: {payload['session_id']}\n"
            f"gif_path: {payload['gif_path']}\n"
            f"frame_count: {payload['frame_count']}\n"
        )
    _signal_done_and_exit(0)


def session_close(argv: Optional[list[str]] = None) -> None:
    """``web-session-close`` entry point — F018.

    Thin SessionManager wrapper: no Chromium attach, just the row
    update + F018 / F019 hooks. The os._exit dance the primitives need
    isn't relevant here because there's no asyncio event loop kept
    alive by a watchdog graph — pure Postgres + a subprocess.
    """
    _session_close_impl(argv)


@log_call("BOUNDARY")
def _session_close_impl(argv: Optional[list[str]]) -> None:
    parser = _build_session_close_parser()
    args = parser.parse_args(argv)
    try:
        mgr = SessionManager()
        mgr.close(
            args.session_id,
            status=args.status,
            failure_mode=getattr(args, "failure_mode", None),
            with_judge=getattr(args, "with_judge", False),
        )
    except SessionNotFoundError as exc:
        if args.use_json:
            sys.stderr.write(
                json.dumps(
                    {
                        "success": False,
                        "session_id": args.session_id,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                    indent=2,
                )
                + "\n"
            )
        else:
            sys.stderr.write(f"SessionNotFoundError: {exc}\n")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        if args.use_json:
            sys.stderr.write(
                json.dumps(
                    {
                        "success": False,
                        "session_id": args.session_id,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                    indent=2,
                )
                + "\n"
            )
        else:
            sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        sys.exit(1)

    payload = {"session_id": args.session_id, "status": args.status}
    if args.use_json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(f"session_id: {args.session_id}\nstatus: {args.status}\n")
    sys.exit(0)


def session_list(argv: Optional[list[str]] = None) -> None:
    """``web-session-list`` entry point — F018.

    Reads the registry only — no Chromium attach. Returns a JSON
    array of session dicts (or a human-readable table when ``--json``
    isn't set).
    """
    _session_list_impl(argv)


@log_call("BOUNDARY")
def _session_list_impl(argv: Optional[list[str]]) -> None:
    parser = _build_session_list_parser()
    args = parser.parse_args(argv)
    try:
        mgr = SessionManager()
        if args.status == "all":
            sessions = (
                mgr.list(status="active")
                + mgr.list(status="closed")
                + mgr.list(status="crashed")
            )
        else:
            sessions = mgr.list(status=args.status)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        if args.use_json:
            sys.stderr.write(
                json.dumps(
                    {
                        "success": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                    indent=2,
                )
                + "\n"
            )
        else:
            sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        sys.exit(1)

    payload = [
        {
            "session_id": s.session_id,
            "status": s.status,
            "started_at": s.started_at,
            "last_used_at": s.last_used_at,
            "pid": s.pid,
            "goal": s.goal,
            "cdp_endpoint": s.cdp_endpoint,
        }
        for s in sessions
    ]
    if args.use_json:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        if not payload:
            sys.stdout.write("(no sessions)\n")
        else:
            for s in payload:
                sys.stdout.write(
                    f"  [{s['status']}] {s['session_id']} pid={s['pid']} "
                    f"goal={s['goal'] or '-'} last_used={s['last_used_at']}\n"
                )
    sys.exit(0)


# ── web-await-auth (F008) ──────────────────────────────────────────────


def _build_await_auth_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for ``web-await-auth`` (F008).

    Defaults for ``--poll-interval-secs`` and ``--timeout-secs`` are
    sourced at parse time from the matching env vars
    (``WEB_AGENT_AUTH_WALL_POLL_SECS`` / ``WEB_AGENT_AUTH_WALL_TIMEOUT_SECS``)
    or fall back to the literals 2 / 300 from the plan. Reading at parse
    time (not import time) is what lets tests monkeypatch the env vars
    via ``monkeypatch.setenv`` and have the change take effect.
    """
    default_poll = int(os.environ.get("WEB_AGENT_AUTH_WALL_POLL_SECS", "2"))
    default_timeout = int(os.environ.get("WEB_AGENT_AUTH_WALL_TIMEOUT_SECS", "300"))
    p = argparse.ArgumentParser(
        prog="web-await-auth",
        description=(
            "Poll an existing session for auth-wall clearance. Attaches to "
            "the Chromium recorded against --session-id, registers the "
            "Network.responseReceived tracker once, then calls "
            "detect_auth_wall in a loop every --poll-interval-secs. Exits 0 "
            "when the wall clears (detector returns None), non-zero on "
            "--timeout-secs without clearance. Env-var overrides: "
            "WEB_AGENT_AUTH_WALL_POLL_SECS, WEB_AGENT_AUTH_WALL_TIMEOUT_SECS."
        ),
    )
    p.add_argument(
        "--session-id",
        required=True,
        help="Existing session id to poll for auth-wall clearance.",
    )
    p.add_argument(
        "--poll-interval-secs",
        type=int,
        default=default_poll,
        dest="poll_interval_secs",
        help=(
            "Seconds between auth-wall polls. Default 2 "
            "(override via WEB_AGENT_AUTH_WALL_POLL_SECS)."
        ),
    )
    p.add_argument(
        "--timeout-secs",
        type=int,
        default=default_timeout,
        dest="timeout_secs",
        help=(
            "Total seconds before giving up. Default 300 "
            "(override via WEB_AGENT_AUTH_WALL_TIMEOUT_SECS)."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Emit a JSON envelope on stdout (and on stderr-on-error).",
    )
    return p


def _session_is_headless(browser: Any) -> bool:
    """Best-effort: is this BrowserSession running headless?

    Reads ``browser.browser_profile.headless``. ``None`` means "not
    explicitly set" — :class:`BrowserProfile` falls back to "headless
    when no display is available" at launch time, so ``None`` is
    treated as ambiguous and the warning is NOT emitted (we'd rather
    miss a warning than nag the user). Any attribute error (e.g. unit-
    test stub with no profile) returns ``False`` — i.e. assume headed
    and don't nag.
    """
    try:
        return bool(browser.browser_profile.headless)
    except AttributeError:
        return False


async def _await_auth_core(
    *,
    browser: Any,
    session_id: str,
    poll_interval_secs: float,
    timeout_secs: float,
) -> dict:
    """Poll for auth-wall clearance; return the result envelope.

    Side effects:

    * On entry: emits a stderr warning when ``browser`` is headless.
    * Calls :func:`register_response_tracker` once (idempotent guard).
    * For every poll where the detector returns a non-None signal,
      writes one ``still waiting on <signal>...`` line to stderr.

    Returns a dict the CLI wrapper renders as either JSON or human
    text. Success shape:

        {"session_id", "cleared": True, "signal_when_cleared": None,
         "elapsed_secs": float}

    Timeout shape:

        {"session_id", "cleared": False, "timeout_signal": "<name>",
         "elapsed_secs": float}

    ``time.monotonic`` is used for elapsed-time measurement so a wall-
    clock skew during the poll does not corrupt the timeout decision.
    """
    import time as _time

    from src.web_agent.browser.watchdogs.auth_wall_watchdog import (
        detect_auth_wall,
        register_response_tracker,
    )

    # Pre-flight: warn (but don't auto-promote) when headless.
    if _session_is_headless(browser):
        sys.stderr.write(
            "WARNING: session is headless — you need a headed window to "
            "complete login. Consider re-running web-navigate with --headed.\n"
        )

    # Idempotent: only registers once per session.
    register_response_tracker(browser)

    start = _time.monotonic()
    last_signal: Optional[str] = None

    while True:
        signal = await detect_auth_wall(browser)
        elapsed = _time.monotonic() - start

        if signal is None:
            return {
                "session_id": session_id,
                "cleared": True,
                "signal_when_cleared": None,
                "elapsed_secs": elapsed,
            }

        # Still walled — record progress, decide whether to keep going.
        last_signal = signal
        sys.stderr.write(f"still waiting on {signal}...\n")

        if elapsed >= timeout_secs:
            return {
                "session_id": session_id,
                "cleared": False,
                "timeout_signal": last_signal,
                "elapsed_secs": elapsed,
            }

        # Sleep, then poll again. asyncio.sleep (not time.sleep) so the
        # event loop stays cooperative.
        await asyncio.sleep(poll_interval_secs)


def await_auth(argv: Optional[list[str]] = None) -> None:
    """``web-await-auth`` entry point — F008.

    Wrapped at the boundary by ``_run_cli_with_hard_exit`` so the
    process terminates via ``os._exit`` instead of falling through to
    asyncio's slow shutdown path. See ``_hard_exit`` for the rationale.
    """
    _run_cli_with_hard_exit(_await_auth_impl, argv)


@log_call("BOUNDARY")
def _await_auth_impl(argv: Optional[list[str]]) -> None:
    parser = _build_await_auth_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(_await_auth_async(args))
    except SystemExit:
        # Tests + behave run with COWORK_WEB_AGENT_NO_HARD_EXIT=1 — let
        # SystemExit propagate so callers can catch it.
        raise
    except (SessionNotFoundError, SessionCrashedError) as exc:
        _emit_error(exc, use_json=args.use_json)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        _emit_error(exc, use_json=args.use_json)


async def _await_auth_async(args: argparse.Namespace) -> None:
    """Attach to the existing session, run the poll loop, emit + exit.

    Session lookup happens here (not inside ``_await_auth_core``) so
    that unit tests can drive ``_await_auth_core`` directly with a
    SimpleNamespace stand-in and skip the SessionManager round-trip.
    """
    mgr = SessionManager()
    mgr.reap_stale()
    sess_meta = mgr.attach(args.session_id)
    browser = _attach_browser_session(sess_meta.cdp_endpoint)
    try:
        await asyncio.wait_for(
            _start_session(browser), timeout=max(args.timeout_secs, 30)
        )
        result = await _await_auth_core(
            browser=browser,
            session_id=sess_meta.session_id,
            poll_interval_secs=float(args.poll_interval_secs),
            timeout_secs=float(args.timeout_secs),
        )
    finally:
        await _detach_quietly(browser)

    # Re-attach to bump last_used_at on the session row.
    try:
        mgr.attach(args.session_id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            f"web-await-auth: post-poll attach() to bump last_used_at "
            f"raised {type(exc).__name__}: {exc}"
        )

    if result["cleared"]:
        if args.use_json:
            sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
        else:
            sys.stdout.write(f"auth cleared after {result['elapsed_secs']:.1f}s\n")
        _signal_done_and_exit(0)
    else:
        # Timeout path.
        if args.use_json:
            sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
        sys.stderr.write(
            f"auth-wall not cleared within {args.timeout_secs}s "
            f"(still {result['timeout_signal']})\n"
        )
        _signal_done_and_exit(1)


# ── web-diagnose (F010) ────────────────────────────────────────────────


# DOM audit JS — single Runtime.evaluate returning the four documented
# counters. Same shape carried over during the web-agent merge but
# evaluated via raw CDP rather than Playwright. The expression must be
# wrapped in a single value-returning function so ``Runtime.evaluate`` with
# ``returnByValue=True`` ships back a serialisable dict.
_JS_DOM_AUDIT = r"""
(function() {
  return {
    iframes: document.querySelectorAll('iframe').length,
    open_shadow_hosts: Array.from(
      document.querySelectorAll('*')
    ).filter(function(el) {
      return el.shadowRoot && el.shadowRoot.mode === 'open';
    }).length,
    portal_roots: document.querySelectorAll(
      '#portal-root, #modal-root, [data-portal], [data-reach-portal]'
    ).length,
    react_root_found: !!(
      document.querySelector('#root, #__next, [data-reactroot]')
      || (typeof window !== 'undefined' && window.React)
    ),
  };
})()
""".strip()


# Marker attribute on the session for the diagnose network logger.
# Deliberately a different name from auth-wall's
# ``_auth_wall_response_tracker_registered`` so the two listeners are
# orthogonal: registering one does NOT skip the other.
_DIAGNOSE_NET_LOGGER_REGISTERED = "_diagnose_network_logger_registered"


def _build_diagnose_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for ``web-diagnose`` (F010).

    Captures the CDP-only artifact set: ``network.log``, ``dom_audit.json``,
    initial PNG + page-content HTML, optional inner HTML for a container,
    and per-tab artifacts when a trigger selector is given. Does NOT emit
    a ``trace.zip`` — Playwright-specific, dropped in the merge.
    """
    p = argparse.ArgumentParser(
        prog="web-diagnose",
        description=(
            "Diagnostic capture against a URL: writes network.log, "
            "dom_audit.json, an initial screenshot + page HTML, and "
            "(optionally) per-tab artifacts driven by a trigger CSS "
            "selector. Inner-HTML of a named container is captured "
            "when --container-selector is provided. No trace.zip — "
            "this is the CDP-only diagnostic dump for the web-agent surface."
        ),
    )
    p.add_argument(
        "--url",
        required=True,
        help="URL to diagnose (http(s):// or file://).",
    )
    p.add_argument(
        "--container-selector",
        default=None,
        dest="container_selector",
        help=(
            "Optional CSS selector for a container element. When given, "
            "each capture stage also dumps the container's outerHTML as "
            "``<tag>_inner.html``."
        ),
    )
    p.add_argument(
        "--trigger-selector",
        default=None,
        dest="trigger_selector",
        help=(
            "Optional CSS selector for trigger elements (tabs, etc.). "
            "Each matching element is clicked once and per-tab artifacts "
            "(``tab_NN.png``, ``tab_NN_page_content.html``, optionally "
            "``tab_NN_inner.html``) are captured after a settle delay."
        ),
    )
    p.add_argument(
        "--out-dir",
        default=str(_default_diagnose_out_dir()),
        dest="out_dir",
        help=(
            "Directory to write artifacts into. Default: a UTC-stamped "
            "subdirectory under files/scraped/_diagnostics/."
        ),
    )
    p.add_argument(
        "--settle-ms",
        type=int,
        default=1500,
        dest="settle_ms",
        help=(
            "Milliseconds to wait after each click for the page to "
            "settle before capturing per-tab artifacts. Default: 1500."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Emit the summary as JSON on stdout.",
    )
    return p


def _default_diagnose_out_dir() -> Path:
    """Build the default ``files/scraped/_diagnostics/<UTC>/`` output path."""
    from datetime import datetime, timezone

    project_root = Path(__file__).resolve().parents[2]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return project_root / "files" / "scraped" / "_diagnostics" / ts


def _register_diagnose_network_logger(session: Any, log_path: Path) -> Any:
    """Register CDP listeners that append one line per request/response.

    Format (one line per event):

        >> METHOD URL    (from Network.requestWillBeSent)
        << STATUS URL    (from Network.responseReceived)

    The handlers are best-effort — a bad event header never propagates.

    Idempotent: a session that's already been registered returns ``None``
    instead of re-registering (the existing log handle, if any, is owned
    by the original caller).

    Args:
        session: A live ``BrowserSession`` (or duck-typed equivalent).
        log_path: Where to append the network log.

    Returns:
        The open file handle (caller is responsible for closing) on the
        first registration; ``None`` on subsequent calls against the same
        session.
    """
    if getattr(session, _DIAGNOSE_NET_LOGGER_REGISTERED, False):
        return None

    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = log_path.open("a", encoding="utf-8")

    def _on_request(event: Any, session_id: Any = None) -> None:
        try:
            req = (
                event.get("request", {})
                if hasattr(event, "get")
                else getattr(event, "request", {})
            )
            method = (
                req.get("method", "?")
                if isinstance(req, dict)
                else getattr(req, "method", "?")
            )
            url = (
                req.get("url", "?")
                if isinstance(req, dict)
                else getattr(req, "url", "?")
            )
            fh.write(f">> {method} {url}\n")
            fh.flush()
        except Exception as exc:  # noqa: BLE001 — defensive in event loop
            logger.debug(
                "diagnose-net: request handler raised %s: %s",
                type(exc).__name__,
                exc,
            )

    def _on_response(event: Any, session_id: Any = None) -> None:
        try:
            resp = (
                event.get("response", {})
                if hasattr(event, "get")
                else getattr(event, "response", {})
            )
            status = (
                resp.get("status", "?")
                if isinstance(resp, dict)
                else getattr(resp, "status", "?")
            )
            url = (
                resp.get("url", "?")
                if isinstance(resp, dict)
                else getattr(resp, "url", "?")
            )
            fh.write(f"<< {status} {url}\n")
            fh.flush()
        except Exception as exc:  # noqa: BLE001 — defensive in event loop
            logger.debug(
                "diagnose-net: response handler raised %s: %s",
                type(exc).__name__,
                exc,
            )

    try:
        cdp_client = session.cdp_client
        cdp_client.register.Network.requestWillBeSent(_on_request)
        cdp_client.register.Network.responseReceived(_on_response)
        setattr(session, _DIAGNOSE_NET_LOGGER_REGISTERED, True)
    except Exception as exc:  # noqa: BLE001 — best-effort registration
        logger.warning(
            "diagnose-net: listener registration failed (%s: %s)",
            type(exc).__name__,
            exc,
        )
    return fh


async def _diagnose_capture_dom_audit(session: Any, out_path: Path) -> dict:
    """Run the DOM audit JS and write the result to disk.

    Returns the audit dict; falls back to a four-key dict with an
    ``_error`` field if Runtime.evaluate fails.
    """
    cdp_session = await session.get_or_create_cdp_session()
    try:
        response = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={
                "expression": _JS_DOM_AUDIT,
                "returnByValue": True,
            },
            session_id=cdp_session.session_id,
        )
        value = response.get("result", {}).get("value", {})
        audit = {
            "iframes": int(value.get("iframes", 0)),
            "open_shadow_hosts": int(value.get("open_shadow_hosts", 0)),
            "portal_roots": int(value.get("portal_roots", 0)),
            "react_root_found": bool(value.get("react_root_found", False)),
        }
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "diagnose: dom_audit raised %s: %s — emitting zeroed dict",
            type(exc).__name__,
            exc,
        )
        audit = {
            "iframes": 0,
            "open_shadow_hosts": 0,
            "portal_roots": 0,
            "react_root_found": False,
            "_error": f"{type(exc).__name__}: {exc}",
        }
    out_path.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    return audit


async def _diagnose_capture_inner_html(
    session: Any, container_selector: str
) -> Optional[str]:
    """Return the outerHTML of the first ``container_selector`` match.

    Uses CDP ``DOM.querySelector`` + ``DOM.getOuterHTML``. Returns
    ``None`` when no element matches.
    """
    cdp_session = await session.get_or_create_cdp_session()
    doc = await cdp_session.cdp_client.send.DOM.getDocument(
        params={"depth": 1}, session_id=cdp_session.session_id
    )
    root_node_id = doc["root"]["nodeId"]
    result = await cdp_session.cdp_client.send.DOM.querySelector(
        params={"nodeId": root_node_id, "selector": container_selector},
        session_id=cdp_session.session_id,
    )
    node_id = result.get("nodeId", 0)
    if not node_id:
        return None
    outer = await cdp_session.cdp_client.send.DOM.getOuterHTML(
        params={"nodeId": node_id}, session_id=cdp_session.session_id
    )
    return outer.get("outerHTML", "")


async def _diagnose_capture_screenshot(session: Any, out_path: Path) -> None:
    """Capture a full-page PNG via CDP and write to ``out_path``."""
    png_bytes = await _capture_screenshot_bytes(session, full_page=True)
    out_path.write_bytes(png_bytes)


async def _diagnose_query_all_node_ids(session: Any, selector: str) -> list[int]:
    """Return all CDP nodeIds matching ``selector``.

    Uses ``DOM.querySelectorAll(rootNodeId, selector)``. Returns an empty
    list when nothing matches.
    """
    cdp_session = await session.get_or_create_cdp_session()
    doc = await cdp_session.cdp_client.send.DOM.getDocument(
        params={"depth": 1}, session_id=cdp_session.session_id
    )
    root_node_id = doc["root"]["nodeId"]
    result = await cdp_session.cdp_client.send.DOM.querySelectorAll(
        params={"nodeId": root_node_id, "selector": selector},
        session_id=cdp_session.session_id,
    )
    return list(result.get("nodeIds", []))


async def _diagnose_click_node(session: Any, node_id: int) -> None:
    """Best-effort click on a CDP nodeId via Runtime.evaluate.

    We reuse the JS-level ``el.click()`` path that ``web-click`` takes —
    rather than computing screen coordinates and dispatching mouse
    events — because diagnose is informational, not user-fidelity. Any
    handler exception is swallowed; per-tab capture continues regardless.
    """
    try:
        cdp_session = await session.get_or_create_cdp_session()
        # Resolve the node to a JS object so we can call .click() on it.
        resolved = await cdp_session.cdp_client.send.DOM.resolveNode(
            params={"nodeId": node_id},
            session_id=cdp_session.session_id,
        )
        object_id = resolved.get("object", {}).get("objectId")
        if not object_id:
            return
        await cdp_session.cdp_client.send.Runtime.callFunctionOn(
            params={
                "functionDeclaration": "function() { this.click(); }",
                "objectId": object_id,
            },
            session_id=cdp_session.session_id,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "diagnose: click on nodeId=%s raised %s: %s",
            node_id,
            type(exc).__name__,
            exc,
        )


async def _diagnose_core(
    *,
    browser: Any,
    url: str,
    container_selector: Optional[str],
    trigger_selector: Optional[str],
    out_dir: Path,
    settle_ms: int,
) -> dict:
    """Run the full diagnose flow and return a summary dict.

    Side effects: writes every artifact to ``out_dir`` (created if
    missing). Captures:

    * ``network.log`` — one line per request/response, populated by the
      registered CDP listeners as events arrive. The handle is closed at
      the end of this function.
    * ``dom_audit.json`` — single Runtime.evaluate counters.
    * ``01_initial.png`` — full-page screenshot.
    * ``01_initial_page_content.html`` — ``page_html()`` result.
    * ``01_initial_inner.html`` — only when ``container_selector`` is
      provided AND the container matches at least one element.
    * ``tab_NN.png`` / ``tab_NN_page_content.html`` /
      ``tab_NN_inner.html`` — only when ``trigger_selector`` is provided
      (last one further gated on ``container_selector``).

    Notably: NO ``trace.zip``. That was Playwright-specific.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    network_log = out_dir / "network.log"
    log_fh = _register_diagnose_network_logger(browser, network_log)

    tab_count = 0
    audit: dict = {}
    try:
        # Navigate to the URL. _start_session is already done outside.
        await _navigate(browser, url)

        # Touch the log so it exists even if no network events fire (e.g.
        # against ``about:blank``). Tests assert ``network.log`` is a file
        # regardless of whether the mock fires events.
        if log_fh is not None:
            log_fh.flush()
        elif not network_log.exists():
            network_log.touch()

        # ── Initial artifacts ────────────────────────────────────────
        await _diagnose_capture_screenshot(browser, out_dir / "01_initial.png")
        page_html = await browser.page_html()
        (out_dir / "01_initial_page_content.html").write_text(
            page_html or "", encoding="utf-8"
        )

        if container_selector:
            inner = await _diagnose_capture_inner_html(browser, container_selector)
            if inner is not None:
                (out_dir / "01_initial_inner.html").write_text(inner, encoding="utf-8")
            else:
                logger.info(
                    "diagnose: container_selector %r matched nothing — "
                    "skipping 01_initial_inner.html",
                    container_selector,
                )

        audit = await _diagnose_capture_dom_audit(browser, out_dir / "dom_audit.json")

        # ── Per-tab loop ─────────────────────────────────────────────
        if trigger_selector:
            node_ids = await _diagnose_query_all_node_ids(browser, trigger_selector)
            tab_count = len(node_ids)
            for i, node_id in enumerate(node_ids):
                tag = f"tab_{i:02d}"
                await _diagnose_click_node(browser, node_id)
                # asyncio.sleep is cooperative — the event loop yields
                # so any in-flight network response handlers continue
                # writing to network.log during this gap.
                await asyncio.sleep(settle_ms / 1000.0)

                try:
                    await _diagnose_capture_screenshot(browser, out_dir / f"{tag}.png")
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.warning(
                        "diagnose: %s screenshot failed: %s: %s",
                        tag,
                        type(exc).__name__,
                        exc,
                    )

                try:
                    tab_page_html = await browser.page_html()
                    (out_dir / f"{tag}_page_content.html").write_text(
                        tab_page_html or "", encoding="utf-8"
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.warning(
                        "diagnose: %s page_html failed: %s: %s",
                        tag,
                        type(exc).__name__,
                        exc,
                    )

                if container_selector:
                    try:
                        tab_inner = await _diagnose_capture_inner_html(
                            browser, container_selector
                        )
                        if tab_inner is not None:
                            (out_dir / f"{tag}_inner.html").write_text(
                                tab_inner, encoding="utf-8"
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "diagnose: %s inner.html failed: %s: %s",
                            tag,
                            type(exc).__name__,
                            exc,
                        )
    finally:
        if log_fh is not None:
            try:
                log_fh.flush()
                log_fh.close()
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "diagnose: network log close raised %s: %s",
                    type(exc).__name__,
                    exc,
                )

    return {
        "out_dir": str(out_dir),
        "tab_count": tab_count,
        "audit": audit,
    }


def diagnose(argv: Optional[list[str]] = None) -> None:
    """``web-diagnose`` entry point — F010.

    Wrapped at the boundary by ``_run_cli_with_hard_exit`` so the process
    terminates via ``os._exit`` instead of falling through to asyncio's
    slow shutdown path. See ``_hard_exit`` for the rationale.
    """
    _run_cli_with_hard_exit(_diagnose_impl, argv)


@log_call("BOUNDARY")
def _diagnose_impl(argv: Optional[list[str]]) -> None:
    parser = _build_diagnose_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(_diagnose_async(args))
    except SystemExit:
        raise
    except (SessionNotFoundError, SessionCrashedError) as exc:
        _emit_error(exc, use_json=args.use_json)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        _emit_error(exc, use_json=args.use_json)


async def _diagnose_async(args: argparse.Namespace) -> None:
    """Spin up a fresh session, run the diagnose flow, emit summary, exit.

    Unlike the other primitives, ``web-diagnose`` always creates its own
    session — this is a one-shot tool, not part of a multi-step recipe.
    The session is closed at the end so Chromium doesn't linger.
    """
    mgr = SessionManager()
    mgr.reap_stale()
    sess_meta = mgr.start()
    browser = _attach_browser_session(sess_meta.cdp_endpoint)
    try:
        await asyncio.wait_for(_start_session(browser), timeout=30.0)
        summary = await _diagnose_core(
            browser=browser,
            url=args.url,
            container_selector=args.container_selector,
            trigger_selector=args.trigger_selector,
            out_dir=Path(args.out_dir),
            settle_ms=args.settle_ms,
        )
    finally:
        await _detach_quietly(browser)
        # Close the session so Chromium doesn't survive the CLI. Failures
        # are non-fatal — the user will reap on next start-up anyway.
        try:
            mgr.close(sess_meta.session_id, status="closed")
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "diagnose: session close raised %s: %s",
                type(exc).__name__,
                exc,
            )

    if args.use_json:
        sys.stdout.write(json.dumps(summary, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(
            f"out_dir: {summary['out_dir']}\n"
            f"tab_count: {summary['tab_count']}\n"
            f"audit: {json.dumps(summary['audit'], indent=2, default=str)}\n"
        )
    _signal_done_and_exit(0)


def recipe_list(argv: Optional[list[str]] = None) -> None:
    """``web-recipe-list`` entry point — F011/F012 of web-agent-postgres.

    Thin argparse + output-formatting wrapper around
    ``src.web_agent.recipes.list_recipes``. No SQL lives here — the
    business function owns every read per
    ``.claude/rules/layer-responsibilities.md``.
    """
    parser = argparse.ArgumentParser(
        prog="web-recipe-list",
        description=(
            "List learned recipes from web_agent.wa_recipe, sorted by "
            "last_used DESC. Filters all optional; combine with AND."
        ),
    )
    parser.add_argument("--domain", default=None, help="Exact-match domain filter.")
    parser.add_argument("--intent", default=None, help="Exact-match intent filter.")
    confirmed_group = parser.add_mutually_exclusive_group()
    confirmed_group.add_argument(
        "--confirmed",
        dest="confirmed",
        action="store_const",
        const=True,
        help="Only confirmed recipes.",
    )
    confirmed_group.add_argument(
        "--unconfirmed",
        dest="confirmed",
        action="store_const",
        const=False,
        help="Only unconfirmed recipes.",
    )
    parser.add_argument(
        "--top", type=int, default=None, help="Limit number of results."
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true", help="Emit JSON output."
    )
    args = parser.parse_args(argv)

    from src.utils.db_util import get_connection
    from src.web_agent.recipes import list_recipes

    conn = get_connection("web_agent")
    try:
        rows = list_recipes(
            conn,
            domain=args.domain,
            intent=args.intent,
            confirmed=args.confirmed,
            top=args.top,
        )
    finally:
        conn.close()

    if args.as_json:
        sys.stdout.write(json.dumps(rows, indent=2, default=str) + "\n")
        return

    if not rows:
        sys.stdout.write("(no recipes)\n")
        return

    headers = ("domain", "intent", "selector", "confirmed", "last_used")
    formatted: list[tuple[str, ...]] = [headers]
    for row in rows:
        formatted.append(
            (
                str(row.get("domain", "")),
                str(row.get("intent", "")),
                str(row.get("selector", "")),
                str(row.get("confirmed", "")),
                str(row.get("last_used", "")),
            )
        )
    widths = [max(len(r[i]) for r in formatted) for i in range(len(headers))]
    for line in formatted:
        sys.stdout.write(
            " | ".join(c.ljust(widths[i]) for i, c in enumerate(line)) + "\n"
        )


# ── Module entry for ``python -m tools.web_agent.run`` ────────────────


def main(argv: Optional[list[str]] = None) -> None:
    """Dispatch to the primitive named by the first positional arg.

    Mirrors the dispatcher pattern used by other tools/<x>/run.py modules
    so ``python -m tools.web_agent`` works as a discovery surface.
    """
    parser = argparse.ArgumentParser(prog="tools.web_agent")
    parser.add_argument(
        "subcommand",
        choices=(
            "navigate",
            "click",
            "type",
            "extract",
            "screenshot",
            "session-close",
            "session-list",
            "session-record-compile",
            "await-auth",
            "diagnose",
            "recipe-list",
        ),
        help="Primitive to run.",
    )
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    dispatch = {
        "navigate": navigate,
        "click": click,
        "type": type_,
        "extract": extract,
        "screenshot": screenshot,
        "session-close": session_close,
        "session-list": session_list,
        "session-record-compile": record_compile,
        "await-auth": await_auth,
        "diagnose": diagnose,
        "recipe-list": recipe_list,
    }
    dispatch[args.subcommand](args.rest)
