import crypto from 'node:crypto';

const TOKEN_TTL_MS = 24 * 60 * 60 * 1000;
const tokenStore = new Map();

function normalizeContext(context) {
  const raw = String(context || 'general').toLowerCase().replace(/[^a-z0-9-]/g, '');
  return raw || 'general';
}

function normalizeDomain(domain) {
  const raw = String(domain || '').toLowerCase().replace(/[^a-z0-9.-]/g, '');
  return raw || 'unknown.local';
}

function keyFor(context, domain) {
  return `${normalizeContext(context)}:${normalizeDomain(domain)}`;
}

export function issueIntentToken(context, domain) {
  const now = Date.now();
  const key = keyFor(context, domain);
  const existing = tokenStore.get(key);
  if (existing && Number(existing.expiresAt || 0) > now) {
    return existing.token;
  }
  const token = crypto.randomBytes(24).toString('base64url');
  tokenStore.set(key, {
    token,
    context: normalizeContext(context),
    domain: normalizeDomain(domain),
    issuedAt: now,
    expiresAt: now + TOKEN_TTL_MS,
  });
  return token;
}

export function revokeContext(context) {
  const c = normalizeContext(context);
  for (const key of Array.from(tokenStore.keys())) {
    if (key.startsWith(`${c}:`)) {
      tokenStore.delete(key);
    }
  }
}

export function listContexts() {
  const now = Date.now();
  const contexts = new Set();
  for (const entry of tokenStore.values()) {
    if (Number(entry.expiresAt || 0) <= now) continue;
    contexts.add(String(entry.context || 'general'));
  }
  return Array.from(contexts.values());
}

export function _tokenStoreSize() {
  return tokenStore.size;
}

