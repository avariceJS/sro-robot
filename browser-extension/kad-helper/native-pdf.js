/* Observe an ordinary top-level PDF navigation and pass its response bytes through unchanged. */
(() => {
  const api = globalThis.browser || globalThis.chrome;
  const MAX_BYTES = 25 * 1024 * 1024;
  const MAX_HANDOFF_BYTES = 128 * 1024;
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

  function hasKnownChallengeMarker(html) {
    const text = html.toLowerCase();
    return /captcha|ddos\.guard|ddos-guard|js\.challenge|challenge-platform|__js_p_|qrator|access denied|request blocked/.test(text);
  }

  function explicitlyHidden(element) {
    for (let current = element; current; current = current.parentElement) {
      if (current.hasAttribute?.('hidden')) return true;
      if (current.style?.display === 'none') return true;
      if (current === element && current.style?.visibility === 'hidden') return true;
    }
    return false;
  }

  function inspectPostHandoff(html, expectedUrl) {
    const failed = (reason, metadata = {}) => ({ok: false, reason, formCount: 0,
      controlCount: 0, inputCount: 0, ancestorHidden: false, ...metadata});
    if (hasKnownChallengeMarker(html)) return failed('challenge_marker');
    if (typeof DOMParser !== 'function') return failed('unsupported_parser');
    let doc;
    try { doc = new DOMParser().parseFromString(html, 'text/html'); } catch { return failed('malformed_document'); }
    if (!doc || doc.querySelector('parsererror')) return failed('malformed_document');
    const forms = [...doc.querySelectorAll('form')];
    const controls = [...doc.querySelectorAll('input,button,select,textarea,[contenteditable]')];
    const docCounts = {formCount: forms.length, controlCount: controls.length,
      inputCount: controls.filter(control => String(control.localName || '').toLowerCase() === 'input').length};
    if (doc.querySelector('base,meta[http-equiv="refresh"],iframe,object,embed')) return failed('interactive_control', docCounts);
    if (forms.length === 0) return failed('missing_form', docCounts);
    if (forms.length !== 1) return failed('multiple_forms', docCounts);
    const form = forms[0];
    if (String(form.getAttribute('method') || '').trim().toLowerCase() !== 'post') return failed('method_mismatch', docCounts);
    const target = String(form.getAttribute('target') || '').trim().toLowerCase();
    if (target && target !== '_self') return failed('action_mismatch', docCounts);
    let action;
    try { action = new URL(form.getAttribute('action') || '', expectedUrl).href; } catch { return failed('action_mismatch', docCounts); }
    if (action !== expectedUrl) return failed('action_mismatch', docCounts);
    const inputs = [...form.querySelectorAll('input')];
    const counts = {formCount: 1, controlCount: controls.length, inputCount: inputs.length};
    let ancestorHidden = false;
    for (const control of controls) {
      if (inputs.includes(control)) continue;
      const tag = String(control.localName || '').toLowerCase();
      const hidden = explicitlyHidden(control);
      if (tag === 'button' && form.contains(control) && hidden &&
          ['', 'submit'].includes(String(control.getAttribute('type') || '').trim().toLowerCase())) {
        ancestorHidden ||= hidden;
        continue;
      }
      return failed('interactive_control', {...counts, ancestorHidden: hidden});
    }
    const names = new Set();
    for (const input of inputs) {
      const name = String(input.getAttribute('name') || '').trim().toLowerCase();
      if (!['token', 'hash'].includes(name) || names.has(name)) return failed('unsupported_fields', {...counts, ancestorHidden});
      const type = String(input.getAttribute('type') || '').trim().toLowerCase();
      const hidden = explicitlyHidden(input);
      if (type !== 'hidden' && !(type === 'text' && hidden)) return failed('interactive_control', {...counts, ancestorHidden: hidden});
      ancestorHidden ||= type === 'text' && hidden;
      names.add(name);
    }
    if (inputs.length !== 2) return failed('unsupported_fields', {...counts, ancestorHidden});
    if (!names.has('token') || !names.has('hash')) return failed('unsupported_fields', {...counts, ancestorHidden});
    return {ok: true, ...counts, ancestorHidden};
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
      let requestId = null;
      let postRequestId = null;
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
        if (response) { response.chunks = []; response.size = 0; }
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
      function onBeforeRequest(details) {
        if (!matchesTabFrame(details)) return;
        if (!matchesExpectedURL(details.url)) {
          if (phase !== 'initial_get' || requestId !== null) fail('pdf_navigation_mismatch');
          return;
        }
        try {
          const method = String(details.method || '').toUpperCase();
          if (method !== 'GET' && method !== 'POST') { fail('pdf_handoff_mismatch'); return; }
          if (phase === 'await_post') {
            if (method !== 'POST' || postRequestId !== null || details.requestId === requestId) { fail('pdf_handoff_mismatch'); return; }
            postRequestId = details.requestId;
          } else {
            if (method !== 'GET') { fail('pdf_handoff_mismatch'); return; }
            if (requestId !== null && details.requestId !== requestId) { fail('pdf_duplicate_request'); return; }
            if (requestId === null) requestId = details.requestId;
          }
          if (activeResponse) { fail('pdf_duplicate_response'); return; }
          const response = {url: details.url, method, requestId: details.requestId, filter: null,
            started: false, disconnectPending: false, headersSeen: false, status: null,
            contentType: '', redirect: false, size: 0, chunks: [], magicSeen: 0};
          activeResponse = response;
          response.filter = api.webRequest.filterResponseData(details.requestId);
          response.filter.onstart = () => {
            response.started = true;
            if (response.disconnectPending) disconnect(response);
          };
          response.filter.ondata = event => {
            if (settled || activeResponse !== response) return;
            try {
              const view = new Uint8Array(event.data);
              if (response.redirect) { response.filter?.write(event.data); return; }
              response.filter.write(event.data);
              if (!response.headersSeen || !(response.status >= 200 && response.status < 300)) {
                fail('pdf_headers_missing'); return;
              }
              if (response.phaseHtml) {
                if (response.size + view.byteLength > MAX_HANDOFF_BYTES) { fail('pdf_handoff_too_large'); return; }
              } else if (response.size + view.byteLength > MAX_BYTES) { fail('pdf_too_large'); return; }
              if (response.contentType === 'text/html') {
                response.size += view.byteLength;
                response.chunks.push(new Uint8Array(view));
                return;
              }
              for (let index = 0; index < view.byteLength && response.magicSeen < PDF_MAGIC.length; index++) {
                if (view[index] !== PDF_MAGIC[response.magicSeen]) { fail('not_pdf'); return; }
                response.magicSeen++;
              }
              response.size += view.byteLength;
              response.chunks.push(new Uint8Array(view));
            } catch { fail('pdf_stream'); }
          };
          response.filter.onstop = () => {
            if (settled || activeResponse !== response) return;
            if (response.redirect) { disconnect(response); clearResponse(response); return; }
            if (response.contentType === 'text/html') {
              if (phase !== 'initial_get' || response.method !== 'GET' || response.url !== expectedUrl ||
                  !response.url.includes('/Document/Pdf/')) { fail('pdf_html'); return; }
              const htmlBytes = new Uint8Array(response.size);
              let cursor = 0;
              for (const part of response.chunks) { htmlBytes.set(part, cursor); cursor += part.byteLength; }
              let html;
              try { html = new TextDecoder('utf-8', {fatal: true}).decode(htmlBytes); } catch { fail('pdf_handoff_malformed'); return; }
              const handoff = inspectPostHandoff(html, expectedUrl);
              if (!handoff.ok) {
                console.info('kad-pdf-handoff', JSON.stringify({reason: handoff.reason,
                  formCount: handoff.formCount, controlCount: handoff.controlCount,
                  inputCount: handoff.inputCount, ancestorHidden: handoff.ancestorHidden}));
                if (handoff.reason === 'challenge_marker') { fail('pdf_html_challenge'); return; }
                fail(`pdf_handoff_${handoff.reason}`);
                return;
              }
              phase = 'await_post';
              close(response);
              clearResponse(response);
              activeResponse = null;
              return;
            }
            if (response.magicSeen < PDF_MAGIC.length) { fail('not_pdf'); return; }
            if (!(phase === 'await_post' && response.method === 'POST') &&
                !(phase === 'initial_get' && response.method === 'GET')) { fail('pdf_handoff_mismatch'); return; }
            complete(response);
          };
          response.filter.onerror = () => { if (activeResponse === response && !response.redirect) fail('pdf_network'); };
        } catch { fail('pdf_capture'); }
      }
      function onHeadersReceived(details) {
        if (!matchesTabFrame(details)) return;
        if (details.requestId !== (phase === 'await_post' ? postRequestId : requestId) || !matchesExpectedURL(details.url)) {
          if (phase === 'await_post' && matchesExpectedURL(details.url)) fail('pdf_handoff_mismatch');
          return;
        }
        if (!activeResponse) return;
        try {
          const response = activeResponse;
          const status = Number(details.statusCode);
          response.status = status;
          response.headersSeen = true;
          if (status >= 300 && status < 400) {
            if (response.method === 'POST') { fail('pdf_post_redirect'); return; }
            response.redirect = true; disconnect(response); return;
          }
          if (status === 401 || status === 403) { fail('challenge'); return; }
          if (status === 429) { fail('rate-limited'); return; }
          if (!(status >= 200 && status < 300)) { fail('pdf_http'); return; }
          if (response.method === 'POST' && status !== 200) { fail('pdf_http'); return; }
          const headers = details.responseHeaders || [];
          response.contentType = String(headers.find(h => String(h.name).toLowerCase() === 'content-type')?.value || '')
            .split(';', 1)[0].trim().toLowerCase();
          if (response.contentType === 'text/html') {
            if (phase !== 'initial_get' || response.method !== 'GET') { fail('pdf_mime'); return; }
            if (response.url !== expectedUrl || !response.url.includes('/Document/Pdf/')) { fail('pdf_html'); return; }
            response.phaseHtml = true;
            return;
          }
          if (response.contentType !== 'application/pdf' ||
              !(phase === 'await_post' && response.method === 'POST') &&
              !(phase === 'initial_get' && response.method === 'GET')) fail('pdf_mime');
        } catch { fail('pdf_capture'); }
      }
      function onBeforeRedirect(details) {
        if (!matchesTabFrame(details) || details.requestId !== requestId || !matchesExpectedURL(details.url)) return;
        try {
          const status = Number(details.statusCode);
          if (!(status >= 300 && status < 400) || !activeResponse?.redirect) { fail('pdf_redirect'); return; }
          expectedUrl = responsePdfURL(details.redirectUrl, selected);
          clearResponse(activeResponse);
          activeResponse = null;
        } catch { fail('pdf_redirect'); }
      }
      function onErrorOccurred(details) {
        if (!matchesTabFrame(details)) return;
        if (phase === 'await_post' && matchesExpectedURL(details.url) && details.requestId !== postRequestId) { fail('pdf_handoff_mismatch'); return; }
        if (!matchesExpectedURL(details.url) || details.requestId !== (phase === 'await_post' ? postRequestId : requestId)) return;
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
