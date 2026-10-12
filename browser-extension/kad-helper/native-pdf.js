/* Capture a PDF from an ordinary top-level browser navigation while passing every byte through unchanged. */
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

  function redirectPdfURL(raw, original) {
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
    if (!api?.webRequest?.filterResponseData || !api?.webRequest?.onHeadersReceived ||
        !api?.webRequest?.onBeforeRedirect || !api?.webRequest?.onErrorOccurred || !api?.tabs?.update) {
      return Promise.reject(new Error('native_pdf_unavailable'));
    }
    if (!Number.isInteger(tabId) || tabId < 0) return Promise.reject(new Error('invalid_tab'));
    let selected;
    try { selected = selectedPdfURL(rawUrl); } catch (error) { return Promise.reject(error); }
    if (signal?.aborted) return Promise.reject(new Error('aborted'));

    return new Promise((resolve, reject) => {
      let requestId = null;
      let filter = null;
      let filterStarted = false;
      let disconnectPending = false;
      let settled = false;
      let size = 0;
      let chunks = [];
      let expectedRequestUrl = selected.href;
      const timer = setTimeout(() => fail('pdf_timeout'), TIMEOUT_MS);

      function removeListeners() {
        api.webRequest.onHeadersReceived.removeListener(onHeadersReceived);
        api.webRequest.onBeforeRedirect.removeListener(onBeforeRedirect);
        api.webRequest.onErrorOccurred.removeListener(onErrorOccurred);
        signal?.removeEventListener('abort', onAbort);
        clearTimeout(timer);
      }
      function clearChunks() {
        chunks = [];
        size = 0;
      }
      function disconnect() {
        const current = filter;
        if (current && !filterStarted) {
          disconnectPending = true;
          return;
        }
        filter = null;
        disconnectPending = false;
        if (current) {
          try { current.disconnect(); } catch {}
        }
      }
      function closeCompletedStream() {
        const current = filter;
        filter = null;
        if (current) {
          try { current.close(); } catch {}
        }
      }
      function fail(reason) {
        if (settled) return;
        settled = true;
        removeListeners();
        disconnect();
        clearChunks();
        reject(new Error(reason));
      }
      function succeed() {
        if (settled) return;
        const bytes = new Uint8Array(size);
        let offset = 0;
        for (const part of chunks) { bytes.set(part, offset); offset += part.byteLength; }
        settled = true;
        removeListeners();
        closeCompletedStream();
        clearChunks();
        resolve(bytes);
      }
      function owns(details) {
        if (settled || details.tabId !== tabId || details.type !== 'main_frame' || details.frameId !== 0) return false;
        let href;
        try { href = redirectPdfURL(details.url, selected); } catch { return false; }
        return href === expectedRequestUrl;
      }
      function onHeadersReceived(details) {
        if (!owns(details)) return;
        try {
          if (requestId !== null && details.requestId !== requestId) {
            fail('pdf_duplicate_request');
            return;
          }
          if (requestId === null) requestId = details.requestId;
          const status = Number(details.statusCode);
          if (status >= 300 && status < 400) return;
          if (status === 401 || status === 403) { fail('challenge'); return; }
          if (status === 429) { fail('rate-limited'); return; }
          if (!(status >= 200 && status < 300)) { fail('pdf_http'); return; }
          const headers = details.responseHeaders || [];
          const contentType = String(headers.find(h => String(h.name).toLowerCase() === 'content-type')?.value || '')
            .split(';', 1)[0].trim().toLowerCase();
          if (contentType === 'text/html') { fail('pdf_html'); return; }
          if (contentType !== 'application/pdf') { fail('pdf_mime'); return; }
          if (filter) { fail('pdf_duplicate_response'); return; }
          filter = api.webRequest.filterResponseData(details.requestId);
          filter.onstart = () => {
            filterStarted = true;
            if (disconnectPending) disconnect();
          };
          filter.ondata = event => {
            if (settled) return;
            try {
              const view = new Uint8Array(event.data);
              filter.write(event.data);
              if (size + view.byteLength > MAX_BYTES) { fail('pdf_too_large'); return; }
              const copy = new Uint8Array(view);
              size += copy.byteLength;
              chunks.push(copy);
            } catch { fail('pdf_stream'); }
          };
          filter.onstop = () => {
            if (settled) return;
            const prefix = new Uint8Array(Math.min(PDF_MAGIC.length, size));
            let prefixOffset = 0;
            for (const part of chunks) {
              const take = Math.min(part.byteLength, prefix.byteLength - prefixOffset);
              if (take > 0) { prefix.set(part.subarray(0, take), prefixOffset); prefixOffset += take; }
              if (prefixOffset === prefix.byteLength) break;
            }
            if (size < PDF_MAGIC.length || PDF_MAGIC.some((byte, index) => prefix[index] !== byte)) {
              fail('not_pdf');
              return;
            }
            succeed();
          };
          filter.onerror = () => fail('pdf_network');
        } catch {
          fail('pdf_capture');
        }
      }
      function onBeforeRedirect(details) {
        if (!owns(details)) return;
        try {
          if (requestId !== null && details.requestId !== requestId) { fail('pdf_duplicate_request'); return; }
          if (requestId === null) requestId = details.requestId;
          if (!(Number(details.statusCode) >= 300 && Number(details.statusCode) < 400)) { fail('pdf_redirect'); return; }
          expectedRequestUrl = redirectPdfURL(details.redirectUrl, selected);
        } catch (error) {
          fail(error?.message === 'pdf_redirect' ? 'pdf_redirect' : 'pdf_capture');
        }
      }
      function onErrorOccurred(details) {
        if (!owns(details) || requestId !== null && requestId !== details.requestId) return;
        if (requestId === null) requestId = details.requestId;
        fail('pdf_network');
      }
      function onAbort() { fail('aborted'); }

      try {
        api.webRequest.onHeadersReceived.addListener(onHeadersReceived,
          {urls: ['https://kad.arbitr.ru/*'], types: ['main_frame'], tabId}, ['blocking', 'responseHeaders']);
        api.webRequest.onBeforeRedirect.addListener(onBeforeRedirect,
          {urls: ['https://kad.arbitr.ru/*'], types: ['main_frame'], tabId});
        api.webRequest.onErrorOccurred.addListener(onErrorOccurred,
          {urls: ['https://kad.arbitr.ru/*'], types: ['main_frame'], tabId});
        signal?.addEventListener('abort', onAbort, {once: true});
        api.tabs.update(tabId, {url: selected.href}).catch(() => fail('pdf_navigation'));
      } catch {
        fail('pdf_capture');
      }
    });
  }

  globalThis.SroKadNativePdf = {capture};
})();
