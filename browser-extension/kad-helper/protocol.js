/* Job metadata only. No document content, cookies or external services. */
(() => {
  const CASE = /^[АA]\d{1,3}-\d+\/\d{4}$/i;
  const UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
  function normalizeCase(value) {
    if (typeof value !== 'string') throw new Error('invalid_case');
    const result = value.trim().toUpperCase().replace(/^A/, 'А');
    if (!CASE.test(result) || result.length > 64) throw new Error('invalid_case');
    return result;
  }
  function kadURL(value) {
    if (typeof value !== 'string' || value.length > 2048 || /[\s\\]/u.test(value)) throw new Error('invalid_url');
    const url = new URL(value);
    if (url.origin !== 'https://kad.arbitr.ru' || url.username || url.password) throw new Error('invalid_url');
    return url;
  }
  function cardURL(value) {
    const url = kadURL(value);
    if (!new RegExp(`^/Card/${UUID}$`, 'i').test(url.pathname) || url.search || url.hash) throw new Error('invalid_card');
    return url.href;
  }
  function requestId(value) {
    if (typeof value !== 'string' || !new RegExp(`^${UUID}$`, 'i').test(value)) throw new Error('invalid_request');
    return value;
  }
  function launchURL(number, id) {
    return 'https://kad.arbitr.ru/#' + new URLSearchParams({sro_robot: '1', case: normalizeCase(number), run: requestId(id)});
  }
  globalThis.SroKadProtocol = {normalizeCase, kadURL, cardURL, requestId, launchURL};
})();
