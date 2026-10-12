/* Small, conservative KAD DOM helpers. No network APIs or automatic document selection. */
(function (root, factory) {
  const api = factory();
  root.SroKadAutomation = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(globalThis, function () {
  "use strict";

  const CASE_TOKEN = /(^|[^A-Za-zА-Яа-яЁё0-9])([АA]\d+-\d+\/\d{2,4}(?:-\d+)?)(?![A-Za-zА-Яа-яЁё0-9/-])/g;
  const CASE_INPUT = 'input[placeholder="например, А50-5568/08"]';
  const CARD_PATH = /^\/Card\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i;
  const rowIds = new WeakMap();
  let nextRowId = 1;

  class KadAutomationError extends Error {
    constructor(code, message) {
      super(message);
      this.name = "KadAutomationError";
      this.code = code;
    }
  }
  const fail = (code, message) => { throw new KadAutomationError(code, message); };
  function rawAuthorityHasUserInfo(value) {
    const match = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]*)/i.exec(value);
    return Boolean(match && match[1].includes("@"));
  }

  function normalizeCase(value) {
    if (typeof value !== "string") return "";
    const trimmed = value.trim();
    return /^[AaАа]/.test(trimmed) ? `А${trimmed.slice(1)}` : trimmed;
  }

  function validateCardUrl(value) {
    if (typeof value !== "string" || /[\s\u0000-\u001f\u007f]/.test(value) || rawAuthorityHasUserInfo(value)) return null;
    try {
      const url = new URL(value);
      const match = CARD_PATH.exec(url.pathname);
      if (url.protocol !== "https:" || url.hostname.toLowerCase() !== "kad.arbitr.ru" ||
          (url.port && url.port !== "443") || url.username || url.password || url.search ||
          url.hash || !match) return null;
      return { href: url.href, cardId: match[1].toLowerCase() };
    } catch (_) { return null; }
  }

  function caseTokens(text) {
    const tokens = [];
    const regex = new RegExp(CASE_TOKEN.source, CASE_TOKEN.flags);
    for (const match of String(text || "").matchAll(regex)) tokens.push(normalizeCase(match[2]));
    return tokens;
  }

  function visible(element) {
    if (!element || element.hidden || element.getAttribute?.("aria-hidden") === "true") return false;
    const view = element.ownerDocument?.defaultView;
    const style = view?.getComputedStyle?.(element);
    if (style && (style.display === "none" || style.visibility === "hidden" || style.opacity === "0")) return false;
    const rect = element.getBoundingClientRect?.();
    return !rect || (rect.width > 0 && rect.height > 0);
  }
  function visibleOutsideHelper(element, doc) {
    if (!element) return false;
    for (let node = element; node; node = node.parentElement) {
      if (node.id === "sro-kad-helper" || node.getAttribute?.("id") === "sro-kad-helper" || !visible(node)) return false;
      if (node === doc.body) break;
    }
    return true;
  }

  function rowsIn(doc) {
    return Array.from(doc.querySelectorAll("tr, [role=row]")).filter(row => visibleOutsideHelper(row, doc));
  }
  function idFor(row) {
    if (!rowIds.has(row)) rowIds.set(row, nextRowId++);
    return rowIds.get(row);
  }

  function resultCount(text) {
    const match = String(text || "").match(/Найдено\s+(\d+)\s+дел\w*/i);
    return match ? Number(match[1]) : null;
  }
  function snapshot(doc) {
    const body = siteText(doc);
    const rows = rowsIn(doc);
    return {
      rowIds: rows.map(idFor),
      rowCaseTokens: rows.map(row => caseTokens(row.innerText)),
      resultCount: resultCount(body),
      promptVisible: /Укажите атрибуты дела для поиска/i.test(body),
    };
  }
  function matchingIds(state, caseNumber) {
    const q = normalizeCase(caseNumber);
    const ids = new Set();
    (state.rowCaseTokens || []).forEach((tokens, i) => {
      if (tokens.includes(q) && state.rowIds?.[i] !== undefined) ids.add(state.rowIds[i]);
    });
    return ids;
  }
  function exactFreshIds(before, after, caseNumber) {
    const old = matchingIds(before, caseNumber);
    return [...matchingIds(after, caseNumber)].filter(id => !old.has(id));
  }
  function resultStateChanged(before, after) {
    return before.resultCount !== after.resultCount || before.promptVisible !== after.promptVisible ||
      JSON.stringify(before.rowIds) !== JSON.stringify(after.rowIds) ||
      JSON.stringify(before.rowCaseTokens) !== JSON.stringify(after.rowCaseTokens);
  }

  function siteText(doc) {
    if (!doc.body || typeof doc.createTreeWalker !== "function") return "";
    const walker = doc.createTreeWalker(doc.body, doc.defaultView?.NodeFilter?.SHOW_TEXT || 4);
    const chunks = [];
    let node;
    while ((node = walker.nextNode())) {
      if (!visibleOutsideHelper(node.parentElement, doc)) continue;
      const text = String(node.nodeValue || "").trim();
      if (text) chunks.push(text);
    }
    return chunks.join(" ");
  }
  function inspectPage(doc, scriptErrors = []) {
    const body = siteText(doc).toLowerCase();
    const url = (() => { try { return new URL(doc.location?.href || doc.URL || ""); } catch (_) { return null; } })();
    if (!url || url.protocol !== "https:" || url.hostname.toLowerCase() !== "kad.arbitr.ru" || (url.port && url.port !== "443") || url.username || url.password)
      return { state: "unsafe-navigation" };
    if (["ошибка 429", "too many requests", "лимит запросов"].some(x => body.includes(x)))
      return { state: "rate-limited" };
    if (["captcha", "капча", "проверка, что вы не робот", "доступ ограничен", "access denied"].some(x => body.includes(x)))
      return { state: "challenge" };
    if (scriptErrors.includes("wasm")) return { state: "wasm-error" };
    if (scriptErrors.length) return { state: "script-error" };
    return { state: "ok" };
  }

  function installScriptErrorMonitor(win) {
    const kinds = new Set();
    const onError = event => {
      const text = `${event?.filename || ""} ${event?.message || ""}`.toLowerCase();
      kinds.add(/wasm|webassembly|__wbindgen_start/.test(text) ? "wasm" : "javascript");
    };
    const onRejection = () => kinds.add("javascript");
    win.addEventListener("error", onError);
    win.addEventListener("unhandledrejection", onRejection);
    return {
      getErrors: () => [...kinds],
      dispose: () => {
        win.removeEventListener("error", onError);
        win.removeEventListener("unhandledrejection", onRejection);
      },
    };
  }

  function collectExactCaseLinks(doc, caseNumber) {
    const q = normalizeCase(caseNumber);
    const exactRows = rowsIn(doc).filter(row => caseTokens(row.innerText).includes(q));
    const links = [];
    for (const row of exactRows) {
      for (const anchor of Array.from(row.querySelectorAll("a")).filter(anchor => visibleOutsideHelper(anchor, doc))) {
        const label = String(anchor.innerText || anchor.textContent || "").replace(/\s+/g, " ").trim();
        const card = validateCardUrl(anchor.href || anchor.getAttribute("href"));
        if (normalizeCase(label) === q && card)
          links.push({ rowId: idFor(row), href: card.href, cardId: card.cardId, label: q });
      }
    }
    return { exactRowIds: exactRows.map(idFor), links };
  }

  function abortIfNeeded(signal) {
    if (signal?.aborted) fail("aborted", "KAD operation was cancelled");
  }
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

  async function waitForPageReady(doc, monitor, {signal, deadline, now, sleep, pollIntervalMs, settleMs}) {
    let stableSince = now(), signature = null, settled = false;
    while (now() <= deadline) {
      abortIfNeeded(signal);
      const page = inspectPage(doc, monitor.getErrors());
      if (page.state !== "ok") fail(page.state, `KAD page state: ${page.state}`);
      const scripts = Array.from(doc.scripts || []).map(el => el.src).filter(Boolean)
        .filter(src => new URL(src, doc.location.href).origin === new URL(doc.location.href).origin).sort();
      const loaded = new Set(doc.defaultView.performance?.getEntriesByType("resource").map(entry => entry.name) || []);
      const next = JSON.stringify(scripts);
      if (next !== signature || doc.readyState !== "complete" || !scripts.every(src => loaded.has(src))) {
        signature = next;
        stableSince = now();
      } else if (now() - stableSince >= settleMs) { settled = true; break; }
      await sleep(pollIntervalMs);
    }
    if (!settled) fail("page-ready", "KAD page scripts did not settle before timeout");
  }

  async function runSearch(doc, caseNumber, options = {}) {
    const q = normalizeCase(caseNumber);
    if (!/^А\d+-\d+\/\d{4}$/.test(q)) fail("invalid-case", "Case number format is not supported");
    const { signal, timeoutMs = 20000, pollIntervalMs = 250, sleep = pause, now = () => Date.now(), onProgress = () => {}, settleMs = 2000 } = options;
    const monitor = installScriptErrorMonitor(doc.defaultView);
    const deadline = now() + timeoutMs;
    try {
      abortIfNeeded(signal);
      onProgress("ready");
      await waitForPageReady(doc, monitor, {signal, deadline, now, sleep, pollIntervalMs, settleMs});
      let input, add, setter;
      onProgress("input");
      while (now() <= deadline) {
        abortIfNeeded(signal);
        const page = inspectPage(doc, monitor.getErrors());
        if (page.state !== "ok") fail(page.state, `KAD page state: ${page.state}`);
        input = doc.querySelector(CASE_INPUT);
        add = doc.querySelector("#sug-cases .tag > i.b-icon.add");
        setter = Object.getOwnPropertyDescriptor(doc.defaultView.HTMLInputElement.prototype, "value")?.set;
        if (visibleOutsideHelper(input, doc) && visibleOutsideHelper(add, doc) && typeof add.click === "function" && setter) break;
        input = add = setter = null;
        await sleep(pollIntervalMs);
      }
      if (!input || !add || !setter) fail("timeout", "KAD search controls did not become ready before timeout");
      abortIfNeeded(signal);
      input.focus?.();
      setter.call(input, q);
      input.dispatchEvent(new doc.defaultView.Event("input", { bubbles: true }));
      input.dispatchEvent(new doc.defaultView.Event("change", { bubbles: true }));
      abortIfNeeded(signal);
      if (normalizeCase(input.value) !== q) fail("search-filter", "Case input did not retain the requested number");
      onProgress("filter");
      // Let input/change handlers finish before committing the single case criterion.
      await sleep(pollIntervalMs);
      abortIfNeeded(signal);
      if (normalizeCase(input.value) !== q) fail("search-filter", "Case input changed before filter commit");
      add.click();
      const filterConfirmed = () => {
        const filter = doc.querySelector("#sug-cases");
        return visibleOutsideHelper(filter, doc) &&
          Array.from(filter?.querySelectorAll?.(".tag.added") || []).some(tag =>
            Array.from(tag.querySelectorAll("input")).some(el => normalizeCase(el.value) === q));
      };
      while (!filterConfirmed() && now() <= deadline) {
        abortIfNeeded(signal);
        const page = inspectPage(doc, monitor.getErrors());
        if (page.state !== "ok") fail(page.state, `KAD page state: ${page.state}`);
        await sleep(pollIntervalMs);
      }
      if (!filterConfirmed()) fail("search-filter", "KAD did not commit the case criterion; search was not submitted");
      onProgress("button");

      let button = null;
      while (now() <= deadline) {
        abortIfNeeded(signal);
        const page = inspectPage(doc, monitor.getErrors());
        if (page.state !== "ok") fail(page.state, `KAD page state: ${page.state}`);
        const scoped = Array.from(doc.querySelectorAll('#b-form-submit button[type="submit"]'));
        const candidates = scoped.length ? scoped : Array.from(doc.querySelectorAll('button[type="submit"]'));
        const labelled = candidates.filter(el => !el.disabled && visibleOutsideHelper(el, doc) &&
          String(el.innerText || el.textContent || "").trim().toLowerCase() === "найти");
        if (labelled.length === 1 && typeof labelled[0].click === "function") button = labelled[0];
        if (button) break;
        await sleep(pollIntervalMs);
      }
      if (!button) fail("timeout", "A unique visible KAD search button did not become ready before timeout");
      if (!filterConfirmed()) fail("search-filter", "Case criterion changed before submission");
      const before = snapshot(doc);
      abortIfNeeded(signal);
      button.click();
      onProgress("submitted");

      let lastState = "unchanged";
      while (now() <= deadline) {
        abortIfNeeded(signal);
        const classification = inspectPage(doc, monitor.getErrors());
        if (classification.state !== "ok") fail(classification.state, `KAD page state: ${classification.state}`);
        const after = snapshot(doc);
        const changed = resultStateChanged(before, after);
        const exact = collectExactCaseLinks(doc, q);
        const fresh = new Set(exactFreshIds(before, after, q));
        const freshLinks = exact.links.filter(link => fresh.has(link.rowId));
        if (changed && !after.promptVisible && after.resultCount !== null) {
          if (after.resultCount === 0 && exact.exactRowIds.length === 0 && before.resultCount !== 0)
            return { state: "no-results", resultCount: 0, links: [] };
          if (after.resultCount > 0 && fresh.size > 0) {
            if (after.resultCount !== 1)
              return { state: "ambiguous", resultCount: after.resultCount, links: [] };
            if (exact.exactRowIds.length !== 1 || freshLinks.length !== 1 || exact.links.length !== 1)
              return { state: "ambiguous", resultCount: after.resultCount, links: [] };
            return { state: "matched", resultCount: after.resultCount, links: freshLinks };
          }
          lastState = "changed-incomplete";
        }
        await sleep(pollIntervalMs);
      }
      fail("timeout", lastState === "unchanged"
        ? "KAD result state did not change before timeout"
        : "KAD did not show a fresh exact result with a completed visible count before timeout");
    } finally { monitor.dispose(); }
  }

  function cardIdentity(doc, caseNumber) {
    const card = validateCardUrl(doc.location?.href || doc.URL || "");
    if (!card) return { ok: false, card: null };
    const q = normalizeCase(caseNumber);
    const titleMatches = normalizeCase(doc.title || "") === q;
    const headerMatches = Array.from(doc.querySelectorAll("h1, h2, h3, h4, [role=heading]"))
      .filter(node => visibleOutsideHelper(node, doc)).some(node => caseTokens(node.innerText).includes(q));
    const electronicCaseVisible = /Электронное дело/i.test(siteText(doc));
    return { ok: titleMatches && headerMatches && electronicCaseVisible, card, titleMatches, headerMatches, electronicCaseVisible };
  }

  function collectPdfCandidates(doc, caseNumber, cardUrl) {
    const identity = cardIdentity(doc, caseNumber);
    const supplied = validateCardUrl(cardUrl);
    if (!identity.ok || !supplied || identity.card.cardId !== supplied.cardId) return [];
    const candidates = [];
    const seen = new Set();
    const uuid = supplied.cardId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pdfPath = new RegExp(`^/Kad/PdfDocument/${uuid}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[^/]+\\.pdf$`, "i");
    for (const anchor of Array.from(doc.querySelectorAll("a")).filter(anchor => visibleOutsideHelper(anchor, doc))) {
      const rawHref = anchor.getAttribute("href") || anchor.href || "";
      const href = anchor.href || rawHref;
      let url;
      try { url = new URL(href, doc.location.href); } catch (_) { continue; }
      if (url.protocol !== "https:" || url.hostname.toLowerCase() !== "kad.arbitr.ru" || (url.port && url.port !== "443") || url.username || url.password || rawAuthorityHasUserInfo(rawHref) || url.search || url.hash || !pdfPath.test(url.pathname)) continue;
      if (seen.has(url.href)) continue;
      const label = String(anchor.innerText || anchor.textContent || anchor.getAttribute("aria-label") || "").trim();
      if (!label) fail("candidate-label-missing", "A visible PDF candidate has no readable label");
      if (label.length > 2000) fail("candidate-label-too-long", "A PDF candidate label exceeds the supported size");
      seen.add(url.href);
      candidates.push({ href: url.href, label, cardUrl: identity.card.href, cardId: identity.card.cardId });
      if (candidates.length > 100) fail("candidate-limit", "More than 100 visible PDF candidates were found");
    }
    return candidates;
  }

  async function runCard(doc, caseNumber, options = {}) {
    const q = normalizeCase(caseNumber);
    if (!/^А\d+-\d+\/\d{4}$/.test(q)) fail("invalid-case", "Case number format is not supported");
    const { signal, timeoutMs = 15000, pollIntervalMs = 250, sleep = pause, now = () => Date.now(), cardUrl = doc.location?.href || doc.URL, settleMs = 2000 } = options;
    const parsedCard = validateCardUrl(cardUrl);
    if (!parsedCard) fail("invalid-card-url", "Current page is not a public KAD card URL");
    const monitor = installScriptErrorMonitor(doc.defaultView);
    try {
      let clicked = false;
      const deadline = now() + timeoutMs;
      await waitForPageReady(doc, monitor, {signal, deadline, now, sleep, pollIntervalMs, settleMs});
      while (now() <= deadline) {
        abortIfNeeded(signal);
        const classification = inspectPage(doc, monitor.getErrors());
        if (classification.state !== "ok") fail(classification.state, `KAD page state: ${classification.state}`);
        const identity = cardIdentity(doc, q);
        if (identity.card?.cardId !== parsedCard.cardId) fail("card-identity", "KAD card URL changed during verification");
        if (identity.ok && !clicked) {
          const exactTextElements = Array.from(doc.querySelectorAll("*"))
            .filter(node => visibleOutsideHelper(node, doc) && String(node.innerText || node.textContent || "").trim().toLowerCase() === "электронное дело");
          const controls = exactTextElements.filter(node => !Array.from(node.querySelectorAll("*"))
            .some(child => exactTextElements.includes(child)));
          if (controls.length !== 1 || typeof controls[0].click !== "function")
            fail("card-section", "A unique visible electronic case control was not found");
          controls[0].click();
          clicked = true;
        }
        if (identity.ok && clicked) {
          const candidates = collectPdfCandidates(doc, q, parsedCard.href);
          if (candidates.length) return { state: "candidates", cardUrl: parsedCard.href, cardId: parsedCard.cardId, candidates, selectionRequired: true };
        }
        await sleep(pollIntervalMs);
      }
      fail("timeout", clicked
        ? "No visible public PDF candidates appeared before timeout"
        : "KAD card identity or electronic case control did not verify before timeout");
    } finally { monitor.dispose(); }
  }

  return { KadAutomationError, normalizeCase, validateCardUrl, inspectPage, installScriptErrorMonitor,
    snapshot, resultStateChanged, exactFreshIds, collectExactCaseLinks, collectPdfCandidates, runSearch, runCard };
});
