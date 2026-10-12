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
  function actDate(value) {
    if (value == null || value === '') return null;
    if (typeof value !== 'string' || !/^[1-9]\d{3}-\d{2}-\d{2}$/.test(value)) throw new Error('invalid_act_date');
    const date = new Date(value + 'T00:00:00Z');
    if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== value) throw new Error('invalid_act_date');
    return value;
  }
  function automaticDocument(candidates, requestedDate) {
    const date = actDate(requestedDate);
    if (date && candidates.some(item => !actDate(item.actDate))) return {error: 'document_date_missing'};
    const matches = date ? candidates.filter(item => item.actDate === date) : candidates;
    if (!matches.length) return {error: 'document_date_not_found'};
    if (matches.length !== 1) return {error: date ? 'ambiguous_document_date' : 'ambiguous_documents'};
    return {candidate: matches[0], reason: date ? 'unique_act_date' : 'only_visible_document'};
  }
  function launchURL(number, id, requestedDate) {
    const params = new URLSearchParams({sro_robot: '1', case: normalizeCase(number), run: requestId(id)});
    const date = actDate(requestedDate);
    if (date) params.set('act_date', date);
    return 'https://kad.arbitr.ru/#' + params;
  }
  globalThis.SroKadProtocol = {normalizeCase, kadURL, cardURL, requestId, actDate, automaticDocument, launchURL};
})();
