/* Local transport for the maintained extension observer; never replay requests. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const REPO = path.resolve(__dirname, '../../..');
const MAX_BYTES = 25 * 1024 * 1024;
const EVENTS = ['Network.requestWillBeSent', 'Network.responseReceived',
  'Network.loadingFinished', 'Network.loadingFailed', 'Fetch.requestPaused'];
const CODES = new Set(['invalid_pdf_url', 'pdf_redirect', 'aborted', 'pdf_timeout',
  'pdf_navigation_mismatch', 'pdf_handoff_mismatch', 'pdf_duplicate_request',
  'pdf_duplicate_response', 'pdf_post_redirect', 'challenge', 'rate-limited',
  'pdf_http', 'pdf_mime', 'pdf_html', 'pdf_headers_missing', 'pdf_handoff_too_large',
  'pdf_too_large', 'pdf_capture', 'pdf_html_challenge', 'pdf_viewer_shell', 'not_pdf',
  'pdf_network', 'pdf_debug_detached', 'pdf_navigation', 'private_path',
  'invalid_endpoint', 'browser_unavailable', 'browser_context_unverified',
  'browser_target_unresolved', 'capture_output_invalid']);

function privatePath(raw, existing = true) {
  if (typeof raw !== 'string' || !path.isAbsolute(raw)) throw Error('private_path');
  const value = path.resolve(raw);
  if (value === REPO || value.startsWith(REPO + path.sep) || REPO.startsWith(value + path.sep)) throw Error('private_path');
  const checked = existing ? value : path.dirname(value);
  if (fs.realpathSync(checked) !== checked) throw Error('private_path');
  const stat = fs.lstatSync(checked);
  if (stat.isSymbolicLink() || stat.mode & 0o077 || (existing ? !stat.isFile() : !stat.isDirectory()) ||
      !existing && fs.existsSync(value)) throw Error('private_path');
  return value;
}

function validateEndpoint(raw) {
  let value;
  try { value = new URL(raw); } catch { throw Error('invalid_endpoint'); }
  if (value.protocol !== 'ws:' || value.hostname !== '127.0.0.1' || !value.port ||
      value.username || value.password || value.search || value.hash ||
      !/^\/devtools\/browser\/[A-Za-z0-9-]+$/.test(value.pathname)) throw Error('invalid_endpoint');
  return value.href;
}

function ordinaryKadURL(raw) {
  try { const value = new URL(raw); return value.origin === 'https://kad.arbitr.ru' &&
    !value.username && !value.password && !value.hash; } catch { return false; }
}
function expectedTargetURL(actual, expected) {
  if (actual === 'about:blank') return true;
  if (!ordinaryKadURL(actual) || !ordinaryKadURL(expected)) return false;
  if (actual === expected) return true;
  const from = new URL(expected), to = new URL(actual);
  return from.pathname.startsWith('/Kad/PdfDocument/') && !from.search &&
    to.pathname === from.pathname.replace('/Kad/PdfDocument/', '/Document/Pdf/') &&
    ['', '?isAddStamp=True'].includes(to.search);
}
function listenerSet() {
  const callbacks = new Set();
  return {addListener(fn) { callbacks.add(fn); }, removeListener(fn) { callbacks.delete(fn); },
    fire(...args) { for (const fn of [...callbacks]) fn(...args); }};
}

function retryAfterSeconds(value, now = Date.now()) {
  if (typeof value !== 'string') return 60;
  const text = value.trim();
  const seconds = /^\d+$/.test(text) ? Number(text) : (Date.parse(text) - now) / 1000;
  return Number.isFinite(seconds) && seconds >= 0 ? Math.max(60, Math.ceil(seconds)) : 60;
}

async function captureWithSession(session, url, {signal} = {}) {
  const onEvent = listenerSet(), onDetach = listenerSet(), tabId = 1;
  const handlers = EVENTS.map(method => {
    const fn = params => onEvent.fire({tabId}, method, params);
    session.on(method, fn); return [method, fn];
  });
  let finalURL = url, mainFrameId = null, cooldownSeconds = 60;
  const track = params => {
    if (params.type !== 'Document') return;
    if (mainFrameId === null) mainFrameId = params.frameId;
    if (params.frameId === mainFrameId && params.request?.url) finalURL = params.request.url;
  };
  session.on('Network.requestWillBeSent', track);
  const rate = params => {
    const response = params.response;
    if (params.type !== 'Document' || params.frameId !== mainFrameId ||
        response?.status !== 429 || !ordinaryKadURL(response.url) || !expectedTargetURL(response.url, url)) return;
    const headers = response.headers;
    if (!headers || typeof headers !== 'object') return;
    const key = Object.keys(headers).find(name => name.toLowerCase() === 'retry-after');
    if (key) cooldownSeconds = retryAfterSeconds(headers[key]);
  };
  session.on('Network.responseReceived', rate);
  const disconnected = () => onDetach.fire({tabId});
  session.on('Disconnected', disconnected);
  const chrome = {debugger: {onEvent, onDetach, async attach() {}, async detach() {},
    sendCommand(_target, method, params) { return session.send(method, params); }},
    tabs: {async update(_tab, options) {
      const result = await session.send('Page.navigate', {url: options.url});
      if (result.errorText && result.errorText !== 'net::ERR_ABORTED') throw Error('pdf_navigation');
    }}};
  const context = vm.createContext({chrome, URL, Uint8Array, atob, setTimeout, clearTimeout});
  vm.runInContext(fs.readFileSync(path.join(REPO, 'browser-extension/kad-helper/native-pdf.js'), 'utf8'), context,
    {filename: 'native-pdf.js'});
  try {
    const bytes = await context.SroKadNativePdf.capture({tabId, url, signal});
    return {bytes: Buffer.from(bytes), finalURL};
  } catch (error) {
    if (error?.message === 'rate-limited') {
      const fixed = Error('rate-limited'); fixed.cooldownSeconds = cooldownSeconds; throw fixed;
    }
    throw error;
  } finally {
    for (const [method, fn] of handlers) session.off(method, fn);
    session.off('Network.requestWillBeSent', track);
    session.off('Network.responseReceived', rate);
    session.off('Disconnected', disconnected);
    await session.send('Fetch.disable', {}).catch(() => {});
  }
}

async function prepareJournal(root, journalPath, endpoint, expectedURL) {
  if (!ordinaryKadURL(expectedURL)) throw Error('invalid_pdf_url');
  if (fs.existsSync(journalPath)) {
    privatePath(journalPath);
    if (fs.statSync(journalPath).size > 16384) throw Error('browser_target_unresolved');
    let prior;
    try { prior = JSON.parse(fs.readFileSync(journalPath, 'utf8')); } catch { throw Error('browser_target_unresolved'); }
    if (!prior || prior.endpoint !== endpoint || !/^[A-Za-z0-9-]+$/.test(prior.target_id || '') ||
        !ordinaryKadURL(prior.expected_url)) throw Error('browser_target_unresolved');
    let info;
    try { info = await root.send('Target.getTargetInfo', {targetId: prior.target_id}); }
    catch (error) { if (!/No target with given id found/i.test(error?.message || '')) throw Error('browser_target_unresolved'); }
    if (info) {
      if (info.targetInfo?.targetId !== prior.target_id || !expectedTargetURL(info.targetInfo.url, prior.expected_url)) throw Error('browser_target_unresolved');
      if ((await root.send('Target.closeTarget', {targetId: prior.target_id})).success !== true) throw Error('browser_target_unresolved');
    }
    fs.unlinkSync(journalPath);
  }
  privatePath(journalPath, false);
  const record = {endpoint, target_id: null, expected_url: expectedURL};
  fs.writeFileSync(journalPath, JSON.stringify(record), {flag: 'wx', mode: 0o600});
  function update() {
    const fd = fs.openSync(journalPath, fs.constants.O_WRONLY | fs.constants.O_TRUNC | fs.constants.O_NOFOLLOW);
    try { fs.writeFileSync(fd, JSON.stringify(record)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  }
  return {setTarget(id) { record.target_id = id; update(); },
    recordExpectedURL(url) {
      if (!ordinaryKadURL(url)) throw Error('invalid_pdf_url');
      record.expected_url = url; update();
    }, expectedURL() { return record.expected_url; }, clear() { fs.unlinkSync(journalPath); }};
}

async function withOwnedBackgroundPage(chromium, endpoint, fn, {journalPath, expectedURL, timeoutMs = 60000} = {}) {
  endpoint = validateEndpoint(endpoint);
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 240000) throw Error('browser_unavailable');
  const browser = await chromium.connectOverCDP(endpoint, {timeout: 15000});
  let root, session, targetId, journal, unresolved = false;
  const abort = new AbortController(), deadline = setTimeout(() => abort.abort(), timeoutMs);
  try {
    const context = browser.contexts()[0];
    if (!context || !context.pages().some(page => ordinaryKadURL(page.url()))) throw Error('browser_context_unverified');
    root = await browser.newBrowserCDPSession();
    journal = await prepareJournal(root, journalPath, endpoint, expectedURL);
    const pageReady = context.waitForEvent('page', {timeout: 10000}); pageReady.catch(() => {});
    try { ({targetId} = await root.send('Target.createTarget', {url: 'about:blank', background: true})); }
    catch { throw Error('browser_target_unresolved'); }
    journal.setTarget(targetId);
    const page = await pageReady;
    session = await context.newCDPSession(page);
    if ((await session.send('Target.getTargetInfo', {})).targetInfo?.targetId !== targetId) throw Error('browser_unavailable');
    return await fn({page, session, signal: abort.signal,
      recordExpectedURL: value => journal.recordExpectedURL(value)});
  } finally {
    clearTimeout(deadline);
    if (session) await session.detach().catch(() => {});
    if (targetId && root) {
      const current = await root.send('Target.getTargetInfo', {targetId}).catch(() => null);
      if (journal && current?.targetInfo?.targetId === targetId &&
          expectedTargetURL(current.targetInfo.url, journal.expectedURL())) {
        const closed = await root.send('Target.closeTarget', {targetId}).catch(() => null);
        if (closed?.success === true) journal.clear(); else unresolved = true;
      } else unresolved = true;
    }
    if (root) await root.detach().catch(() => {});
    // Never call browser.close(): only our recorded target may be closed here.
    if (unresolved) throw Error('browser_target_unresolved');
  }
}

async function captureInBrowser(chromium, endpoint, url, journalPath) {
  return withOwnedBackgroundPage(chromium, endpoint,
    ({session, signal}) => captureWithSession(session, url, {signal}), {journalPath, expectedURL: url});
}

async function main() {
  try {
    const jobPath = privatePath(process.argv[2]);
    if (fs.statSync(jobPath).size > 16384) throw Error('private_path');
    const job = JSON.parse(fs.readFileSync(jobPath, 'utf8'));
    const output = privatePath(job.output_path, false);
    if (path.dirname(output) !== path.dirname(jobPath)) throw Error('private_path');
    const {chromium} = require(process.argv[3]);
    const result = await captureInBrowser(chromium, job.endpoint, job.url, job.journal_path);
    if (result.bytes.length > MAX_BYTES || !result.bytes.subarray(0, 5).equals(Buffer.from('%PDF-'))) throw Error('capture_output_invalid');
    fs.writeFileSync(output, result.bytes, {flag: 'wx', mode: 0o600});
    fs.writeFileSync(output + '.json', JSON.stringify({final_url: result.finalURL, size: result.bytes.length,
      sha256: crypto.createHash('sha256').update(result.bytes).digest('hex')}), {flag: 'wx', mode: 0o600});
    process.stdout.write(JSON.stringify({ok: true, bytes: result.bytes.length}) + '\n');
    process.exitCode = 0;
  } catch (error) {
    const status = {ok: false, code: CODES.has(error?.message) ? error.message : 'browser_unavailable'};
    if (status.code === 'rate-limited') status.cooldown_seconds = error.cooldownSeconds;
    process.stdout.write(JSON.stringify(status) + '\n');
    process.exitCode = 1;
  }
}
module.exports = {captureWithSession, captureInBrowser, withOwnedBackgroundPage, validateEndpoint, privatePath, retryAfterSeconds};
if (require.main === module) main().finally(() => process.exit(process.exitCode || 0));
