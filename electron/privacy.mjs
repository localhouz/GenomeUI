const TRACKING_PARAMS = new Set([
  'utm_source',
  'utm_medium',
  'utm_campaign',
  'utm_term',
  'utm_content',
  'fbclid',
  'gclid',
  'msclkid',
  'mc_eid',
  '_ga',
  'ref',
  'source',
  'affiliate_id',
]);

export function stripTrackingFromUrl(rawUrl) {
  try {
    const parsed = new URL(String(rawUrl || ''));
    let strippedCount = 0;
    for (const key of Array.from(parsed.searchParams.keys())) {
      if (!TRACKING_PARAMS.has(key)) continue;
      parsed.searchParams.delete(key);
      strippedCount += 1;
    }
    return {
      ok: true,
      changed: strippedCount > 0,
      strippedCount,
      url: parsed.toString(),
    };
  } catch {
    return { ok: false, changed: false, strippedCount: 0, url: String(rawUrl || '') };
  }
}

export function sanitizeRequestHeaders(details, requestHeaders = {}) {
  const headers = { ...requestHeaders };
  const ref = String(details?.referrer || '');
  const resourceType = String(details?.resourceType || '');
  if (ref && resourceType === 'mainFrame') {
    delete headers.Referer;
    delete headers.referer;
  }
  headers['X-Genome-Surface'] = '1';
  return headers;
}

export function sanitizeResponseHeaders(details, responseHeaders = {}) {
  const headers = { ...responseHeaders };
  const resourceType = String(details?.resourceType || '');
  const isFrame = resourceType === 'mainFrame' || resourceType === 'subFrame';
  if (!isFrame) {
    delete headers['set-cookie'];
    delete headers['Set-Cookie'];
  }
  return headers;
}

