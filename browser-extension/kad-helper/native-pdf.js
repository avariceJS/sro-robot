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

  function capture({tabId, url: rawUrl, signal} = {}) {
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

  globalThis.SroKadNativePdf = {capture};
})();
