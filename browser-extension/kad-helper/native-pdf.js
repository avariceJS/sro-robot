/* Observe an ordinary top-level PDF navigation and pass its response bytes through unchanged. */
(() => {
  const api = globalThis.browser || globalThis.chrome;
  const MAX_BYTES = 25 * 1024 * 1024;
  const MAX_HANDOFF_BYTES = 128 * 1024;
  const TIMEOUT_MS = 60000;
  const PDF_MAGIC = [0x25, 0x50, 0x44, 0x46, 0x2d];
  const CHALLENGE_MARKERS = [
    'captcha', 'ddos.guard', 'ddos-guard', 'js.challenge', 'challenge-platform',
    '__js_p_', 'qrator', 'access denied', 'request blocked', 'request denied', 'forbidden', 'unauthorized',
  ];

  function selectedPdfURL(raw) {
    let url;
    try { url = new URL(raw); } catch { throw new Error('invalid_pdf_url'); }
    if (url.origin !== 'https://kad.arbitr.ru' || url.username || url.password || url.search || url.hash ||
        !/^\/Kad\/PdfDocument\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/[^/]+\.pdf$/i.test(url.pathname)) {
      throw new Error('invalid_pdf_url');
    }
    return url;
  }

  function responsePdfURL(raw, original) {
    let url;
    try { url = new URL(raw); } catch { throw new Error('pdf_redirect'); }
    const targetPath = original.pathname.replace('/Kad/PdfDocument/', '/Document/Pdf/');
    if (url.origin !== original.origin || url.username || url.password || url.hash ||
        url.pathname !== original.pathname && url.pathname !== targetPath ||
        url.search !== '' && !(url.pathname === targetPath && url.search === '?isAddStamp=True')) {
      throw new Error('pdf_redirect');
    }
    return url.href;
  }

  function latin1(body) {
    const bytes = new Uint8Array(body.length);
    for (let index = 0; index < body.length; index++) bytes[index] = body.charCodeAt(index) & 255;
    return bytes;
  }
  function decodeBase64(body) {
    const binary = atob(body);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
    return bytes;
  }
  function hasPdfMagic(bytes) {
    if (!bytes || bytes.length < PDF_MAGIC.length) return false;
    for (let index = 0; index < PDF_MAGIC.length; index++) if (bytes[index] !== PDF_MAGIC[index]) return false;
    return true;
  }
  function looksLikeHtml(bytes) {
    let index = 0;
    if (bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) index = 3;
    while (index < bytes.length && (bytes[index] === 9 || bytes[index] === 10 || bytes[index] === 13 || bytes[index] === 32)) index += 1;
    return bytes[index] === 0x3c;
  }
  function bytesFromBody(body, base64Encoded) {
    if (typeof body !== 'string') throw new Error('pdf_capture');
    if (base64Encoded) return decodeBase64(body);
    const direct = latin1(body);
    if (hasPdfMagic(direct) || body.length < 8 || body.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(body)) return direct;
    try {
      const decoded = decodeBase64(body);
      if (hasPdfMagic(decoded)) return decoded;
    } catch { /* Keep the original bytes when the text is not a PDF payload. */ }
    return direct;
  }

  function findChallenge(bytes) {
    let carry = '';
    for (let offset = 0; offset < bytes.length; offset += 4096) {
      let text = carry;
      const end = Math.min(bytes.length, offset + 4096);
      for (let index = offset; index < end; index++) text += String.fromCharCode(bytes[index]);
      const lower = text.toLowerCase();
      if (CHALLENGE_MARKERS.some(marker => lower.includes(marker))) return true;
      carry = lower.slice(-32);
    }
    return false;
  }

  function captureFirefox({tabId, url: rawUrl, signal} = {}) {
    if (!api?.webRequest?.filterResponseData || !api?.webRequest?.onBeforeRequest ||
        !api?.webRequest?.onHeadersReceived || !api?.webRequest?.onBeforeRedirect ||
        !api?.webRequest?.onErrorOccurred || !api?.tabs?.update) {
      return Promise.reject(new Error('native_pdf_unavailable'));
    }
    if (!Number.isInteger(tabId) || tabId < 0) return Promise.reject(new Error('invalid_tab'));
    let selected;
    try { selected = selectedPdfURL(rawUrl); } catch (error) { return Promise.reject(error); }
    if (signal?.aborted) return Promise.reject(new Error('aborted'));

    return new Promise((resolve, reject) => {
      let phase = 'initial_get';
      let getRequestId = null;
      let postRequestId = null;
      let expectedUrl = selected.href;
      let getResponse = null;
      let postResponse = null;
      let getTerminal = false;
      let postTerminal = false;
      let directGetPdf = false;
      let settled = false;
      const timer = setTimeout(() => fail('pdf_timeout'), TIMEOUT_MS);

      function removeListeners() {
        api.webRequest.onBeforeRequest.removeListener(onBeforeRequest);
        api.webRequest.onHeadersReceived.removeListener(onHeadersReceived);
        api.webRequest.onBeforeRedirect.removeListener(onBeforeRedirect);
        api.webRequest.onErrorOccurred.removeListener(onErrorOccurred);
        signal?.removeEventListener('abort', onAbort);
        clearTimeout(timer);
      }
      function clearResponse(response) {
        if (!response) return;
        response.chunks = [];
        response.size = 0;
        response.scanTail = '';
      }
      function disconnect(response) {
        const filter = response?.filter;
        if (!filter) return;
        if (!response.started) { response.disconnectPending = true; return; }
        response.filter = null;
        response.disconnectPending = false;
        try { filter.disconnect(); } catch {}
      }
      function close(response) {
        const filter = response?.filter;
        if (!filter) return;
        response.filter = null;
        try { filter.close(); } catch {}
      }
      function fail(reason) {
        if (settled) return;
        settled = true;
        removeListeners();
        disconnect(getResponse);
        disconnect(postResponse);
        clearResponse(getResponse);
        clearResponse(postResponse);
        getResponse = null;
        postResponse = null;
        reject(new Error(reason));
      }
      function collect(response) {
        const bytes = new Uint8Array(response.size);
        let offset = 0;
        for (const part of response.chunks) { bytes.set(part, offset); offset += part.byteLength; }
        return bytes;
      }
      function maybeComplete() {
        if (settled) return;
        if (directGetPdf && getTerminal && getResponse?.magicSeen >= PDF_MAGIC.length) {
          const bytes = collect(getResponse);
          settled = true;
          removeListeners();
          close(getResponse);
          clearResponse(getResponse);
          getResponse = null;
          resolve(bytes);
          return;
        }
        if (phase === 'await_post' && getTerminal && postTerminal && postResponse?.magicSeen >= PDF_MAGIC.length) {
          const bytes = collect(postResponse);
          settled = true;
          removeListeners();
          close(getResponse);
          close(postResponse);
          clearResponse(getResponse);
          clearResponse(postResponse);
          getResponse = null;
          postResponse = null;
          resolve(bytes);
        }
      }
      function matchesTabFrame(details) {
        return !settled && details.tabId === tabId && details.type === 'main_frame' && details.frameId === 0;
      }
      function matchesExpectedURL(raw) {
        try { return responsePdfURL(raw, selected) === expectedUrl; } catch { return false; }
      }
      function scanHtml(response, bytes) {
        let text = '';
        for (const byte of bytes) text += String.fromCharCode(byte);
        const combined = response.scanTail + text.toLowerCase();
        if (CHALLENGE_MARKERS.some(marker => combined.includes(marker))) { fail('pdf_html_challenge'); return false; }
        response.scanTail = combined.slice(-32);
        return true;
      }
      function attachResponse(details, method) {
        const response = {requestId: details.requestId, url: details.url, method, filter: null,
          started: false, disconnectPending: false, headersSeen: false, status: null, contentType: '',
          redirect: false, kind: '', size: 0, chunks: [], magicSeen: 0, scanTail: ''};
        if (method === 'GET') getResponse = response;
        else postResponse = response;
        response.filter = api.webRequest.filterResponseData(details.requestId);
        response.filter.onstart = () => {
          response.started = true;
          if (response.disconnectPending) disconnect(response);
        };
        response.filter.ondata = event => {
          if (settled || (method === 'GET' ? getResponse !== response : postResponse !== response)) return;
          try {
            const view = new Uint8Array(event.data);
            if (response.redirect) { response.filter?.write(event.data); return; }
            response.filter.write(event.data);
            if (!response.headersSeen || !(response.status >= 200 && response.status < 300)) { fail('pdf_headers_missing'); return; }
            if (response.kind === 'html') {
              if (response.size + view.byteLength > MAX_HANDOFF_BYTES) { fail('pdf_handoff_too_large'); return; }
              if (!scanHtml(response, view)) return;
            } else {
              if (response.size + view.byteLength > MAX_BYTES) { fail('pdf_too_large'); return; }
              for (let index = 0; index < view.byteLength && response.magicSeen < PDF_MAGIC.length; index++) {
                if (view[index] !== PDF_MAGIC[response.magicSeen]) { fail('not_pdf'); return; }
                response.magicSeen++;
              }
            }
            response.size += view.byteLength;
            if (response.kind !== 'html') response.chunks.push(new Uint8Array(view));
          } catch { fail('pdf_stream'); }
        };
        response.filter.onstop = () => {
          if (settled) return;
          if (method === 'GET' && getResponse !== response || method === 'POST' && postResponse !== response) {
            if (response.redirect) { disconnect(response); clearResponse(response); }
            return;
          }
          if (response.redirect) { disconnect(response); clearResponse(response); return; }
          if (method === 'GET') {
            if (response.kind === 'html') {
              getTerminal = true;
              close(response);
              clearResponse(response);
              maybeComplete();
              return;
            }
            if (response.magicSeen < PDF_MAGIC.length) { fail('not_pdf'); return; }
            getTerminal = true;
            directGetPdf = true;
            maybeComplete();
            return;
          }
          if (response.magicSeen < PDF_MAGIC.length) { fail('not_pdf'); return; }
          postTerminal = true;
          close(response);
          maybeComplete();
        };
        response.filter.onerror = () => {
          if (settled || response.redirect || (method === 'GET' ? getResponse !== response : postResponse !== response)) return;
          if (method === 'GET' && phase === 'await_post' && postRequestId !== null && response.kind === 'html') {
            return;
          }
          fail('pdf_network');
        };
      }
      function onBeforeRequest(details) {
        if (!matchesTabFrame(details)) return;
        if (!matchesExpectedURL(details.url)) {
          fail('pdf_navigation_mismatch');
          return;
        }
        const method = String(details.method || '').toUpperCase();
        if (phase === 'await_post') {
          if (method !== 'POST' || postRequestId !== null || details.requestId === getRequestId) { fail('pdf_handoff_mismatch'); return; }
          postRequestId = details.requestId;
          try { attachResponse(details, 'POST'); } catch { fail('pdf_capture'); }
          return;
        }
        if (method !== 'GET') { fail('pdf_handoff_mismatch'); return; }
        if (getRequestId !== null && details.requestId !== getRequestId) { fail('pdf_duplicate_request'); return; }
        if (getResponse) { fail('pdf_duplicate_response'); return; }
        getRequestId = details.requestId;
        try { attachResponse(details, 'GET'); } catch { fail('pdf_capture'); }
      }
      function onHeadersReceived(details) {
        if (!matchesTabFrame(details)) return;
        const response = details.requestId === getRequestId ? getResponse : details.requestId === postRequestId ? postResponse : null;
        if (!response || response.url !== details.url || !matchesExpectedURL(details.url)) {
          if (phase === 'await_post' && matchesExpectedURL(details.url)) fail('pdf_handoff_mismatch');
          return;
        }
        try {
          const status = Number(details.statusCode);
          response.status = status;
          response.headersSeen = true;
          if (status >= 300 && status < 400) {
            if (response.method === 'POST') { fail('pdf_post_redirect'); return; }
            response.redirect = true;
            disconnect(response);
            return;
          }
          if (status === 401 || status === 403) { fail('challenge'); return; }
          if (status === 429) { fail('rate-limited'); return; }
          if (!(status >= 200 && status < 300)) { fail('pdf_http'); return; }
          if (response.method === 'POST' && status !== 200) { fail('pdf_http'); return; }
          response.contentType = String((details.responseHeaders || []).find(h => String(h.name).toLowerCase() === 'content-type')?.value || '')
            .split(';', 1)[0].trim().toLowerCase();
          if (response.contentType === 'text/html') {
            if (response.method === 'POST') { fail('pdf_mime'); return; }
            if (response.method !== 'GET' || details.url !== expectedUrl || !details.url.includes('/Document/Pdf/')) { fail('pdf_html'); return; }
            if (status !== 200) { fail('pdf_http'); return; }
            response.kind = 'html';
            phase = 'await_post';
            return;
          }
          if (response.contentType !== 'application/pdf') { fail('pdf_mime'); return; }
          if (response.method === 'POST' && phase !== 'await_post') { fail('pdf_handoff_mismatch'); return; }
          response.kind = 'pdf';
          if (response.method === 'GET') directGetPdf = true;
        } catch { fail('pdf_capture'); }
      }
      function onBeforeRedirect(details) {
        if (!matchesTabFrame(details) || details.requestId !== getRequestId || !getResponse ||
            getResponse.url !== details.url || !matchesExpectedURL(details.url)) return;
        try {
          const status = Number(details.statusCode);
          if (!(status >= 300 && status < 400) || !getResponse.redirect) { fail('pdf_redirect'); return; }
          expectedUrl = responsePdfURL(details.redirectUrl, selected);
          clearResponse(getResponse);
          getResponse = null;
        } catch { fail('pdf_redirect'); }
      }
      function onErrorOccurred(details) {
        if (!matchesTabFrame(details)) return;
        if (details.requestId === getRequestId && getResponse?.url === details.url && matchesExpectedURL(details.url)) {
          if (details.error === 'NS_BINDING_ABORTED' && phase === 'await_post' && postRequestId !== null && getResponse?.kind === 'html') {
            getTerminal = true;
            disconnect(getResponse);
            clearResponse(getResponse);
            maybeComplete();
            return;
          }
          fail('pdf_network');
          return;
        }
        if (details.requestId === postRequestId && postResponse?.url === details.url && matchesExpectedURL(details.url)) { fail('pdf_network'); return; }
        if (getRequestId !== null || phase === 'await_post') fail('pdf_navigation_mismatch');
      }
      function onAbort() { fail('aborted'); }

      try {
        const requestFilter = {urls: ['https://kad.arbitr.ru/*'], types: ['main_frame'], tabId};
        api.webRequest.onBeforeRequest.addListener(onBeforeRequest, requestFilter, ['blocking']);
        api.webRequest.onHeadersReceived.addListener(onHeadersReceived, requestFilter, ['blocking', 'responseHeaders']);
        api.webRequest.onBeforeRedirect.addListener(onBeforeRedirect, requestFilter);
        api.webRequest.onErrorOccurred.addListener(onErrorOccurred, requestFilter);
        signal?.addEventListener('abort', onAbort, {once: true});
        api.tabs.update(tabId, {url: selected.href}).catch(() => fail('pdf_navigation'));
      } catch { fail('pdf_capture'); }
    });
  }

  // Chrome has no response-stream filter. Its PDF viewer also replaces the
  // document body seen by Network.getResponseBody with the viewer page, so the
  // original bytes are read at Fetch response stage and the same response is
  // continued with only its interception id. No URL, header, body, or post data
  // is sent back. maxPostDataSize 0 keeps form values such as token/hash out of
  // the Network event. Requests are not modified or replayed.
  function captureChromium({tabId, url: rawUrl, signal} = {}) {
    if (!api?.debugger?.attach || !api?.debugger?.sendCommand || !api?.debugger?.onEvent || !api?.tabs?.update) {
      return Promise.reject(new Error('native_pdf_unavailable'));
    }
    if (!Number.isInteger(tabId) || tabId < 0) return Promise.reject(new Error('invalid_tab'));
    let selected;
    try { selected = selectedPdfURL(rawUrl); } catch (error) { return Promise.reject(error); }
    if (signal?.aborted) return Promise.reject(new Error('aborted'));

    return new Promise((resolve, reject) => {
      let phase = 'initial_get';
      let expectedUrl = selected.href;
      let mainFrameId = null;
      let getRequestId = null;
      let postRequestId = null;
      let getRecord = null;
      let postRecord = null;
      let getTerminal = false;
      let postTerminal = false;
      let directGetPdf = false;
      let getBytes = null;
      let postBytes = null;
      let settled = false;
      let chain = Promise.resolve();
      const intercepted = new Map();
      const timer = setTimeout(() => fail('pdf_timeout'), TIMEOUT_MS);

      function detach() {
        clearTimeout(timer);
        signal?.removeEventListener('abort', onAbort);
        api.debugger.onEvent.removeListener(onEvent);
        api.debugger.onDetach.removeListener(onDetach);
        api.debugger.sendCommand({tabId}, 'Fetch.disable', {}).catch(() => {});
        api.debugger.detach({tabId}).catch(() => {});
      }
      function fail(reason) {
        if (settled) return;
        settled = true;
        detach();
        reject(new Error(reason));
      }
      function succeed(bytes) {
        if (settled) return;
        settled = true;
        detach();
        resolve(bytes);
      }
      function urlMatches(raw) {
        try { return responsePdfURL(raw, selected) === expectedUrl; } catch { return false; }
      }
      function recordFor(requestId) {
        if (requestId && requestId === getRequestId) return getRecord;
        if (requestId && requestId === postRequestId) return postRecord;
        return null;
      }
      function maybeComplete() {
        if (settled) return;
        if (directGetPdf && getTerminal && getBytes) succeed(getBytes);
        else if (phase === 'await_post' && getTerminal && postTerminal && postBytes) succeed(postBytes);
      }
      function onRequest(params) {
        if (params.type !== 'Document') return;
        if (mainFrameId === null) mainFrameId = params.frameId;
        else if (params.frameId !== mainFrameId) return;
        const url = params.request?.url;
        const method = String(params.request?.method || '').toUpperCase();
        if (params.redirectResponse) {
          const from = params.redirectResponse.url;
          const status = Number(params.redirectResponse.status);
          if (!getRecord || params.requestId !== getRequestId || method !== 'GET' || !(status >= 300 && status < 400) || !from || !urlMatches(from)) {
            fail('pdf_redirect');
            return;
          }
          try { expectedUrl = responsePdfURL(url, selected); } catch { fail('pdf_redirect'); return; }
          getRecord.url = url;
          getRecord.redirect = false;
          getRecord.headersSeen = false;
          getRecord.kind = '';
          return;
        }
        if (!url || !urlMatches(url)) { fail('pdf_navigation_mismatch'); return; }
        if (phase === 'await_post') {
          if (method !== 'POST' || postRequestId !== null || params.requestId === getRequestId) { fail('pdf_handoff_mismatch'); return; }
          postRequestId = params.requestId;
          postRecord = {method, url, headersSeen: false, kind: '', redirect: false, terminal: false};
          return;
        }
        if (method !== 'GET') { fail('pdf_handoff_mismatch'); return; }
        if (getRequestId !== null && params.requestId !== getRequestId) { fail('pdf_duplicate_request'); return; }
        if (getRequestId !== null) { fail('pdf_duplicate_response'); return; }
        getRequestId = params.requestId;
        getRecord = {method, url, headersSeen: false, kind: '', redirect: false, terminal: false};
      }
      function onResponse(params) {
        if (params.type !== 'Document' || params.frameId !== mainFrameId) return;
        const record = recordFor(params.requestId);
        const response = params.response || {};
        if (!record || record.url !== response.url || !urlMatches(response.url)) {
          if (phase === 'await_post' && urlMatches(response.url)) fail('pdf_handoff_mismatch');
          return;
        }
        const status = Number(response.status);
        record.status = status;
        record.headersSeen = true;
        if (status >= 300 && status < 400) {
          if (record.method === 'POST') { fail('pdf_post_redirect'); return; }
          record.redirect = true;
          return;
        }
        if (status === 401 || status === 403) { fail('challenge'); return; }
        if (status === 429) { fail('rate-limited'); return; }
        if (!(status >= 200 && status < 300)) { fail('pdf_http'); return; }
        if (record.method === 'POST' && status !== 200) { fail('pdf_http'); return; }
        const mime = String(response.mimeType || '').split(';', 1)[0].trim().toLowerCase();
        record.mime = mime;
        if (mime === 'text/html') {
          if (record.method === 'POST') { fail('pdf_mime'); return; }
          if (record.method !== 'GET' || response.url !== expectedUrl || !String(response.url).includes('/Document/Pdf/')) { fail('pdf_html'); return; }
          record.kind = 'html';
          phase = 'await_post';
          return;
        }
        if (mime !== 'application/pdf') { fail('pdf_mime'); return; }
        if (record.method === 'POST' && phase !== 'await_post') { fail('pdf_handoff_mismatch'); return; }
        record.kind = 'pdf';
        if (record.method === 'GET') directGetPdf = true;
      }
      async function onFinished(params) {
        const record = recordFor(params.requestId);
        if (!record || record.redirect) return;
        if (!record.headersSeen || !record.kind) { fail('pdf_headers_missing'); return; }
        if (record.terminal) { fail('pdf_duplicate_response'); return; }
        const limit = record.kind === 'html' ? MAX_HANDOFF_BYTES : MAX_BYTES;
        const encoded = Number(params.encodedDataLength);
        if (Number.isFinite(encoded) && encoded > limit) { fail(record.kind === 'html' ? 'pdf_handoff_too_large' : 'pdf_too_large'); return; }
        const stored = intercepted.get(params.requestId);
        if (stored?.error === 'too_large') { fail(record.kind === 'html' ? 'pdf_handoff_too_large' : 'pdf_too_large'); return; }
        let bytes = stored?.bytes || null;
        async function readNetworkBody() {
          const result = await api.debugger.sendCommand({tabId}, 'Network.getResponseBody', {requestId: params.requestId});
          return bytesFromBody(result?.body, result?.base64Encoded === true);
        }
        try {
          if (record.kind === 'pdf' && !hasPdfMagic(bytes)) {
            const fallback = await readNetworkBody();
            if (hasPdfMagic(fallback)) bytes = fallback;
          } else if (!bytes) bytes = await readNetworkBody();
        } catch { if (!bytes) { fail('pdf_capture'); return; } }
        if (settled || recordFor(params.requestId) !== record) return;
        if (!bytes) { fail('pdf_capture'); return; }
        if (bytes.length > limit) { fail(record.kind === 'html' ? 'pdf_handoff_too_large' : 'pdf_too_large'); return; }
        record.terminal = true;
        if (record.kind === 'html') {
          if (findChallenge(bytes)) { fail('pdf_html_challenge'); return; }
          if (record.method === 'GET') getTerminal = true;
          maybeComplete();
          return;
        }
        if (!hasPdfMagic(bytes)) { fail(looksLikeHtml(bytes) ? 'pdf_viewer_shell' : 'not_pdf'); return; }
        if (record.method === 'GET') { getTerminal = true; getBytes = bytes; }
        else { postTerminal = true; postBytes = bytes; }
        maybeComplete();
      }
      function onFailed(params) {
        if (params.type && params.type !== 'Document') return;
        if (mainFrameId !== null && params.frameId !== undefined && params.frameId !== mainFrameId) return;
        if (params.requestId === getRequestId && getRecord && !getRecord.redirect) {
          if (params.canceled === true && phase === 'await_post' && postRequestId !== null && getRecord.kind === 'html') {
            getTerminal = true;
            maybeComplete();
            return;
          }
          fail('pdf_network');
          return;
        }
        if (params.requestId === postRequestId && postRecord) { fail('pdf_network'); return; }
        if (params.type === 'Document' && (getRequestId !== null || phase === 'await_post')) fail('pdf_navigation_mismatch');
      }
      function ownedDocumentUrl(raw) {
        try { responsePdfURL(raw, selected); return true; } catch { return false; }
      }
      async function continuePaused(requestId) {
        try { await api.debugger.sendCommand({tabId}, 'Fetch.continueResponse', {requestId}); }
        catch {
          try { await api.debugger.sendCommand({tabId}, 'Fetch.continueRequest', {requestId}); }
          catch { if (!settled) fail('pdf_capture'); }
        }
      }
      async function onPaused(params) {
        const requestId = params.requestId;
        if (!requestId) return;
        try {
          const status = Number(params.responseStatusCode);
          if (!settled && (!params.resourceType || params.resourceType === 'Document') && params.networkId &&
              status >= 200 && status < 300 && ownedDocumentUrl(params.request?.url)) {
            try {
              const result = await api.debugger.sendCommand({tabId}, 'Fetch.getResponseBody', {requestId});
              const body = bytesFromBody(result?.body, result?.base64Encoded === true);
              intercepted.set(params.networkId, body.length > MAX_BYTES ? {error: 'too_large'} : {bytes: body});
            } catch { /* The completed Network body remains available as a fallback. */ }
          }
        } finally {
          await continuePaused(requestId);
        }
      }
      function onEvent(source, method, params) {
        if (source?.tabId !== tabId) return;
        chain = chain.then(async () => {
          if (method === 'Fetch.requestPaused') { await onPaused(params || {}); return; }
          if (settled) return;
          if (method === 'Network.requestWillBeSent') onRequest(params || {});
          else if (method === 'Network.responseReceived') onResponse(params || {});
          else if (method === 'Network.loadingFinished') await onFinished(params || {});
          else if (method === 'Network.loadingFailed') onFailed(params || {});
        }).catch(error => fail(error?.message || 'pdf_capture'));
      }
      function onDetach(source) {
        if (source?.tabId === tabId) fail('pdf_debug_detached');
      }
      function onAbort() { fail('aborted'); }

      api.debugger.onEvent.addListener(onEvent);
      api.debugger.onDetach.addListener(onDetach);
      let stage = 'attach';
      api.debugger.attach({tabId}, '1.3').then(() => {
        stage = 'enable';
        return api.debugger.sendCommand({tabId}, 'Network.enable', {
          maxPostDataSize: 0,
          maxResourceBufferSize: MAX_BYTES,
          maxTotalBufferSize: MAX_BYTES,
        });
      }).then(() => api.debugger.sendCommand({tabId}, 'Fetch.enable', {
        patterns: [{urlPattern: 'https://kad.arbitr.ru/*', resourceType: 'Document', requestStage: 'Response'}],
      }).catch(() => {})).then(() => {
        if (settled) return;
        stage = 'navigate';
        signal?.addEventListener('abort', onAbort, {once: true});
        return api.tabs.update(tabId, {url: selected.href});
      }).catch(() => fail(signal?.aborted ? 'aborted' : stage === 'navigate' ? 'pdf_navigation' : 'pdf_capture'));
    });
  }

  function capture(options) {
    if (api?.webRequest?.filterResponseData) return captureFirefox(options);
    if (api?.debugger?.attach && api?.debugger?.sendCommand) return captureChromium(options);
    return Promise.reject(new Error('native_pdf_unavailable'));
  }

  globalThis.SroKadNativePdf = {capture};
})();
