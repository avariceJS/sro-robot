/* Observe an ordinary top-level PDF navigation and pass its response bytes through unchanged. */
(() => {
  const api = globalThis.browser || globalThis.chrome;
  const MAX_BYTES = 25 * 1024 * 1024;
  const TIMEOUT_MS = 60000;
  const PDF_MAGIC = [0x25, 0x50, 0x44, 0x46, 0x2d];

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
      let requestId = null;
      let expectedUrl = selected.href;
      let activeResponse = null;
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
        if (response) {
          response.chunks = [];
          response.size = 0;
        }
      }
      function disconnect(response) {
        const filter = response?.filter;
        if (!filter) return;
        if (!response.started) {
          response.disconnectPending = true;
          return;
        }
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
        disconnect(activeResponse);
        clearResponse(activeResponse);
        activeResponse = null;
        reject(new Error(reason));
      }
      function complete(response) {
        if (settled || activeResponse !== response) return;
        const bytes = new Uint8Array(response.size);
        let offset = 0;
        for (const part of response.chunks) { bytes.set(part, offset); offset += part.byteLength; }
        settled = true;
        removeListeners();
        close(response);
        clearResponse(response);
        activeResponse = null;
        resolve(bytes);
      }
      function matchesTabFrame(details) {
        return !settled && details.tabId === tabId && details.type === 'main_frame' && details.frameId === 0;
      }
      function matchesExpectedURL(raw) {
        try { return responsePdfURL(raw, selected) === expectedUrl; } catch { return false; }
      }
      function matchesOwned(details) {
        return matchesTabFrame(details) && details.requestId === requestId && matchesExpectedURL(details.url);
      }
      function onBeforeRequest(details) {
        if (!matchesTabFrame(details) || !matchesExpectedURL(details.url)) return;
        try {
          if (requestId !== null && details.requestId !== requestId) { fail('pdf_duplicate_request'); return; }
          if (requestId === null) requestId = details.requestId;
          if (activeResponse) { fail('pdf_duplicate_response'); return; }
          const response = {url: details.url, filter: null, started: false, disconnectPending: false,
            headersSeen: false, status: null, contentType: '', redirect: false, size: 0, chunks: [], magicSeen: 0};
          activeResponse = response;
          response.filter = api.webRequest.filterResponseData(details.requestId);
          response.filter.onstart = () => {
            response.started = true;
            if (response.disconnectPending) disconnect(response);
          };
          response.filter.ondata = event => {
            if (settled || activeResponse !== response) return;
            try {
              if (response.redirect) { response.filter?.write(event.data); return; }
              const view = new Uint8Array(event.data);
              response.filter.write(event.data);
              if (!response.headersSeen || !(response.status >= 200 && response.status < 300)) {
                fail('pdf_headers_missing');
                return;
              }
              if (response.size + view.byteLength > MAX_BYTES) { fail('pdf_too_large'); return; }
              for (let index = 0; index < view.byteLength && response.magicSeen < PDF_MAGIC.length; index++) {
                if (view[index] !== PDF_MAGIC[response.magicSeen]) {
                  fail(response.contentType === 'text/html' ? 'pdf_html' : 'not_pdf');
                  return;
                }
                response.magicSeen++;
              }
              response.size += view.byteLength;
              response.chunks.push(new Uint8Array(view));
            } catch { fail('pdf_stream'); }
          };
          response.filter.onstop = () => {
            if (settled || activeResponse !== response) return;
            if (response.redirect) { disconnect(response); clearResponse(response); return; }
            if (response.magicSeen < PDF_MAGIC.length) {
              fail(response.contentType === 'text/html' ? 'pdf_html' : 'not_pdf');
              return;
            }
            complete(response);
          };
          response.filter.onerror = () => {
            if (activeResponse === response && !response.redirect) fail('pdf_network');
          };
        } catch { fail('pdf_capture'); }
      }
      function onHeadersReceived(details) {
        if (!matchesOwned(details) || !activeResponse) return;
        try {
          const response = activeResponse;
          const status = Number(details.statusCode);
          response.status = status;
          response.headersSeen = true;
          if (status >= 300 && status < 400) {
            response.redirect = true;
            disconnect(response);
            return;
          }
          if (status === 401 || status === 403) { fail('challenge'); return; }
          if (status === 429) { fail('rate-limited'); return; }
          if (!(status >= 200 && status < 300)) { fail('pdf_http'); return; }
          const headers = details.responseHeaders || [];
          response.contentType = String(headers.find(h => String(h.name).toLowerCase() === 'content-type')?.value || '')
            .split(';', 1)[0].trim().toLowerCase();
          if (response.contentType !== 'application/pdf' && response.contentType !== 'text/html') {
            fail('pdf_mime');
          }
        } catch { fail('pdf_capture'); }
      }
      function onBeforeRedirect(details) {
        if (!matchesOwned(details)) return;
        try {
          const status = Number(details.statusCode);
          if (!(status >= 300 && status < 400) || !activeResponse?.redirect) { fail('pdf_redirect'); return; }
          expectedUrl = responsePdfURL(details.redirectUrl, selected);
          clearResponse(activeResponse);
          activeResponse = null;
        } catch (error) {
          fail(error?.message === 'pdf_redirect' ? 'pdf_redirect' : 'pdf_capture');
        }
      }
      function onErrorOccurred(details) {
        if (!matchesTabFrame(details) || !matchesExpectedURL(details.url) ||
            requestId !== null && requestId !== details.requestId) return;
        if (requestId === null) requestId = details.requestId;
        fail('pdf_network');
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
