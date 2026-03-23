import QRCode from 'qrcode';

const STORAGE_KEY = 'genui_memory_v4';
const SESSION_STORAGE_KEY = 'genui_session_v1';
const DEVICE_STORAGE_KEY = 'genui_device_v1';
const HISTORY_LIMIT = 40;
const FALLBACK_POLL_MS = 2500;
const PRESENCE_HEARTBEAT_MS = 30000;
const WS_RECONNECT_MIN_MS = 1200;
const WS_RECONNECT_MAX_MS = 20000;
const WS_RECONNECT_FACTOR = 1.8;

function safeRandomId(prefix = 'id') {
    try {
        if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
            return crypto.randomUUID();
        }
    } catch { }
    try {
        if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
            const bytes = new Uint8Array(12);
            crypto.getRandomValues(bytes);
            const token = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
            return `${prefix}-${token}`;
        }
    } catch { }
    return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function safeStructuredClone(value) {
    try {
        if (typeof structuredClone === 'function') return structuredClone(value);
    } catch { }
    return JSON.parse(JSON.stringify(value));
}

// Service worker disabled — GenomeUI runs in Electron, not as a PWA.
// SW caching conflicts with clean-boot semantics (OS always boots fresh).
function isElectronRuntime() {
    return typeof navigator !== 'undefined' && /\bElectron\//.test(String(navigator.userAgent || ''));
}

function rewriteLoopbackOrigin(raw = '', fallbackPort = '') {
    try {
        const url = new URL(String(raw || '').trim(), window.location.href);
        const host = String(url.hostname || '').trim();
        if (host !== 'localhost' && host !== '127.0.0.1') return url.origin;
        const localHosts = new Set(['127.0.0.1', '::1']);
        const candidates = [];
        if (Array.isArray(window.__GENOME_HOST_CANDIDATES__)) candidates.push(...window.__GENOME_HOST_CANDIDATES__);
        if (typeof window !== 'undefined' && window.location && window.location.hostname) candidates.push(window.location.hostname);
        const chosen = candidates.map(v => String(v || '').trim()).find(v => v && !localHosts.has(v) && v !== 'localhost');
        if (!chosen) return url.origin;
        url.hostname = chosen;
        if (fallbackPort) url.port = String(fallbackPort);
        return url.origin;
    } catch {
        return String(raw || '').trim();
    }
}

if (typeof window !== 'undefined' && 'serviceWorker' in navigator) {
    if (isElectronRuntime()) {
        navigator.serviceWorker.getRegistrations().then(regs => {
            for (const reg of regs) reg.unregister();
        });
    } else {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js').catch(() => { });
        });
    }
}

const DEFAULT_MEMORY = {
    tasks: [
        { id: safeRandomId(), title: 'Replace mode-based UI with layered synthesis', done: false, createdAt: Date.now() - 7_200_000 },
        { id: safeRandomId(), title: 'Add schema validator for UI plans', done: false, createdAt: Date.now() - 5_400_000 }
    ],
    expenses: [
        { id: safeRandomId(), amount: 28.5, category: 'food', note: 'lunch', createdAt: Date.now() - 86_400_000 },
        { id: safeRandomId(), amount: 96, category: 'cloud', note: 'gpu runtime', createdAt: Date.now() - 43_200_000 }
    ],
    notes: [
        { id: safeRandomId(), text: 'Generative UI should synthesize from intent layers every turn.', createdAt: Date.now() - 3_000_000 }
    ]
};

const RemoteTurnService = {
    async initSession(sessionId) {
        const response = await fetch('/api/session/init', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sessionId })
        });

        if (!response.ok) {
            throw new Error(`Session init failed: ${response.status}`);
        }

        return response.json();
    },

    async getSession(sessionId) {
        const response = await fetch(`/api/session/${encodeURIComponent(sessionId)}`);
        if (!response.ok) {
            throw new Error(`Session fetch failed: ${response.status}`);
        }
        return response.json();
    },

    async process(intent, sessionId, baseRevision, deviceId, onConflict = 'rebase_if_commutative', idempotencyKey = null, activeContent = null, confirmed = false) {
        const authToken = sessionStorage.getItem('genome_session') || '';
        const body = { intent, sessionId, baseRevision, deviceId, onConflict, idempotencyKey };
        if (activeContent) body.activeContent = activeContent;
        if (confirmed) body.confirmed = true;
        const response = await fetch(`/api/turn`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Genome-Auth': authToken,
            },
            body: JSON.stringify(body)
        });

        if (!response.ok) {
            let detail = {};
            try {
                detail = await response.json();
            } catch {
                detail = {};
            }
            const err = new Error(`Remote turn failed: ${response.status}`);
            err.kind = 'remote_http';
            err.status = response.status;
            err.detail = detail?.detail || detail || {};
            if (response.status === 409 && (err.detail?.code === 'revision_conflict')) {
                err.kind = 'revision_conflict';
            } else if (response.status === 403 && (err.detail?.code === 'connector_scope_required')) {
                err.kind = 'permission_denied';
            }
            throw err;
        }

        return response.json();
    },

    async handoffStart(sessionId, deviceId) {
        const response = await fetch(`/api/session/${encodeURIComponent(sessionId)}/handoff/start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ deviceId })
        });
        if (!response.ok) {
            throw new Error(`Handoff start failed: ${response.status}`);
        }
        return response.json();
    },

    async handoffClaim(sessionId, deviceId, token) {
        const response = await fetch(`/api/session/${encodeURIComponent(sessionId)}/handoff/claim`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ deviceId, token })
        });
        if (!response.ok) {
            throw new Error(`Handoff claim failed: ${response.status}`);
        }
        return response.json();
    },

    async presenceHeartbeat(sessionId, deviceId, label, platform) {
        const response = await fetch(`/api/session/${encodeURIComponent(sessionId)}/presence`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                deviceId,
                label,
                platform,
                userAgent: navigator.userAgent || ''
            })
        });
        if (!response.ok) {
            throw new Error(`Presence heartbeat failed: ${response.status}`);
        }
        return response.json();
    }
};

// ─── Semantic Cache ──────────────────────────────────────────────────────────
const SemanticCache = {
    _store: new Map(),  // key → { response, domain, expires }
    _MAX: 120,          // max entries before LRU eviction

    // TTL in seconds per domain. 0 = never cache (always fetch fresh).
    _TTL: {
        // Time-sensitive: short TTL
        sports:       120,   // scores/standings change fast
        music:         60,   // now-playing can change any moment
        rideshare:     60,   // surge pricing fluctuates
        news:         180,   // headlines update frequently
        location:       0,   // always fresh — user moves
        weather:      600,   // forecast stable for ~10 min
        finance:      180,   // stock quotes change, but not every call
        banking:      300,   // balances/transactions: 5 min stale ok
        health:         0,   // biometrics always fresh
        // Semi-stable: medium TTL
        food:         600,   // menus/ratings rarely change mid-session
        travel:       900,   // flight/hotel prices: 15 min ok
        shopping:    1800,   // product listings: 30 min ok
        social:       120,   // feed changes quickly
        // Effectively static: long TTL
        dictionary:  7200,   // word definitions never change
        translation: 7200,   // translations never change
        recipes:     3600,   // recipe data is static
        books:       3600,   // book metadata is static
        // Always fresh — mutations or device state
        calendar:       0,
        email:          0,
        reminders:      0,
        contacts:       0,
        smarthome:      0,
        payments:       0,
    },

    _key(intent) { return intent.trim().toLowerCase(); },

    _ttl(domain) { return ((this._TTL[domain] ?? 0) * 1000); },

    _evict() {
        if (this._store.size < this._MAX) return;
        // Remove the oldest entry (Map preserves insertion order)
        const oldest = this._store.keys().next().value;
        if (oldest !== undefined) this._store.delete(oldest);
    },

    get(intent) {
        const key = this._key(intent);
        const entry = this._store.get(key);
        if (!entry) return null;
        if (Date.now() > entry.expires) { this._store.delete(key); return null; }
        // Move to end (LRU touch)
        this._store.delete(key);
        this._store.set(key, entry);
        return entry.response;
    },

    set(intent, domain, response) {
        const ttl = this._ttl(domain);
        if (ttl === 0) return;
        this._evict();
        this._store.set(this._key(intent), { response, domain, expires: Date.now() + ttl });
    },

    invalidate(domain) {
        for (const [k, v] of this._store) { if (v.domain === domain) this._store.delete(k); }
    }
};

// ─── Sound Engine ─────────────────────────────────────────────────────────────
const SoundEngine = {
    _ctx: null,
    enabled: false,

    _getCtx() {
        if (!this._ctx) this._ctx = new (window.AudioContext || window.webkitAudioContext)();
        if (this._ctx.state === 'suspended') this._ctx.resume();
        return this._ctx;
    },

    _tone(freq, type, duration, gain, startDelay = 0) {
        if (!this.enabled) return;
        try {
            const ctx = this._getCtx();
            const osc = ctx.createOscillator();
            const g   = ctx.createGain();
            osc.connect(g); g.connect(ctx.destination);
            osc.type = type;
            osc.frequency.setValueAtTime(freq, ctx.currentTime + startDelay);
            g.gain.setValueAtTime(0.001, ctx.currentTime + startDelay);
            g.gain.linearRampToValueAtTime(gain, ctx.currentTime + startDelay + 0.01);
            g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + startDelay + duration);
            osc.start(ctx.currentTime + startDelay);
            osc.stop(ctx.currentTime + startDelay + duration + 0.05);
        } catch (_) {}
    },

    // Soft ascending triad — scene transition
    transition() {
        this._tone(523.25, 'sine', 0.28, 0.07);
        this._tone(659.25, 'sine', 0.28, 0.055, 0.07);
        this._tone(783.99, 'sine', 0.32, 0.045, 0.14);
    },

    // Descending minor 2nd — error
    error() {
        this._tone(440,   'sine', 0.15, 0.1);
        this._tone(415.3, 'sine', 0.22, 0.08, 0.12);
    },

    // Bright ping — success
    success() {
        this._tone(1046.5, 'sine', 0.18, 0.07);
        this._tone(1318.5, 'sine', 0.14, 0.05, 0.09);
    },

    // 4 kHz click transient — button press
    click() { this._tone(4000, 'square', 0.035, 0.04); },

    // Low descending pair — high-risk confirm prompt
    confirm() {
        this._tone(370, 'sine', 0.22, 0.09);
        this._tone(311, 'sine', 0.28, 0.07, 0.18);
    },

    toggle() {
        this.enabled = !this.enabled;
        return this.enabled;
    }
};

const UIEngine = {
    container: document.getElementById('ui-container'),
    input: document.getElementById('intent-input'),
    status: document.getElementById('status'),
    historyReel: document.getElementById('history-reel'),
    inputContainer: document.querySelector('.input-container'),

    state: {
        memory: safeStructuredClone(DEFAULT_MEMORY),
        session: {
            lastIntent: '',
            lastEnvelope: null,
            lastExecution: null,
            lastPlan: null,
            lastKernelTrace: null,
            handoff: { activeDeviceId: null, pending: null, lastClaimAt: null },
            presence: { activeCount: 0, count: 0, items: [], timeoutMs: 120000, updatedAt: 0 },
            workspace: { repoId: 'user-global', branch: 'main', worktrees: {}, activeContent: null },
            activeHistoryIndex: -1,
            sessionId: '',
            deviceId: '',
            revision: 0,
            graphSnapshot: null,
            graphSnapshotRevision: 0,
            graphFetchInFlight: false,
            isApplyingLocalTurn: false,
            syncTransport: 'idle',
            networkOnline: true,
            reconnectAttempts: 0,
            locationHint: ''
        },
        history: [],
        intentHistory: [],
        intentHistoryIndex: -1,
        metrics: {
            latency: 0,
            entropy: 0.02
        },
        sceneDock: {
            activeDomain: 'generic',
            lastTransition: null
        },
        webdeck: {
            mode: 'surface'
        },
        pwa: {
            installReady: false,
            installed: false
        },
        runtimeEvents: [],
        activeSurface: null   // { domain, name, getData, cleanup } — set while a functional surface is mounted
    },

    async init() {
        this._userHasActed = false;
        this._bootGuardUntil = Date.now() + 600; // block all renders for first 600ms
        this._deferredInstallPrompt = null;
        this.container.classList.add('visible');
        // Preserve runtime/worktree/history state across boots, but never auto-restore a stale scene.
        sessionStorage.removeItem('genome_session'); // require auth on every boot — OS behaviour
        this.loadState();
        this.renderWelcome();
        this.primeRelativeLocationContext().catch(() => { });
        this.setupElectronChrome();
        this.setupUXChrome();
        this.setupPwaRuntime();
        this.setConnectivity(typeof navigator?.onLine === 'boolean' ? navigator.onLine : true);
        this.bindEvents();
        this.updateShortcutHint();
        await this.ensureAuth();
        await this.runBootSequence();
        this.handleOAuthCallback();
        this._initNetworkMesh();
        this._initNotifications();
        this.showToast('Surface ready. Press ? for command help.', 'info', 3000);
    },

    // ── Passkey Authentication ───────────────────────────────────────────────

    async ensureAuth() {
        // Check if auth is enabled on the backend
        let status;
        try {
            const res = await fetch('/api/auth/status');
            status = await res.json();
        } catch {
            return; // backend unreachable — allow through, WS will gate
        }
        if (!status.enabled) return;

        // Auth is enabled — check for a live session token
        const existing = sessionStorage.getItem('genome_session');
        if (existing) return; // assume valid; WS close 4401 will re-trigger if expired

        // Need to authenticate (or register for the first time)
        return new Promise((resolve) => {
            this._showAuthScreen(status.registered, resolve);
        });
    },

    _showAuthScreen(registered, onSuccess) {
        // Remove any existing auth overlay
        document.getElementById('genome-auth-overlay')?.remove();

        const overlay = document.createElement('div');
        overlay.id = 'genome-auth-overlay';
        overlay.className = 'auth-overlay';
        overlay.innerHTML = `
            <div class="auth-card">
                <div class="auth-logo">
                    <span class="auth-logo-g">G</span><span class="auth-logo-rest">enomeUI</span>
                </div>
                <div class="auth-headline">
                    ${registered ? 'Welcome back' : 'Set up your passkey'}
                </div>
                <div class="auth-sub">
                    ${registered
                        ? 'Authenticate with your device to continue.'
                        : 'Your private key stays on this device. No password required.'}
                </div>
                <button class="auth-btn" id="auth-action-btn">
                    ${registered ? 'Authenticate' : 'Create passkey'}
                </button>
                <div class="auth-hint" id="auth-hint"></div>
            </div>
        `;
        document.body.appendChild(overlay);

        const btn  = overlay.querySelector('#auth-action-btn');
        const hint = overlay.querySelector('#auth-hint');

        btn.addEventListener('click', async () => {
            btn.disabled = true;
            btn.textContent = registered ? 'Waiting for device…' : 'Creating passkey…';
            hint.textContent = '';
            try {
                const token = registered
                    ? await this._authenticatePasskey()
                    : await this._registerPasskey();
                sessionStorage.setItem('genome_session', token);
                overlay.classList.add('auth-overlay--done');
                setTimeout(() => { overlay.remove(); onSuccess(); }, 400);
            } catch (err) {
                btn.disabled = false;
                btn.textContent = registered ? 'Try again' : 'Retry';
                hint.textContent = err.message || 'Authentication failed — please try again.';
            }
        });
    },

    async _registerPasskey() {
        const res     = await fetch('/api/auth/register/begin', { method: 'POST' });
        const options = await res.json();
        const nonce   = options.nonce;

        const createOpts = {
            publicKey: {
                ...options,
                challenge:     this._fromB64url(options.challenge),
                user: {
                    ...options.user,
                    id: this._fromB64url(options.user.id),
                },
                excludeCredentials: (options.excludeCredentials || []).map((c) => ({
                    ...c, id: this._fromB64url(c.id),
                })),
            },
        };
        const credential = await navigator.credentials.create(createOpts);
        const body = {
            nonce,
            credential: {
                id:    credential.id,
                rawId: this._b64url(credential.rawId),
                type:  credential.type,
                response: {
                    clientDataJSON:    this._b64url(credential.response.clientDataJSON),
                    attestationObject: this._b64url(credential.response.attestationObject),
                },
            },
        };
        const completeRes = await fetch('/api/auth/register/complete', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(body),
        });
        const data = await completeRes.json();
        if (!data.ok) throw new Error(data.detail || 'Registration failed');
        return data.sessionToken;
    },

    async _authenticatePasskey() {
        const res     = await fetch('/api/auth/login/begin', { method: 'POST' });
        if (res.status === 409) throw new Error('No passkey registered — reload and set up first.');
        const options = await res.json();
        const nonce   = options.nonce;

        const getOpts = {
            publicKey: {
                ...options,
                challenge:       this._fromB64url(options.challenge),
                allowCredentials: (options.allowCredentials || []).map((c) => ({
                    ...c, id: this._fromB64url(c.id),
                })),
            },
        };
        const assertion = await navigator.credentials.get(getOpts);
        const body = {
            nonce,
            assertion: {
                id:    assertion.id,
                rawId: this._b64url(assertion.rawId),
                type:  assertion.type,
                response: {
                    clientDataJSON:    this._b64url(assertion.response.clientDataJSON),
                    authenticatorData: this._b64url(assertion.response.authenticatorData),
                    signature:         this._b64url(assertion.response.signature),
                    userHandle:        assertion.response.userHandle
                        ? this._b64url(assertion.response.userHandle)
                        : null,
                },
            },
        };
        const completeRes = await fetch('/api/auth/login/complete', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(body),
        });
        const data = await completeRes.json();
        if (!data.ok) throw new Error(data.detail || 'Authentication failed');
        return data.sessionToken;
    },

    _b64url(buf) {
        return btoa(String.fromCharCode(...new Uint8Array(buf)))
            .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
    },

    _fromB64url(s) {
        s = s.replace(/-/g, '+').replace(/_/g, '/');
        while (s.length % 4) s += '=';
        return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
    },

    // ────────────────────────────────────────────────────────────────────────

    async primeRelativeLocationContext() {
        // Always try GPS — it's more accurate than IP geolocation.
        // Only skip re-detection if we already have clean GPS COORDINATES (lat,lon format).
        // If hint is a verbose city string (e.g. "Tulsa, Oklahoma, United States" from old nominatim
        // data), it will fail open-meteo geocoding → fallback. Force re-detection in that case.
        const existing = String(this.state.session.locationHint || '').trim();
        const isCoords = /^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$/.test(existing);
        if (existing && isCoords && this.state.session._locationHintFromGPS) return;
        const hint = await this.detectBrowserLocationHint();
        const normalized = String(hint || '').trim();
        if (!normalized) return;
        this.state.session.locationHint = normalized;
        this.state.session._locationHintFromGPS = true;
        this.saveState();
        await this.persistHomeLocationHint(normalized).catch(() => { });
    },

    async detectBrowserLocationHint() {
        if (typeof navigator === 'undefined' || !navigator.geolocation) return '';
        try {
            const position = await new Promise((resolve, reject) => {
                navigator.geolocation.getCurrentPosition(resolve, reject, {
                    enableHighAccuracy: false,
                    timeout: 4500,
                    maximumAge: 300000
                });
            });
            const lat = Number(position?.coords?.latitude || 0);
            const lon = Number(position?.coords?.longitude || 0);
            if (!Number.isFinite(lat) || !Number.isFinite(lon)) return '';
            // Return raw coordinates — backend uses them directly with open-meteo,
            // skipping geocoding. More accurate and never fails due to city name formatting.
            return `${lat.toFixed(4)},${lon.toFixed(4)}`;
        } catch {
            return '';
        }
    },

    async persistHomeLocationHint(hint) {
        const value = String(hint || '').trim();
        if (!value) return;
        await fetch('/api/connectors/secrets', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: 'user.home.location', value })
        });
    },

    setupElectronChrome() {
        const isElectron = Boolean(window.electronAPI?.isElectron);
        if (isElectron) {
            document.body.classList.add('electron-mode');
        }
        const controls = document.getElementById('window-controls');
        if (controls) {
            controls.style.display = isElectron ? 'inline-flex' : 'none';
        }
        const closeBtn = document.getElementById('wc-close');
        const minBtn = document.getElementById('wc-minimize');
        const maxBtn = document.getElementById('wc-maximize');
        if (isElectron) {
            closeBtn?.addEventListener('click', () => window.electronAPI?.closeWindow?.());
            minBtn?.addEventListener('click', () => window.electronAPI?.minimizeWindow?.());
            maxBtn?.addEventListener('click', () => window.electronAPI?.maximizeWindow?.());
        }
    },

    setupUXChrome() {
        // Sound toggle
        const soundBtn = document.getElementById('sound-toggle');
        if (soundBtn) {
            soundBtn.addEventListener('click', () => {
                const on = SoundEngine.toggle();
                soundBtn.classList.toggle('on', on);
                soundBtn.title = on ? 'Sound on (Alt+S)' : 'Sound off (Alt+S)';
                if (on) SoundEngine.click();
            });
        }

        const toast = document.createElement('div');
        toast.id = 'ux-toast';
        document.body.appendChild(toast);
        this.toast = toast;

        const help = document.createElement('div');
        help.id = 'help-overlay';
        help.innerHTML = `
            <div class="help-panel">
                <div class="help-title">Command Guide</div>
                <div class="help-sub">Intent-first controls for this surface.</div>
                <div class="help-section">Shortcuts</div>
                <div class="help-line"><span>/</span><span>Focus command input</span></div>
                <div class="help-line"><span>Ctrl/Cmd+K</span><span>Focus and select input</span></div>
                <div class="help-line"><span>Ctrl/Cmd+N</span><span>Open a new window</span></div>
                <div class="help-line"><span>?</span><span>Open or close this guide</span></div>
                <div class="help-line"><span>Esc</span><span>Close guide</span></div>
                <div class="help-line"><span>Up or Down</span><span>Recall prior intents</span></div>
                <div class="help-line"><span>Alt+&larr;/&rarr;</span><span>Restore prev or next scene</span></div>
                <div class="help-line"><span>Alt+Shift+&larr;/&rarr;</span><span>Cycle semantic scene dock</span></div>
                <div class="help-line"><span>Alt+1..9</span><span>Run numbered quick command</span></div>
                <div class="help-line"><span>Alt+M</span><span>Toggle webdeck surface/full mode</span></div>
                <div class="help-line"><span>Swipe &larr;/&rarr;</span><span>Mobile history navigation</span></div>
                <div class="help-section">Intent Examples</div>
                <div class="help-line"><span>add task ship mobile shell</span><span>Create task</span></div>
                <div class="help-line"><span>complete task 1</span><span>Toggle task state</span></div>
                <div class="help-line"><span>add expense 34 transport train</span><span>Track spend</span></div>
                <div class="help-line"><span>add note model by intent routing</span><span>Capture note</span></div>
                <div class="help-line"><span>show tasks</span><span>Focus domain</span></div>
            </div>
        `;
        document.body.appendChild(help);
        this.helpOverlay = help;

        const boot = document.createElement('div');
        boot.id = 'boot-sequence';
        boot.innerHTML = `
            <div class="boot-shell">
                <div class="boot-title">Genome Surface OS</div>
                <div class="boot-subtitle">Generative runtime booting...</div>
                <div class="boot-progress">
                    <div id="boot-progress-bar"></div>
                </div>
                <div id="boot-stage" class="boot-stage">Initializing shell</div>
                <div id="boot-meta" class="boot-meta">0%</div>
            </div>
        `;
        document.body.appendChild(boot);
        this.bootOverlay = boot;
        this.bootProgress = document.getElementById('boot-progress-bar');
        this.bootStage = document.getElementById('boot-stage');
        this.bootMeta = document.getElementById('boot-meta');

        const shortcutHint = document.createElement('div');
        shortcutHint.id = 'shortcut-hint';
        this.status.insertAdjacentElement('afterend', shortcutHint);
        this.shortcutHint = shortcutHint;

        const offline = document.createElement('div');
        offline.id = 'offline-overlay';
        offline.innerHTML = `
            <div class="offline-shell">
                <div class="offline-title">Offline Mode</div>
                <div class="offline-copy">Realtime sync is unavailable. Local intent handling is still active.</div>
                <button type="button" class="offline-retry-btn" data-offline-retry>Retry connection</button>
            </div>
        `;
        document.body.appendChild(offline);
        this.offlineOverlay = offline;
    },

    async runBootSequence() {
        this.input.disabled = true;
        this.setBootState('Initializing shell context', 8);
        await sleep(120);

        this.setBootState('Wiring session transport', 26);
        await this.bootstrapSession();
        await sleep(90);

        this.setBootState('Rehydrating shared runtime', 44);
        await this.rehydrateBootRuntime();
        await sleep(80);

        this.setBootState('Compiling startup intent layers', 60);
        // OS always boots to Latent Surface — session graph history is preserved
        // and accessible via the history reel, but boot never restores last scene.
        this.renderWelcome();
        await sleep(110);

        this.setBootState('Synchronizing surface runtime', 82);
        this.updateHistoryReel();
        this.updateStatus('READY');
        this.startRealtimeSync();
        await sleep(90);

        this.setBootState('Surface online', 100);
        setTimeout(() => this.container.classList.add('visible'), 220);
        this.input.disabled = false;
        this.input.focus();
        await sleep(170);
        this.bootOverlay.classList.add('done');
    },

    markUserEngaged() {
        this._userHasActed = true;
    },

    renderWelcome() {
        const workspace = (this.state.session.workspace && typeof this.state.session.workspace === 'object')
            ? this.state.session.workspace
            : {};
        const active = (workspace.activeContent && typeof workspace.activeContent === 'object')
            ? workspace.activeContent
            : {};
        const worktrees = (workspace.worktrees && typeof workspace.worktrees === 'object')
            ? Object.values(workspace.worktrees).filter((item) => item && typeof item === 'object')
            : [];
        const unread = Array.isArray(this.state.runtimeEvents)
            ? this.state.runtimeEvents.filter((item) => item && typeof item === 'object' && !item.read).length
            : 0;
        const recentHistory = Array.isArray(this.state.history) ? this.state.history.slice(-3).reverse() : [];
        const resumeCards = [];
        if (active.name) {
            resumeCards.push(`
                <button type="button" class="welcome-tile" data-shell-object-kind="repo" data-shell-object-domain="${escapeAttr(String(active.domain || 'document'))}" data-shell-object-name="${escapeAttr(String(active.name || ''))}" data-shell-object-branch="${escapeAttr(String(active.branch || workspace.branch || 'main'))}" data-shell-object-item-id="${escapeAttr(String(active.itemId || ''))}">
                    <div class="welcome-tile-label">resume ${escapeHtml(String(active.name || 'active object'))}</div>
                </button>
            `);
        }
        const worktreeCards = worktrees.slice(0, 3).map((item) => `
            <button type="button" class="welcome-tile" data-shell-object-kind="worktree" data-shell-object-domain="${escapeAttr(String(item.domain || 'document'))}" data-shell-object-name="${escapeAttr(String(item.name || ''))}" data-shell-object-branch="${escapeAttr(String(item.branch || workspace.branch || 'main'))}" data-shell-object-item-id="${escapeAttr(String(item.itemId || ''))}">
                <div class="welcome-tile-label">open ${escapeHtml(String(item.name || 'worktree'))}</div>
            </button>
        `);
        if (unread > 0) {
            resumeCards.push(`
                <button type="button" class="welcome-tile" data-scene-domain="notifications">
                    <div class="welcome-tile-label">review ${escapeHtml(String(unread))} alerts</div>
                </button>
            `);
        }
        resumeCards.push(`
            <button type="button" class="welcome-tile" data-scene-domain="content">
                <div class="welcome-tile-label">open content repo</div>
            </button>
        `);
        resumeCards.push(`
            <button type="button" class="welcome-tile" data-scene-domain="continuity">
                <div class="welcome-tile-label">inspect continuity</div>
            </button>
        `);
        if (worktrees.length > 0) {
            resumeCards.push(`
                <button type="button" class="welcome-tile" data-repo-worktrees="1">
                    <div class="welcome-tile-label">inspect ${escapeHtml(String(worktrees.length))} worktrees</div>
                </button>
            `);
        }
        const welcomeIntents = [
            'show weather in my city',
            'show me running shoes',
            'show my tasks',
            'search web local-first app design'
        ];
        this.container.innerHTML = `
            <div class="welcome-surface">
                <div class="welcome-title">Genome Surface OS</div>
                <div class="welcome-sub">Latent surface loaded. Runtime context is preserved; stale scenes stay asleep.</div>
                <div class="welcome-tiles">
                    ${resumeCards.join('')}
                </div>
                ${worktreeCards.length ? `
                    <div class="welcome-tiles">
                        ${worktreeCards.join('')}
                    </div>
                ` : ''}
                <div class="welcome-sub" style="margin-top:18px;">
                    branch ${escapeHtml(String(workspace.branch || 'main'))}
                    · ${escapeHtml(String(worktrees.length))} worktrees
                    · ${escapeHtml(String(this.state.session.presence?.activeCount || 0))} active devices
                    · ${escapeHtml(String(recentHistory.length))} recent traces
                </div>
                ${recentHistory.length ? `
                    <div class="welcome-tiles">
                        ${recentHistory.map((entry) => {
                            const target = this.inferHistoryEntryShellTarget(entry);
                            if (target.type === 'repo') {
                                return `
                                    <button type="button" class="welcome-tile" data-shell-object-kind="repo" data-shell-object-domain="${escapeAttr(String(target.domain || 'document'))}" data-shell-object-name="${escapeAttr(String(target.name || ''))}" data-shell-object-branch="${escapeAttr(String(target.branch || 'main'))}">
                                        <div class="welcome-tile-label">${escapeHtml(String(target.label || 'resume object'))}</div>
                                    </button>
                                `;
                            }
                            if (target.type === 'scene') {
                                return `
                                    <button type="button" class="welcome-tile" data-shell-object-kind="scene" data-shell-object-scene="${escapeAttr(String(target.value || 'generic'))}">
                                        <div class="welcome-tile-label">${escapeHtml(String(target.label || 'resume surface'))}</div>
                                    </button>
                                `;
                            }
                            return `
                                <button type="button" class="welcome-tile" data-command="${escapeAttr(String(target.value || 'resume surface'))}">
                                    <div class="welcome-tile-label">${escapeHtml(String(target.label || target.value || 'resume surface'))}</div>
                                </button>
                            `;
                        }).join('')}
                    </div>
                ` : ''}
                <div class="welcome-tiles">
                    ${welcomeIntents.map((intent) => `
                        <button type="button" class="welcome-tile" data-command="${escapeAttr(intent)}">
                            <div class="welcome-tile-label">${escapeHtml(intent)}</div>
                        </button>
                    `).join('')}
                </div>
            </div>
        `;
        this.decorateQuickCommandTargets();
    },

    hydrateRemoteHistory(turnHistory, lastTurn = null) {
        if (Array.isArray(this.state.history) && this.state.history.length) return;
        const seedMemory = safeStructuredClone(this.state.memory || DEFAULT_MEMORY);
        const remote = Array.isArray(turnHistory) ? turnHistory : [];
        const entries = remote
            .filter((item) => item && typeof item === 'object')
            .map((item) => {
                const intent = String(item.intent || '').trim();
                if (!intent) return null;
                return {
                    intent,
                    summary: String(item.execution?.message || intent).trim() || intent,
                    timestamp: Number(item.timestamp || item.ts || Date.now()),
                    thumbnail: null,
                    memorySnapshot: safeStructuredClone(seedMemory),
                    envelope: null,
                    plan: null,
                    executionSnapshot: item.execution || null,
                    kernelTrace: null,
                    planner: String(item.route?.target || '').trim(),
                    remoteStub: true,
                };
            })
            .filter(Boolean);
        if (lastTurn?.envelope && lastTurn?.plan) {
            const richEntry = {
                intent: String(lastTurn.intent || 'resume surface').trim() || 'resume surface',
                summary: String(lastTurn.execution?.message || lastTurn.intent || 'Session activity').trim() || 'Session activity',
                timestamp: Number(lastTurn.timestamp || Date.now()),
                thumbnail: null,
                memorySnapshot: safeStructuredClone(seedMemory),
                envelope: lastTurn.envelope,
                plan: lastTurn.plan,
                executionSnapshot: lastTurn.execution || null,
                kernelTrace: lastTurn.kernelTrace || null,
                planner: String(lastTurn.planner || '').trim(),
                remoteStub: false,
            };
            if (entries.length && entries[entries.length - 1]?.intent === richEntry.intent) {
                entries[entries.length - 1] = richEntry;
            } else {
                entries.push(richEntry);
            }
        }
        if (!entries.length) return;
        this.state.history = entries.slice(-HISTORY_LIMIT);
        this.state.session.activeHistoryIndex = this.state.history.length - 1;
    },

    setBootState(label, percent) {
        if (this.bootStage) this.bootStage.textContent = label;
        if (this.bootMeta) this.bootMeta.textContent = `${Math.max(0, Math.min(100, Math.round(percent)))}%`;
        if (this.bootProgress) this.bootProgress.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    },

    async bootstrapSession() {
        this.state.session.deviceId = this.ensureDeviceId();
        const preferred = this.resolveSessionIdFromUrl() || this.state.session.sessionId;
        try {
            const session = await RemoteTurnService.initSession(preferred);
            this.state.session.sessionId = session.sessionId;
            this.state.session.revision = session.revision || 0;
            this.state.memory = session.memory || this.state.memory;
            this.state.session.handoff = session.handoff || this.state.session.handoff;
            this.state.session.presence = session.presence || this.state.session.presence;
            this.state.session.workspace = session.workspace || this.state.session.workspace;
            this.mergeRuntimeEvents(session.notifications, { replace: true });
            this.hydrateRemoteHistory(session.turnHistory, session.lastTurn);
            this.writeSessionIdToUrl(session.sessionId);
            localStorage.setItem(SESSION_STORAGE_KEY, session.sessionId);
            await this.tryClaimHandoffFromUrl();
            try {
                await this.sendPresenceHeartbeat(true);
            } catch {
                // Presence heartbeat should never block boot.
            }
        } catch {
            if (!this.state.session.sessionId) {
                this.state.session.sessionId = this.generateLocalSessionId();
                this.writeSessionIdToUrl(this.state.session.sessionId);
                localStorage.setItem(SESSION_STORAGE_KEY, this.state.session.sessionId);
            }
        }
    },

    ensureDeviceId() {
        const cached = String(localStorage.getItem(DEVICE_STORAGE_KEY) || '').trim();
        if (cached) return cached;
        const next = `dev-${Math.random().toString(36).slice(2, 10)}`;
        localStorage.setItem(DEVICE_STORAGE_KEY, next);
        return next;
    },

    resolveHandoffTokenFromUrl() {
        const params = new URLSearchParams(window.location.search);
        return String(params.get('handoff') || '').trim();
    },

    clearHandoffTokenInUrl() {
        const url = new URL(window.location.href);
        url.searchParams.delete('handoff');
        window.history.replaceState({}, '', url);
    },

    async tryClaimHandoffFromUrl() {
        const token = this.resolveHandoffTokenFromUrl();
        if (!token || !this.state.session.sessionId || !this.state.session.deviceId) return;
        try {
            await this.claimHandoffToken(token);
            this.showToast('Handoff claimed on this device.', 'ok', 2600);
        } catch {
            this.showToast('Handoff token rejected or expired.', 'warn', 2800);
        } finally {
            this.clearHandoffTokenInUrl();
        }
    },

    // ── OAuth Connector Flow ─────────────────────────────────────────────────

    /** Called once on init — handles ?oauth_success or ?oauth_error redirects from popup. */
    handleOAuthCallback() {
        const params = new URLSearchParams(window.location.search);
        const success = String(params.get('oauth_success') || '').trim();
        const error   = String(params.get('oauth_error')   || '').trim();
        if (!success && !error) return;
        // Strip params from URL immediately
        const url = new URL(window.location.href);
        url.searchParams.delete('oauth_success');
        url.searchParams.delete('oauth_error');
        window.history.replaceState({}, '', url);
        if (window.opener && typeof window.opener.postMessage === 'function') {
            // Running inside OAuth popup — notify parent and close
            window.opener.postMessage({ type: 'genome_oauth', success, error }, window.location.origin);
            window.close();
            return;
        }
        // Running in main window (redirect-based flow rather than popup)
        if (success) {
            this.showToast(`${success} connected.`, 'ok', 2800);
            this.showConnectionsPanel(success);
        } else if (error) {
            this.showToast(`OAuth error: ${error}`, 'warn', 3500);
        }
    },

    /** Open OAuth popup for a service. */
    async _oauthConnectService(svc) {
        const token = sessionStorage.getItem('genome_session') || '';
        try {
            const headers = token ? { 'X-Genome-Auth': token } : {};
            const res = await fetch(`/api/connectors/oauth/${encodeURIComponent(svc)}/begin`, { headers });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                this.showToast(err.detail || `Cannot start OAuth for ${svc} (${res.status}).`, 'warn', 4000);
                return;
            }
            const { url } = await res.json();
            if (!url) { this.showToast('No auth URL returned.', 'warn', 2600); return; }
            const popup = window.open(url, 'genome-oauth', 'width=600,height=700,left=200,top=100');
            if (!popup) { this.showToast('Popup blocked. Allow popups for this page.', 'warn', 3000); return; }
            const onMsg = (evt) => {
                if (evt.origin !== window.location.origin) return;
                if (!evt.data || evt.data.type !== 'genome_oauth') return;
                window.removeEventListener('message', onMsg);
                if (evt.data.success) {
                    this.showToast(`${evt.data.success} connected.`, 'ok', 2800);
                    this.showConnectionsPanel(evt.data.success);
                } else if (evt.data.error) {
                    this.showToast(`OAuth error: ${evt.data.error}`, 'warn', 3500);
                }
            };
            window.addEventListener('message', onMsg);
        } catch {
            this.showToast(`Failed to connect ${svc}.`, 'warn', 2600);
        }
    },

    /** Expand a conn-card into an inline credential input form. */
    _showCredentialForm(btn, svc) {
        const card = btn.closest('[data-card-svc]');
        if (!card) return;
        // Swap button for a mini-form
        btn.replaceWith((() => {
            const wrap = document.createElement('div');
            wrap.className = 'conn-card-form';
            wrap.innerHTML = `<input class="conn-card-input" type="text" placeholder="Client ID" autocomplete="off" data-cred-field="client_id">
<input class="conn-card-input" type="password" placeholder="Client Secret" autocomplete="off" data-cred-field="client_secret">
<button class="conn-card-btn conn-card-btn--save" type="button" data-oauth-save="${escapeAttr(svc)}">save</button>`;
            return wrap;
        })());
        card.querySelector('[data-cred-field="client_id"]')?.focus();
    },

    /** POST credential form values to vault, then refresh connections panel. */
    async _saveCredentials(btn, svc) {
        const card = btn.closest('[data-card-svc]');
        if (!card) return;
        const clientId = (card.querySelector('[data-cred-field="client_id"]')?.value || '').trim();
        const clientSecret = (card.querySelector('[data-cred-field="client_secret"]')?.value || '').trim();
        if (!clientId || !clientSecret) {
            this.showToast('Both Client ID and Client Secret are required.', 'warn', 2600);
            return;
        }
        const token = sessionStorage.getItem('genome_session') || '';
        btn.disabled = true;
        btn.textContent = 'saving…';
        try {
            const res = await fetch(`/api/connectors/oauth/${encodeURIComponent(svc)}/credentials`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Genome-Auth': token } : {}) },
                body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                this.showToast(err.detail || `Failed to save credentials for ${svc}.`, 'warn', 3000);
                btn.disabled = false;
                btn.textContent = 'save';
                return;
            }
            // Credentials stored — go straight to OAuth popup
            await this._oauthConnectService(svc);
        } catch {
            this.showToast(`Failed to save credentials for ${svc}.`, 'warn', 2600);
            btn.disabled = false;
            btn.textContent = 'save';
        }
    },

    /** Detect email domain protocol and start the right connect flow. */
    async _connectEmailFlow(emailAddr, ctaEl) {
        const domain = emailAddr.split('@')[1];
        try {
            const res = await fetch(`/api/connectors/email/domain?domain=${encodeURIComponent(domain)}`);
            const info = await res.json();
            if (info.protocol === 'oauth') {
                await this._oauthConnectService(info.provider);
            } else {
                this._showImapPasswordForm(emailAddr, ctaEl);
            }
        } catch {
            this.showToast('Could not detect email provider.', 'warn', 2400);
        }
    },

    /** Show password field for IMAP providers and submit to /api/connectors/imap/connect. */
    _showImapPasswordForm(emailAddr, ctaEl) {
        if (!ctaEl) return;
        ctaEl.innerHTML = `
            <div class="connector-cta-label">${escapeHtml(emailAddr)}</div>
            <input class="connector-email-input" type="password" placeholder="Password or app password" data-imap-password-input />
            <button class="connector-cta-btn" data-action="imap-connect" data-email="${escapeAttr(emailAddr)}">Connect</button>
            <div class="connector-cta-hint">Use an app-specific password if 2FA is enabled.</div>`;
    },

    /** Disconnect (remove vault token) for a service. */
    async _oauthDisconnectService(svc) {
        const token = sessionStorage.getItem('genome_session') || '';
        try {
            const headers = token ? { 'X-Genome-Auth': token } : {};
            const res = await fetch(`/api/auth/connections/${encodeURIComponent(svc)}`, { method: 'DELETE', headers });
            if (!res.ok) { this.showToast(`Could not disconnect ${svc}.`, 'warn', 2600); return; }
            this.showToast(`${svc} disconnected.`, 'ok', 2200);
            this.showConnectionsPanel();
        } catch {
            this.showToast(`Failed to disconnect ${svc}.`, 'warn', 2600);
        }
    },

    async rehydrateBootRuntime() {
        if (!this.state.session.sessionId) return;
        try {
            const [workspace, notifications] = await Promise.all([
                this._fetchWorkspaceState(),
                this._fetchNotificationsState(),
            ]);
            if (workspace && typeof workspace === 'object') {
                this.state.session.workspace = workspace;
            }
            if (Array.isArray(notifications)) {
                this.state.runtimeEvents = notifications.slice(-20);
            }
        } catch {
            // Boot should remain resilient even if runtime refresh is unavailable.
        }
        try {
            const [presenceRes, handoffRes] = await Promise.all([
                fetch(`/api/session/${encodeURIComponent(this.state.session.sessionId)}/presence`),
                fetch(`/api/session/${encodeURIComponent(this.state.session.sessionId)}/handoff/stats`),
            ]);
            if (presenceRes.ok) {
                this.state.session.presence = await presenceRes.json();
            }
            if (handoffRes.ok) {
                this.state.session.handoff = await handoffRes.json();
            }
        } catch {
            // Presence/handoff should never block shell startup.
        }
        this.saveState();
    },

    async startHandoff() {
        const out = await this.startSessionHandoff();
        if (!out) return;
        const pending = (out.handoff && typeof out.handoff.pending === 'object') ? out.handoff.pending : {};
        const paired = (out.pairedSurface && typeof out.pairedSurface === 'object') ? out.pairedSurface : ((pending.pairedSurface && typeof pending.pairedSurface === 'object') ? pending.pairedSurface : {});
        const link = this.buildHandoffShareUrl(out.token || pending.token, out.backendUrl || pending.backendUrl, out.bridgeUrl || pending.bridgeUrl);
        if (link && navigator?.clipboard?.writeText) {
            try { await navigator.clipboard.writeText(link); } catch { }
        }
        if (out.relayRouted || pending.relayRouted || paired.routed || paired.woke) {
            const target = String(paired.targetLabel || paired.targetSurfaceId || 'mobile').trim();
            this.showToast(`Genome takeover requested on ${target}.`, 'ok', 3200);
        } else {
            await this.showHandoffQr(link, pending.expiresAt || out.expiresAt);
            this.showToast('Genome phone surface QR ready.', 'ok', 3200);
        }
        this.updateStatus('HANDOFF_PENDING');
        this.refreshSurface();
        this.saveState();
    },

    async claimHandoffToken(token) {
        const sid = this.state.session.sessionId;
        if (!sid || !this.state.session.deviceId || !token) return;
        const out = await RemoteTurnService.handoffClaim(sid, this.state.session.deviceId, token);
        this.state.session.revision = Number(out.revision || this.state.session.revision);
        this.state.session.handoff = out.handoff || {
            activeDeviceId: out.activeDeviceId || this.state.session.deviceId,
            pending: null,
            lastClaimAt: out.claimedAt || Date.now()
        };
        this.updateStatus('HANDOFF_ACTIVE');
        this.refreshSurface();
        this.saveState();
    },

    parseHandoffIntent(text) {
        const value = String(text || '').trim();
        if (!value) return null;
        const normalized = value.toLowerCase().replace(/\s+/g, ' ').trim();
        if (/^start hand ?off$/i.test(value)) {
            return { mode: 'start' };
        }
        const claim = value.match(/^claim handoff\s+([a-z0-9-]+)$/i);
        if (claim) {
            return { mode: 'claim', token: claim[1] };
        }
        const targetPhone = /\b(phone|iphone|android|mobile|cell)\b/i.test(normalized);
        const targetTablet = /\b(ipad|tablet)\b/i.test(normalized);
        const targetDevice = /\b(device)\b/i.test(normalized);
        const handoffVerb = /\b(hand ?off|continue|switch|move|send|pick up)\b/i.test(normalized);
        const handoffPattern = /\b(hand ?off to|continue (this )?on|switch to|move to|send to|pick up on)\b/i.test(normalized);
        if ((targetPhone || targetTablet || targetDevice) && (handoffVerb || handoffPattern)) {
            return { mode: 'start', target: targetPhone ? 'phone' : (targetTablet ? 'tablet' : 'device') };
        }
        return null;
    },

    resolveSessionIdFromUrl() {
        const params = new URLSearchParams(window.location.search);
        const value = String(params.get('session') || '').trim();
        return value ? value.replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 32) : '';
    },

    writeSessionIdToUrl(sessionId) {
        const url = new URL(window.location.href);
        url.searchParams.set('session', sessionId);
        window.history.replaceState({}, '', url);
    },

    setConnectivity(online, reason = '') {
        const next = Boolean(online);
        const prev = Boolean(this.state.session.networkOnline);
        this.state.session.networkOnline = next;
        document.body.classList.toggle('network-offline', !next);
        if (this.offlineOverlay) {
            this.offlineOverlay.classList.toggle('visible', !next);
        }
        if (next) {
            this.state.session.reconnectAttempts = 0;
        }
        if (prev !== next && !next && reason) {
            this.showToast('Connection lost. Trying to reconnect.', 'warn', 1800);
        }
    },

    resetReconnectBackoff() {
        this._wsReconnectDelay = WS_RECONNECT_MIN_MS;
        this.state.session.reconnectAttempts = 0;
        if (this._wsReconnectTimer) {
            clearTimeout(this._wsReconnectTimer);
            this._wsReconnectTimer = null;
        }
    },

    scheduleWebSocketReconnect(source = 'ws') {
        if (!this.state.session.sessionId) return;
        if (this._wsReconnectTimer) return;
        if (this.state.session.syncTransport === 'ws') return;
        const currentDelay = Math.max(WS_RECONNECT_MIN_MS, Number(this._wsReconnectDelay || WS_RECONNECT_MIN_MS));
        const jitter = Math.floor(Math.random() * Math.max(1, Math.round(currentDelay * 0.2)));
        const delay = Math.min(WS_RECONNECT_MAX_MS, currentDelay + jitter);
        this.state.session.reconnectAttempts = Number(this.state.session.reconnectAttempts || 0) + 1;
        this._wsReconnectTimer = setTimeout(() => {
            this._wsReconnectTimer = null;
            if (!this.state.session.networkOnline && source !== 'online') {
                this.scheduleWebSocketReconnect(source);
                return;
            }
            this.openWebSocketSync();
        }, delay);
        this._wsReconnectDelay = Math.min(WS_RECONNECT_MAX_MS, Math.round(currentDelay * WS_RECONNECT_FACTOR));
    },

    handleTransportFailure(source, error) {
        const isNetworkError = !error || error?.name === 'TypeError' || /network|failed to fetch|fetch failed/i.test(String(error?.message || ''));
        if (isNetworkError) {
            this.setConnectivity(false, String(source || 'transport'));
        }
        if (this.state.session.syncTransport === 'ws') {
            this.state.session.syncTransport = 'sse';
            this.openSessionStream();
        } else if (this.state.session.syncTransport !== 'sse') {
            this.state.session.syncTransport = 'poll';
        }
        this.scheduleWebSocketReconnect(String(source || 'transport'));
        this.updateStatus(this.state.session.statusMode || 'DEGRADED');
    },

    startRealtimeSync() {
        this.resetReconnectBackoff();
        this.openWebSocketSync();
        this.startPresenceHeartbeat();
        if (this._syncTimer) clearInterval(this._syncTimer);
        this._syncTimer = setInterval(() => {
            if (this.state.session.syncTransport === 'ws' || this.state.session.syncTransport === 'sse') return;
            this.pollSession().catch((error) => this.handleTransportFailure('poll', error));
        }, FALLBACK_POLL_MS);
    },

    openWebSocketSync() {
        const sessionId = this.state.session.sessionId;
        if (!sessionId || typeof window.WebSocket === 'undefined') {
            this.openSessionStream();
            return;
        }

        if (this._ws) {
            this._ws.close();
            this._ws = null;
        }

        const wsUrl = `ws://${location.host}/ws`;
        const ws = new WebSocket(wsUrl);
        this._ws = ws;

        ws.onopen = () => {
            // Send auth as first message — never embed tokens in the URL (logs/history)
            const authToken = sessionStorage.getItem('genome_session') || '';
            ws.send(JSON.stringify({ type: 'auth', token: authToken, sessionId }));
            this.setConnectivity(true);
            this.state.session.syncTransport = 'ws';
            this.resetReconnectBackoff();
            this.updateStatus(this.state.session.statusMode || 'SYNCED');
        };

        ws.onmessage = (event) => {
            try {
                const payload = JSON.parse(event.data);
                if (payload.type === 'auth_required') {
                    sessionStorage.removeItem('genome_session');
                    this.ensureAuth().then(() => this.openWebSocketSync());
                    return;
                }
                if (payload.type === 'notification') {
                    const { title = '', body = '', route = '' } = payload;
                    // In-app toast
                    this.showToast(`${title}${body ? ': ' + body : ''}`, 'info', 5000);
                    // OS-level desktop notification (Electron or Web Notifications API)
                    if (window.electronAPI?.notify) {
                        window.electronAPI.notify({ title, body, route });
                    } else if (Notification?.permission === 'granted') {
                        const n = new Notification(title, { body, silent: false });
                        if (route) n.onclick = () => { n.close(); this.submitTurn(route); };
                    }
                    return;
                }
                this.applyRemoteSync(payload);
                this.setConnectivity(true);
            } catch {
                this.state.session.syncTransport = 'poll';
            }
        };

        ws.onerror = (error) => {
            this.handleTransportFailure('ws', error);
        };

        ws.onclose = () => {
            if (this._ws === ws) this._ws = null;
            if (this.state.session.syncTransport !== 'ws') return;
            this.handleTransportFailure('ws_close');
        };
    },

    openSessionStream() {
        const sessionId = this.state.session.sessionId;
        if (!sessionId || typeof window.EventSource === 'undefined') {
            this.state.session.syncTransport = 'poll';
            return;
        }

        if (this._eventSource) {
            this._eventSource.close();
            this._eventSource = null;
        }

        const es = new EventSource(`/api/stream?sessionId=${encodeURIComponent(sessionId)}`);
        this._eventSource = es;

        es.onopen = () => {
            if (this.state.session.syncTransport !== 'ws') {
                this.state.session.syncTransport = 'sse';
            }
            this.setConnectivity(true);
            this.updateStatus(this.state.session.statusMode || 'SYNCED');
        };

        es.onmessage = (event) => {
            try {
                const payload = JSON.parse(event.data);
                this.applyRemoteSync(payload);
                this.setConnectivity(true);
            } catch {
                this.state.session.syncTransport = 'poll';
            }
        };

        es.onerror = (error) => {
            if (this.state.session.syncTransport !== 'ws') {
                this.state.session.syncTransport = 'poll';
            }
            if (this._eventSource) {
                this._eventSource.close();
                this._eventSource = null;
            }
            this.handleTransportFailure('sse', error);
        };
    },

    async pollSession() {
        const sessionId = this.state.session.sessionId;
        if (!sessionId || this.state.session.isApplyingLocalTurn) return;

        const snapshot = await RemoteTurnService.getSession(sessionId);
        this.setConnectivity(true);
        const serverRevision = Number(snapshot.revision || 0);
        if (serverRevision <= this.state.session.revision) return;

        this.state.session.revision = serverRevision;
        this.state.memory = snapshot.memory || this.state.memory;
        this.state.session.handoff = snapshot.handoff || this.state.session.handoff;
        this.state.session.presence = snapshot.presence || this.state.session.presence;
        this.state.session.workspace = snapshot.workspace || this.state.session.workspace;
        this.mergeRuntimeEvents(snapshot.notifications, { replace: true });
        if (snapshot.lastTurn?.envelope && snapshot.lastTurn?.plan && this._userHasActed) {
            const plan = UIPlanSchema.normalize(snapshot.lastTurn.plan);
            this.state.session.lastExecution = snapshot.lastTurn.execution || this.state.session.lastExecution;
            this.state.session.lastKernelTrace = snapshot.lastTurn.kernelTrace || this.deriveKernelTrace(snapshot.lastTurn.execution, snapshot.lastTurn.route);
            this.render(plan, snapshot.lastTurn.envelope, this.state.session.lastKernelTrace);
            this.updateStatus(`SYNCED:${snapshot.lastTurn.planner || 'REMOTE'}`);
        }
        this.saveState();
    },

    applyBackgroundEvents(events) {
        const incoming = Array.isArray(events) ? events : [];
        if (!incoming.length) return;
        for (const item of incoming) {
            if (!item || typeof item !== 'object') continue;
            const createdAt = Number(item.createdAt || Date.now());
            const title = String(item.title || 'Background event').trim();
            const message = String(item.message || '').trim();
            this.mergeRuntimeEvents([{
                id: String(item.jobId || `${item.type || 'event'}:${createdAt}`),
                type: String(item.type || 'event'),
                ts: createdAt,
                title,
                message,
                severity: String(item.severity || 'info'),
                route: String(item.intent || ''),
                read: false,
                source: String(item.source || 'runtime'),
            }]);
            const eventType = String(item.type || 'event');
            if (eventType === 'continuity_alert') {
                this.showToast(`${message || title}`, 'warn', 5000);
                if (this.status) {
                    this.status.dataset.alert = 'continuity';
                }
            } else {
                this.showToast(`${title}: ${message || 'updated'}`, 'info', 2600);
                // OS-level notification for reminders and relay messages
                if (['reminder', 'alarm', 'message', 'relay_message'].includes(eventType)) {
                    const route = eventType.includes('message') ? 'show my messages' : String(item.intent || '').trim();
                    this.osNotify(title, message || 'GenomeUI', route);
                }
            }
        }
    },

    mergeRuntimeEvents(events, { replace = false } = {}) {
        const incoming = Array.isArray(events) ? events : [];
        const prior = replace ? [] : (Array.isArray(this.state.runtimeEvents) ? this.state.runtimeEvents : []);
        const seen = new Set();
        const next = [];
        for (const raw of [...prior, ...incoming]) {
            if (!raw || typeof raw !== 'object') continue;
            const ts = Number(raw.ts || raw.createdAt || Date.now());
            const title = String(raw.title || 'Notification').trim() || 'Notification';
            const id = String(raw.id || `${raw.type || 'event'}:${ts}`).trim() || `${raw.type || 'event'}:${ts}`;
            const key = `${id}:${ts}`;
            if (seen.has(key)) continue;
            seen.add(key);
            next.push({
                id,
                type: String(raw.type || 'event').trim() || 'event',
                ts,
                title,
                message: String(raw.message || '').trim(),
                severity: String(raw.severity || 'info').trim() || 'info',
                route: String(raw.route || raw.intent || '').trim(),
                read: Boolean(raw.read),
                source: String(raw.source || 'runtime').trim() || 'runtime',
            });
        }
        next.sort((a, b) => Number(a.ts || 0) - Number(b.ts || 0));
        this.state.runtimeEvents = next.slice(-20);
    },

    applyRemoteSync(payload) {
        if (!payload || this.state.session.isApplyingLocalTurn) return;
        if (payload.type === 'phone_state_update') {
            document.dispatchEvent(new CustomEvent('phoneStateUpdate', { detail: payload }));
            return;
        }
        if (payload.type === 'continuity_alert') {
            const msg = String(payload.message || 'Continuity alert').slice(0, 120);
            this.showToast(msg, 'warn', 5000);
            if (this.status) this.status.dataset.alert = 'continuity';
            return;
        }
        const revision = Number(payload.revision || 0);
        if (revision <= this.state.session.revision) return;

        const priorHandoff = JSON.stringify(this.state.session.handoff || {});
        this.state.session.revision = revision;
        this.state.memory = payload.memory || this.state.memory;
        this.state.session.handoff = payload.handoff || this.state.session.handoff;
        this.state.session.presence = payload.presence || this.state.session.presence;
        this.state.session.workspace = payload.workspace || this.state.session.workspace;
        this.mergeRuntimeEvents(payload.notifications, { replace: true });
        this.applyBackgroundEvents(payload.backgroundEvents);
        if (payload.lastTurn?.envelope && payload.lastTurn?.plan && this._userHasActed) {
            const plan = UIPlanSchema.normalize(payload.lastTurn.plan);
            this.state.session.lastExecution = payload.lastTurn.execution || this.state.session.lastExecution;
            this.state.session.lastKernelTrace = payload.lastTurn.kernelTrace || this.deriveKernelTrace(payload.lastTurn.execution, payload.lastTurn.route);
            this.render(plan, payload.lastTurn.envelope, this.state.session.lastKernelTrace);
            this.updateStatus(`SYNCED:${payload.lastTurn.planner || 'REMOTE'}`);
        } else if (this._userHasActed && priorHandoff !== JSON.stringify(this.state.session.handoff || {})) {
            this.refreshSurface();
            this.updateStatus('SYNCED:HANDOFF');
        }
        this.saveState();
    },

    generateLocalSessionId() {
        return `local-${Math.random().toString(36).slice(2, 10)}`;
    },

    bindEvents() {
        this.input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                const text = this.input.value.trim();
                this.input.value = '';
                this.handleIntent(text);
                return;
            }

            if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
                event.preventDefault();
                this.recallIntent(event.key === 'ArrowUp' ? -1 : 1);
            }
        });

        document.addEventListener('keydown', (event) => {
            const isMac = /mac/i.test(String(navigator.platform || ''));
            const accel = isMac ? event.metaKey : event.ctrlKey;
            if (accel && String(event.key || '').toLowerCase() === 'k') {
                event.preventDefault();
                this.input.focus();
                this.input.select();
                return;
            }

            if (accel && String(event.key || '').toLowerCase() === 'n') {
                event.preventDefault();
                window.electronAPI?.newWindow?.(window.location.href);
                return;
            }

            // Shift variants must be checked BEFORE plain Alt variants
            if (event.altKey && event.shiftKey && event.key === 'ArrowLeft') {
                event.preventDefault();
                this.stepSceneDomain(-1);
                return;
            }

            if (event.altKey && event.shiftKey && event.key === 'ArrowRight') {
                event.preventDefault();
                this.stepSceneDomain(1);
                return;
            }

            if (event.altKey && event.key === 'ArrowLeft') {
                event.preventDefault();
                this.stepHistory(-1);
                return;
            }

            if (event.altKey && event.key === 'ArrowRight') {
                event.preventDefault();
                this.stepHistory(1);
                return;
            }

            if (event.altKey && /^[1-9]$/.test(String(event.key || ''))) {
                event.preventDefault();
                const idx = Math.max(0, Number(event.key) - 1);
                this.executeQuickCommand(idx);
                return;
            }

            if (event.altKey && String(event.key || '').toLowerCase() === 'm') {
                if (this.container.querySelector('.scene-webdeck')) {
                    event.preventDefault();
                    this.toggleWebdeckMode();
                    return;
                }
            }
            if (event.altKey && String(event.key || '').toLowerCase() === 's') {
                event.preventDefault();
                const soundBtn = document.getElementById('sound-toggle');
                soundBtn?.click();
                return;
            }

            const active = document.activeElement;
            const isEditable = active && (
                active.tagName === 'INPUT'
                || active.tagName === 'TEXTAREA'
                || active.isContentEditable
            );

            if (event.key === 'Escape') {
                this.toggleHelp(false);
                return;
            }

            // '?' only toggles help when NOT typing in a field; F1 always works
            if (event.key === 'F1' || (event.key === '?' && !isEditable)) {
                event.preventDefault();
                this.toggleHelp();
                return;
            }

            if (event.key !== '/') return;
            if (isEditable) return;
            event.preventDefault();
            this.input.focus();
        });

        this.input.addEventListener('focus', () => {
            this.inputContainer.classList.add('active-intent');
        });

        this.input.addEventListener('blur', () => {
            this.inputContainer.classList.remove('active-intent');
        });

        this.container.addEventListener('click', async (event) => {
            const webdeckModeBtn = event.target.closest('[data-webdeck-mode-toggle]');
            if (webdeckModeBtn) {
                this.toggleWebdeckMode();
                return;
            }

            const sceneButton = event.target.closest('[data-scene-domain]');
            if (sceneButton) {
                const domain = String(sceneButton.dataset.sceneDomain || '').trim().toLowerCase();
                this.markUserEngaged();
                if (domain) this.switchToSceneDomain(domain);
                return;
            }

            // ── OAuth connect button ──────────────────────────────────────────
            const connectBtn = event.target.closest('[data-oauth-connect]');
            if (connectBtn) {
                const svc = String(connectBtn.dataset.oauthConnect || '').trim();
                if (!svc) return;
                if (connectBtn.dataset.oauthNeedsCreds === '1') {
                    // No credentials yet — show inline form instead of going to OAuth
                    this._showCredentialForm(connectBtn, svc);
                } else {
                    this._oauthConnectService(svc);
                }
                return;
            }

            // ── Universal email connect button ────────────────────────────────
            if (event.target.closest('[data-action="email-connect"]')) {
                const cta = event.target.closest('.connector-cta');
                const input = cta?.querySelector('[data-email-connect-input]');
                const emailAddr = (input?.value || '').trim().toLowerCase();
                if (!emailAddr || !emailAddr.includes('@')) {
                    this.showToast('Enter a valid email address.', 'warn', 2400);
                    return;
                }
                this._connectEmailFlow(emailAddr, cta);
                return;
            }

            // ── IMAP password submit ──────────────────────────────────────────
            if (event.target.closest('[data-action="imap-connect"]')) {
                const btn = event.target.closest('[data-action="imap-connect"]');
                const cta = btn.closest('.connector-cta');
                const emailAddr = btn.dataset.email || '';
                const pwInput = cta?.querySelector('[data-imap-password-input]');
                const password = pwInput?.value || '';
                if (!password) { this.showToast('Enter a password.', 'warn', 2000); return; }
                btn.disabled = true;
                btn.textContent = 'Connecting…';
                try {
                    const res = await fetch('/api/connectors/imap/connect', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ email: emailAddr, password }),
                    });
                    const data = await res.json();
                    if (data.ok) {
                        this.showToast(`${emailAddr} connected.`, 'ok', 2600);
                        this.handleIntent('check my email');
                    } else {
                        this.showToast(data.error || 'Connection failed.', 'warn', 3000);
                        btn.disabled = false;
                        btn.textContent = 'Connect';
                    }
                } catch {
                    this.showToast('Connection failed.', 'warn', 2400);
                    btn.disabled = false;
                    btn.textContent = 'Connect';
                }
                return;
            }

            // ── OAuth disconnect button ───────────────────────────────────────
            const disconnectBtn = event.target.closest('[data-oauth-disconnect]');
            if (disconnectBtn) {
                const svc = String(disconnectBtn.dataset.oauthDisconnect || '').trim();
                if (svc) this._oauthDisconnectService(svc);
                return;
            }

            const markNotificationsBtn = event.target.closest('[data-notifications-mark-read]');
            if (markNotificationsBtn) {
                const app = String(markNotificationsBtn.dataset.notificationsMarkRead || '').trim();
                const ok = await this.markNotificationsRead(app);
                if (!ok) this.showToast('Could not update notifications.', 'warn', 2200);
                return;
            }

            const clearNotificationsBtn = event.target.closest('[data-notifications-clear]');
            if (clearNotificationsBtn) {
                const app = String(clearNotificationsBtn.dataset.notificationsClear || '').trim();
                const ok = await this.clearNotifications(app);
                if (!ok) this.showToast('Could not clear notifications.', 'warn', 2200);
                return;
            }

            const grantBtn = event.target.closest('[data-connector-grant]');
            if (grantBtn) {
                const scope = String(grantBtn.dataset.connectorGrant || '').trim();
                const ok = await this.setConnectorGrant(scope, true);
                if (!ok) this.showToast('Could not grant connector scope.', 'warn', 2200);
                return;
            }

            const revokeGrantBtn = event.target.closest('[data-connector-revoke]');
            if (revokeGrantBtn) {
                const scope = String(revokeGrantBtn.dataset.connectorRevoke || '').trim();
                const ok = await this.setConnectorGrant(scope, false);
                if (!ok) this.showToast('Could not revoke connector scope.', 'warn', 2200);
                return;
            }

            const inspectConnectorBtn = event.target.closest('[data-connector-inspect]');
            if (inspectConnectorBtn) {
                const service = String(inspectConnectorBtn.dataset.connectorInspect || '').trim().toLowerCase();
                if (!service) return;
                const ok = await this.showConnectionsPanel(service);
                if (!ok) this.showToast('Could not inspect connector.', 'warn', 2200);
                return;
            }

            const liveSurfaceBtn = event.target.closest('[data-live-surface]');
            if (liveSurfaceBtn) {
                const surface = String(liveSurfaceBtn.dataset.liveSurface || '').trim();
                const driveQuery = String(liveSurfaceBtn.dataset.driveQuery || '').trim();
                if (surface === 'email') await this.showEmailInbox();
                else if (surface === 'calendar') await this.showCalendarAgenda();
                else if (surface === 'messaging') await this.showMessagingInbox();
                else if (surface === 'drive') await this.showDriveFiles(driveQuery);
                else if (surface === 'files') await this.showWorkspaceFiles();
                else if (surface === 'content') await this.showContentRepository();
                else if (surface === 'notifications') await this.showNotificationsInbox();
                else if (surface === 'music') this.handleIntent('play my top songs');
                return;
            }

            const continuityClearBtn = event.target.closest('[data-continuity-clear]');
            if (continuityClearBtn) {
                const ok = await this.clearContinuityAlerts();
                if (!ok) this.showToast('Could not clear continuity alerts.', 'warn', 2200);
                return;
            }

            const continuityDrillBtn = event.target.closest('[data-continuity-drill]');
            if (continuityDrillBtn) {
                const ok = await this.drillContinuityAlert();
                if (!ok) this.showToast('Could not drill continuity alert.', 'warn', 2200);
                return;
            }

            const continuityPruneBtn = event.target.closest('[data-continuity-prune]');
            if (continuityPruneBtn) {
                const all = continuityPruneBtn.dataset.continuityPrune === 'all';
                const ok = await this.prunePresence(all);
                if (!ok) this.showToast('Could not prune presence.', 'warn', 2200);
                return;
            }

            const runtimeRefreshBtn = event.target.closest('[data-runtime-refresh]');
            if (runtimeRefreshBtn) {
                const ok = await this.showRuntimeHealth();
                if (!ok) this.showToast('Could not refresh runtime health.', 'warn', 2200);
                return;
            }

            const autopilotToggleBtn = event.target.closest('[data-continuity-autopilot]');
            if (autopilotToggleBtn) {
                const enabled = autopilotToggleBtn.dataset.continuityAutopilot === 'on';
                const ok = await this.setContinuityAutopilot(enabled);
                if (!ok) this.showToast('Could not update continuity autopilot.', 'warn', 2200);
                return;
            }

            const autopilotTickBtn = event.target.closest('[data-continuity-autopilot-tick]');
            if (autopilotTickBtn) {
                const ok = await this.tickContinuityAutopilot();
                if (!ok) this.showToast('Could not run continuity autopilot.', 'warn', 2200);
                return;
            }

            const autopilotModeBtn = event.target.closest('[data-continuity-autopilot-mode]');
            if (autopilotModeBtn) {
                const mode = String(autopilotModeBtn.dataset.continuityAutopilotMode || '').trim().toLowerCase();
                const ok = mode && mode !== 'recommended'
                    ? await this.configureContinuityAutopilot({ mode })
                    : await this.applyRecommendedContinuityMode();
                if (!ok) this.showToast('Could not apply recommended continuity mode.', 'warn', 2200);
                return;
            }

            const continuityNextBtn = event.target.closest('[data-continuity-next-apply]');
            if (continuityNextBtn) {
                const ok = await this.applyContinuityNextAction();
                if (!ok) this.showToast('Could not apply the next continuity action.', 'warn', 2200);
                return;
            }

            const handoffStartBtn = event.target.closest('[data-handoff-start]');
            if (handoffStartBtn) {
                const report = await this.startSessionHandoff(String(handoffStartBtn.dataset.handoffSurface || '').trim());
                if (!report?.ok) {
                    this.showToast('Could not start handoff.', 'warn', 2200);
                    return;
                }
                const pending = (report.handoff && typeof report.handoff.pending === 'object') ? report.handoff.pending : {};
                const paired = (report.pairedSurface && typeof report.pairedSurface === 'object') ? report.pairedSurface : ((pending.pairedSurface && typeof pending.pairedSurface === 'object') ? pending.pairedSurface : {});
                const token = String(report.token || pending.token || '').trim();
                const shareUrl = this.buildHandoffShareUrl(token, report.backendUrl || pending.backendUrl, report.bridgeUrl || pending.bridgeUrl);
                if (shareUrl) {
                    await this.copyTextToClipboard(shareUrl);
                    if (report.relayRouted || pending.relayRouted || paired.routed || paired.woke) {
                        const target = String(paired.targetLabel || paired.targetSurfaceId || 'mobile').trim();
                        this.showToast(`Genome takeover requested on ${target}.`, 'ok', 2400);
                    } else {
                        await this.showHandoffQr(shareUrl, pending.expiresAt || report.expiresAt);
                        this.showToast('Genome phone surface QR ready.', 'ok', 2400);
                    }
                } else {
                    this.showToast('Handoff started.', 'ok', 2200);
                }
                await this.showContinuitySurface();
                return;
            }
            const preferSurfaceBtn = event.target.closest('[data-surface-prefer]');
            if (preferSurfaceBtn) {
                const surfaceId = String(preferSurfaceBtn.dataset.surfacePrefer || '').trim();
                const ok = await this.preferPairedSurface(surfaceId);
                this.showToast(ok ? 'Preferred surface updated.' : 'Could not update preferred surface.', ok ? 'ok' : 'warn', 2200);
                if (ok) await this.showContinuitySurface();
                return;
            }

            const handoffCopyBtn = event.target.closest('[data-handoff-copy]');
            if (handoffCopyBtn) {
                const token = String(handoffCopyBtn.dataset.handoffCopy || '').trim();
                const shareUrl = this.buildHandoffShareUrl(token, handoffCopyBtn.dataset.handoffBackend || '', handoffCopyBtn.dataset.handoffBridge || '');
                const ok = await this.copyTextToClipboard(shareUrl || token);
                if (!ok) this.showToast('Could not copy handoff token.', 'warn', 2200);
                else this.showToast('Handoff link copied.', 'ok', 2200);
                return;
            }

            const handoffQrBtn = event.target.closest('[data-handoff-qr]');
            if (handoffQrBtn) {
                const token = String(handoffQrBtn.dataset.handoffQr || '').trim();
                const backendUrl = String(handoffQrBtn.dataset.handoffBackend || '').trim();
                const shareUrl = this.buildHandoffShareUrl(token, backendUrl, handoffQrBtn.dataset.handoffBridge || '');
                if (!shareUrl) {
                    this.showToast('No pending handoff QR available.', 'warn', 2200);
                    return;
                }
                await this.showHandoffQr(shareUrl, handoffQrBtn.dataset.handoffExpires || 0);
                return;
            }

            const autopilotAlignBtn = event.target.closest('[data-continuity-autopilot-align]');
            if (autopilotAlignBtn) {
                const enabled = autopilotAlignBtn.dataset.continuityAutopilotAlign === 'on';
                const ok = await this.configureContinuityAutopilot({ autoAlignMode: enabled });
                if (!ok) this.showToast('Could not update continuity auto-align.', 'warn', 2200);
                return;
            }

            const autopilotResetBtn = event.target.closest('[data-continuity-autopilot-reset]');
            if (autopilotResetBtn) {
                const clearHistory = autopilotResetBtn.dataset.continuityAutopilotReset === 'history';
                const ok = await this.resetContinuityAutopilot(clearHistory);
                if (!ok) this.showToast('Could not reset continuity autopilot.', 'warn', 2200);
                return;
            }

            const postureApplyBtn = event.target.closest('[data-continuity-posture-apply]');
            if (postureApplyBtn) {
                const index = Math.max(1, Number(postureApplyBtn.dataset.continuityPostureApply || 1));
                const ok = await this.applyContinuityPostureAction(index);
                if (!ok) this.showToast('Could not apply posture action.', 'warn', 2200);
                return;
            }

            const postureBatchBtn = event.target.closest('[data-continuity-posture-batch]');
            if (postureBatchBtn) {
                const limit = Math.max(1, Math.min(10, Number(postureBatchBtn.dataset.continuityPostureBatch || 3)));
                const ok = await this.applyContinuityPostureBatch(limit);
                if (!ok) this.showToast('Could not apply posture batch.', 'warn', 2200);
                return;
            }

            // ── OAuth save-credentials button ─────────────────────────────────
            const saveBtn = event.target.closest('[data-oauth-save]');
            if (saveBtn) {
                const svc = String(saveBtn.dataset.oauthSave || '').trim();
                if (svc) this._saveCredentials(saveBtn, svc);
                return;
            }

            const repoBranchBtn = event.target.closest('[data-repo-branch]');
            if (repoBranchBtn) {
                const domain = String(repoBranchBtn.dataset.repoDomain || '').trim();
                const name = String(repoBranchBtn.dataset.repoName || '').trim();
                const currentBranch = String(repoBranchBtn.dataset.repoBranch || this._contentBranchFor(domain, name)).trim();
                const nextBranch = String(window.prompt(`Create or switch branch for ${name || domain}`, currentBranch === 'main' ? 'draft' : currentBranch) || '').trim();
                if (!nextBranch) return;
                const item = await this.createRepoBranch(domain, name, nextBranch);
                if (!item) {
                    this.showToast('Branch action failed.', 'warn', 2400);
                    return;
                }
                this.showToast(`Branch ready: ${nextBranch}`, 'ok', 2200);
                if (domain && name) {
                    this.openRepoObject(domain, name, nextBranch);
                }
                return;
            }

            const repoHistoryBtn = event.target.closest('[data-repo-history]');
            if (repoHistoryBtn) {
                const domain = String(repoHistoryBtn.dataset.repoDomain || '').trim();
                const name = String(repoHistoryBtn.dataset.repoName || '').trim();
                if (domain && name) {
                    this.showRepoHistory(domain, name);
                }
                return;
            }

            const repoMergeBtn = event.target.closest('[data-repo-merge]');
            if (repoMergeBtn) {
                const domain = String(repoMergeBtn.dataset.repoDomain || '').trim();
                const name = String(repoMergeBtn.dataset.repoName || '').trim();
                const sourceBranch = String(repoMergeBtn.dataset.repoMerge || '').trim();
                const targetBranch = String(repoMergeBtn.dataset.repoTargetBranch || this._contentBranchFor(domain, name)).trim();
                if (domain && name && sourceBranch && targetBranch && sourceBranch !== targetBranch) {
                    const item = await this.mergeRepoBranch(domain, name, sourceBranch, targetBranch);
                    if (!item) {
                        this.showToast('Merge failed.', 'warn', 2400);
                        return;
                    }
                    this.showToast(`Merged ${sourceBranch} into ${targetBranch}.`, 'ok', 2200);
                    if (String(this.state.activeSurface?.domain || '') === domain && String(this.state.activeSurface?.name || '') === name) {
                        await this.openRepoObject(domain, name, targetBranch);
                    } else {
                        await this.showRepoHistory(domain, name);
                    }
                }
                return;
            }

            const repoWorktreesBtn = event.target.closest('[data-repo-worktrees]');
            if (repoWorktreesBtn) {
                this.markUserEngaged();
                this.showWorkspaceWorktrees();
                return;
            }

            const worktreeActivateBtn = event.target.closest('[data-worktree-activate]');
            if (worktreeActivateBtn) {
                const itemId = String(worktreeActivateBtn.dataset.worktreeActivate || '').trim();
                const domain = String(worktreeActivateBtn.dataset.worktreeDomain || '').trim();
                const name = String(worktreeActivateBtn.dataset.worktreeName || '').trim();
                const branch = String(worktreeActivateBtn.dataset.worktreeBranch || '').trim();
                const item = await this.activateWorkspaceWorktree(itemId);
                if (!item) {
                    this.showToast('Could not activate worktree.', 'warn', 2200);
                    return;
                }
                this.openRepoObject(domain || item.domain, name || item.name, branch || item.branch || this._contentBranchFor(domain || item.domain, name || item.name));
                return;
            }

            const worktreeDetachBtn = event.target.closest('[data-worktree-detach]');
            if (worktreeDetachBtn) {
                const itemId = String(worktreeDetachBtn.dataset.worktreeDetach || '').trim();
                const ok = await this.detachWorkspaceWorktree(itemId);
                if (!ok) {
                    this.showToast('Could not detach worktree.', 'warn', 2200);
                    return;
                }
                this.showToast('Worktree detached.', 'ok', 2000);
                this.showWorkspaceWorktrees();
                return;
            }

            const worktreeAttachBtn = event.target.closest('[data-worktree-attach]');
            if (worktreeAttachBtn) {
                const domain = String(worktreeAttachBtn.dataset.worktreeDomain || '').trim();
                const name = String(worktreeAttachBtn.dataset.worktreeName || '').trim();
                const branch = String(worktreeAttachBtn.dataset.worktreeBranch || '').trim();
                const item = await this.attachWorkspaceWorktree(domain, name, branch);
                if (!item) {
                    this.showToast('Could not attach worktree.', 'warn', 2200);
                    return;
                }
                this.showToast('Worktree attached.', 'ok', 2000);
                this.showWorkspaceWorktrees();
                return;
            }

            const repoRevertBtn = event.target.closest('[data-repo-revert]');
            if (repoRevertBtn) {
                const domain = String(repoRevertBtn.dataset.repoDomain || '').trim();
                const name = String(repoRevertBtn.dataset.repoName || '').trim();
                const hash = String(repoRevertBtn.dataset.repoRevert || '').trim();
                if (!domain || !name || !hash) return;
                const ok = window.confirm(`Revert ${name} to ${hash.slice(0, 8)}?`);
                if (!ok) return;
                const item = await this.revertRepoObject(domain, name, hash);
                if (!item) {
                    this.showToast('Revert failed.', 'warn', 2400);
                    return;
                }
                this.showToast(`Reverted ${name} to ${hash.slice(0, 8)}`, 'ok', 2400);
                this.openRepoObject(domain, name, item.branch || this._contentBranchFor(domain, name));
                return;
            }

            const repoOpenBtn = event.target.closest('[data-repo-open]');
            if (repoOpenBtn) {
                const domain = String(repoOpenBtn.dataset.repoDomain || '').trim();
                const name = String(repoOpenBtn.dataset.repoName || '').trim();
                const branch = String(repoOpenBtn.dataset.repoBranch || this._contentBranchFor(domain, name)).trim();
                if (domain && name) {
                    this.markUserEngaged();
                    this.openRepoObject(domain, name, branch);
                }
                return;
            }

            const shellObjectBtn = event.target.closest('[data-shell-object-kind]');
            if (shellObjectBtn) {
                const kind = String(shellObjectBtn.dataset.shellObjectKind || '').trim();
                this.markUserEngaged();
                const ok = await this.openShellObject({
                    kind,
                    domain: String(shellObjectBtn.dataset.shellObjectDomain || '').trim(),
                    name: String(shellObjectBtn.dataset.shellObjectName || '').trim(),
                    branch: String(shellObjectBtn.dataset.shellObjectBranch || '').trim(),
                    itemId: String(shellObjectBtn.dataset.shellObjectItemId || '').trim(),
                    service: String(shellObjectBtn.dataset.shellObjectService || '').trim(),
                    scene: String(shellObjectBtn.dataset.shellObjectScene || '').trim(),
                });
                if (!ok) this.showToast('Could not open shell object.', 'warn', 2200);
                return;
            }
            const suggestion = event.target.closest('[data-command]');
            if (!suggestion) return;
            const command = suggestion.dataset.command;
            if (!command) return;
            const repoOpen = String(command).match(/^open\s+(document|spreadsheet|presentation)\s+(.+)$/i);
            if (repoOpen) {
                const [, domain, name] = repoOpen;
                this.openRepoObject(domain, name);
                return;
            }
            const shellHandled = await this.runDirectShellCommand(command);
            if (shellHandled) return;
            this.input.value = command;
            this.input.focus();
            this.handleIntent(command);
        });

        this.historyReel.addEventListener('click', (event) => {
            const node = event.target.closest('[data-history-index]');
            if (!node) return;
            this.markUserEngaged();
            this.restoreFromHistory(Number(node.dataset.historyIndex));
        });

        document.addEventListener('click', (event) => {
            if (this.historyReel.classList.contains('reel-visible') &&
                !this.historyReel.contains(event.target)) {
                this.hideHistoryReel();
            }
        });

        // Touch-first history swipe (mobile/tablet): horizontal swipe restores prior/next surface.
        this.container.addEventListener('pointerdown', (event) => {
            if (String(event.pointerType || '') !== 'touch') return;
            this._swipeStart = { x: Number(event.clientX || 0), y: Number(event.clientY || 0), ts: Date.now() };
        });
        this.container.addEventListener('pointerup', (event) => {
            const start = this._swipeStart;
            this._swipeStart = null;
            if (!start || String(event.pointerType || '') !== 'touch') return;
            const dx = Number(event.clientX || 0) - start.x;
            const dy = Number(event.clientY || 0) - start.y;
            const dt = Date.now() - Number(start.ts || 0);
            if (dt > 900) return;
            if (Math.abs(dx) < 80 || Math.abs(dx) < Math.abs(dy) * 1.2) return;
            if (start.y <= 120) {
                this.stepSceneDomain(dx > 0 ? -1 : 1);
                return;
            }
            this.navigateIntentHistory(dx > 0 ? -1 : 1);
        });

        this.container.addEventListener('touchstart', (event) => {
            const touch = event.touches && event.touches.length ? event.touches[0] : null;
            if (!touch) return;
            this._touchSwipeStart = { x: Number(touch.clientX || 0), y: Number(touch.clientY || 0), ts: Date.now() };
        }, { passive: true });
        this.container.addEventListener('touchend', (event) => {
            const start = this._touchSwipeStart;
            this._touchSwipeStart = null;
            const touch = event.changedTouches && event.changedTouches.length ? event.changedTouches[0] : null;
            if (!start || !touch) return;
            const dx = Number(touch.clientX || 0) - Number(start.x || 0);
            const dy = Number(touch.clientY || 0) - Number(start.y || 0);
            const dt = Date.now() - Number(start.ts || 0);
            const isHorizontal = Math.abs(dx) > Math.abs(dy) * 1.5;
            if (!isHorizontal || dt > 360 || Math.abs(dx) < 64) return;
            if (Number(start.y || 0) <= 120) {
                this.stepSceneDomain(dx > 0 ? -1 : 1);
                return;
            }
            this.navigateIntentHistory(dx > 0 ? -1 : 1);
        }, { passive: true });

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                this.sendPresenceHeartbeat(true).catch(() => { });
                if (!this._sceneAnimFrame && this.container.querySelector('.scene-canvas')) {
                    this.activateSceneGraphics();
                }
            } else {
                this.teardownSceneGraphics();
            }
        });

        window.addEventListener('resize', () => {
            this.updateShortcutHint();
            const el = document.getElementById('phone-suggestions');
            if (el) el.style.display = window.innerWidth > 600 ? 'none' : '';
        }, { passive: true });

        window.addEventListener('offline', () => {
            this.state.session.syncTransport = 'poll';
            this.setConnectivity(false, 'offline');
            if (this._ws) {
                this._ws.close();
                this._ws = null;
            }
            if (this._eventSource) {
                this._eventSource.close();
                this._eventSource = null;
            }
            this.updateStatus(this.state.session.statusMode || 'OFFLINE');
        });

        window.addEventListener('online', () => {
            this.setConnectivity(true);
            this.openWebSocketSync();
            this.pollSession().catch((error) => this.handleTransportFailure('online_probe', error));
        });

        document.addEventListener('click', (event) => {
            const target = event.target instanceof Element ? event.target : null;
            if (!target) return;
            const retry = target.closest('[data-offline-retry]');
            if (!retry) return;
            this.setConnectivity(true);
            this.openWebSocketSync();
            this.pollSession().catch((error) => this.handleTransportFailure('manual_retry', error));
        });

        window.addEventListener('beforeunload', () => {
            this.stopPresenceHeartbeat();
            if (this._wsReconnectTimer) clearTimeout(this._wsReconnectTimer);
            if (this._ws) this._ws.close();
            if (this._eventSource) this._eventSource.close();
        });
    },

    getDeviceLabel() {
        const platform = this.getDevicePlatform();
        if (platform === 'mobile') return 'Phone';
        if (platform === 'tablet') return 'Tablet';
        return 'Desktop';
    },

    getDevicePlatform() {
        const ua = String(navigator.userAgent || '').toLowerCase();
        if (/ipad|tablet/.test(ua)) return 'tablet';
        if (/mobi|android|iphone/.test(ua)) return 'mobile';
        return 'desktop';
    },

    startPresenceHeartbeat() {
        this.stopPresenceHeartbeat();
        this.sendPresenceHeartbeat(true).catch(() => { });
        this._presenceTimer = setInterval(() => {
            this.sendPresenceHeartbeat(false).catch(() => { });
        }, PRESENCE_HEARTBEAT_MS);
    },

    stopPresenceHeartbeat() {
        if (this._presenceTimer) {
            clearInterval(this._presenceTimer);
            this._presenceTimer = null;
        }
    },

    navigateIntentHistory(direction) {
        this.stepHistory(direction);
    },

    async sendPresenceHeartbeat(force = false) {
        const sid = this.state.session.sessionId;
        const did = this.state.session.deviceId;
        if (!sid || !did) return;
        if (!force && this.state.session.isApplyingLocalTurn) return;
        const payload = await RemoteTurnService.presenceHeartbeat(
            sid,
            did,
            this.getDeviceLabel(),
            this.getDevicePlatform()
        );
        this.state.session.presence = payload || this.state.session.presence;
        this.saveState();
    },

    toggleHelp(forceState) {
        if (!this.helpOverlay) return;
        const nextState = typeof forceState === 'boolean'
            ? forceState
            : !this.helpOverlay.classList.contains('visible');
        this.helpOverlay.classList.toggle('visible', nextState);
    },

    toggleWebdeckMode() {
        const current = String(this.state.webdeck?.mode || 'surface');
        this.state.webdeck.mode = current === 'full' ? 'surface' : 'full';
        this.saveState();
        if (this.state.session.lastPlan && this.state.session.lastEnvelope) {
            this.render(this.state.session.lastPlan, this.state.session.lastEnvelope, this.state.session.lastKernelTrace);
            this.showToast(`Webdeck mode: ${this.state.webdeck.mode}`, 'info', 1400);
        }
    },

    recallIntent(direction) {
        if (!this.state.intentHistory.length) return;
        if (this.state.intentHistoryIndex < 0) {
            this.state.intentHistoryIndex = this.state.intentHistory.length;
        }

        this.state.intentHistoryIndex = clamp(
            this.state.intentHistoryIndex + direction,
            0,
            this.state.intentHistory.length
        );

        if (this.state.intentHistoryIndex === this.state.intentHistory.length) {
            this.input.value = '';
            return;
        }

        const value = this.state.intentHistory[this.state.intentHistoryIndex] || '';
        this.input.value = value;
        this.input.setSelectionRange(value.length, value.length);
    },

    rememberIntent(text) {
        const value = String(text || '').trim();
        if (!value) return;
        const history = this.state.intentHistory.filter((item) => item !== value);
        history.push(value);
        this.state.intentHistory = history.slice(-60);
        this.state.intentHistoryIndex = this.state.intentHistory.length;
    },

    inferIntentContext(text) {
        const lower = String(text || '').toLowerCase();
        if (/shoe|outfit|puma|nike|adidas|shop|buy/.test(lower)) return 'shopping';
        if (/search web|website|url|browse|summarize website|news/.test(lower)) return 'research';
        if (/weather|location/.test(lower)) return 'weather';
        if (/task|note|expense|graph/.test(lower)) return 'workspace';
        return 'general';
    },

    contextFromLatest(core, execution) {
        const latest = this.latestToolResult(execution) || {};
        const op = String(latest.op || '').toLowerCase();
        const kind = String(core?.kind || '').toLowerCase();
        if (op.includes('shopping') || kind === 'shopping') return 'shopping';
        if (op === 'web_search') return 'research';
        if (op === 'fetch_url' || op === 'web_summarize') return 'browsing';
        if (op.includes('weather') || kind === 'weather') return 'weather';
        if (kind === 'webdeck') return 'browsing';
        return this.inferIntentContext(this.state.session.lastIntent || '');
    },

    clearToast() {
        if (!this.toast) return;
        if (this._toastTimer) clearTimeout(this._toastTimer);
        this._toastTimer = null;
        this.toast.className = '';
        this.toast.replaceChildren();
    },

    showToast(message, type = 'info', duration = 1800, action = null) {
        if (!this.toast) return;
        this.toast.className = `visible tone-${type}`;
        this.toast.replaceChildren();
        const text = document.createElement('span');
        text.className = 'ux-toast-text';
        text.textContent = String(message || 'Updated.');
        this.toast.appendChild(text);
        if (action && typeof action === 'object' && typeof action.onClick === 'function') {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'ux-toast-action';
            button.textContent = String(action.label || 'Open');
            button.addEventListener('click', async () => {
                try {
                    await action.onClick();
                } finally {
                    this.clearToast();
                }
            });
            this.toast.appendChild(button);
        }
        if (this._toastTimer) clearTimeout(this._toastTimer);
        this._toastTimer = setTimeout(() => this.clearToast(), duration);
    },

    async installUpdateNow(version = '') {
        if (!window.electronAPI?.installUpdateNow) {
            this.showToast('Update restart is only available in Electron.', 'warn', 2600);
            return;
        }
        try {
            const result = await window.electronAPI.installUpdateNow();
            if (result?.ok) {
                this.showToast(`Restarting to install ${version || 'the update'}...`, 'ok', 2400);
                return;
            }
        } catch (_) {
            // fall through to warning toast below
        }
        this.showToast('No downloaded update is ready to install yet.', 'warn', 2600);
    },

    async handleIntent(text) {
        if (!text) return;
        this.markUserEngaged(); // user is engaged — allow remote sync to render
        this.rememberIntent(text);
        if (window.electronAPI?.isElectron && typeof window.electronAPI?.setIntentContext === 'function') {
            window.electronAPI.setIntentContext(this.inferIntentContext(text));
        }
        const handoffIntent = this.parseHandoffIntent(text);
        if (handoffIntent) {
            try {
                if (handoffIntent.mode === 'start') {
                    await this.startHandoff();
                } else if (handoffIntent.mode === 'claim') {
                    await this.claimHandoffToken(handoffIntent.token);
                    this.showToast('Handoff claimed on this device.', 'ok', 2600);
                }
            } catch {
                this.showToast('Handoff operation failed.', 'warn', 2600);
            }
            return;
        }

        // ── History reel local intercept ───────────────────────────────────
        const lowerText = text.trim().toLowerCase();
        if (/\b(?:show|open|display)\b.*\bhist(?:ory)?\b|\bhist(?:ory)?\b.*\b(?:reel|log|view)\b/.test(lowerText)) {
            this.showHistoryReel();
            return;
        }
        if (/\b(?:hide|close|dismiss)\b.*\bhist(?:ory)?\b/.test(lowerText)) {
            this.hideHistoryReel();
            return;
        }

        const startTime = performance.now();
        this.state.session.lastIntent = text;
        this.state.session.isApplyingLocalTurn = true; // close race: block stale-lastTurn syncs from this moment
        this.status.innerText = 'INTERPRETING INTENT...';
        this.inputContainer.classList.add('active-intent');
        this.container.classList.add('refracting');

        // ── Semantic cache check ────────────────────────────────────────────
        const cached = SemanticCache.get(text);
        if (cached) {
            // Render immediately from cache, fire background refresh
            this._applyRemote(text, cached, startTime, 'cache');
            this.container.classList.remove('refracting');
            this.inputContainer.classList.remove('active-intent');
            this.state.session.isApplyingLocalTurn = false;
            // Background refresh — update cache silently, no re-render
            const activeSurface = this.state.activeSurface;
            const activeContent = activeSurface?.getData
                ? { domain: activeSurface.domain, name: activeSurface.name, data: activeSurface.getData() }
                : null;
            RemoteTurnService.process(
                text, this.state.session.sessionId, this.state.session.revision,
                this.state.session.deviceId, 'rebase_if_commutative',
                `${this.state.session.deviceId}:bg:${Date.now().toString(36)}`, activeContent
            ).then((r) => {
                const domain = r?.envelope?.uiIntent?.kind || '';
                if (domain) SemanticCache.set(text, domain, r);
            }).catch(() => {});
            return;
        }

        await this.showReasoning([
            'Parsing layered intent envelope...',
            'Executing state/tool operations...',
            'Generating schema-validated UI plan...'
        ]);
        // Reasoning overlay has hidden — ghost the existing scene while awaiting backend
        this.container.classList.add('turn-thinking');

        let envelope;
        let execution;
        let kernelTrace;
        let safePlan;
        let mergeInfo = null;
        let plannerSource = 'local';

        try {
            this.state.session.isApplyingLocalTurn = true;
            const activeSurface = this.state.activeSurface;
            const activeContent = activeSurface?.getData
                ? { domain: activeSurface.domain, name: activeSurface.name, data: activeSurface.getData() }
                : null;
            const remote = await RemoteTurnService.process(
                text,
                this.state.session.sessionId,
                this.state.session.revision,
                this.state.session.deviceId,
                'rebase_if_commutative',
                `${this.state.session.deviceId}:${Date.now().toString(36)}:${Math.random().toString(36).slice(2, 8)}`,
                activeContent
            );

            // ── High-risk confirm gate ──────────────────────────────────────
            if (remote.needsConfirm) {
                this.container.classList.remove('refracting', 'turn-thinking');
                this.inputContainer.classList.remove('active-intent');
                this.state.session.isApplyingLocalTurn = false;
                this._showConfirm(remote, text);
                return;
            }

            // ── Populate cache ──────────────────────────────────────────────
            const domain = remote.envelope?.uiIntent?.kind || '';
            if (domain) SemanticCache.set(text, domain, remote);

            console.log('[EMAIL] remote response op=', remote.execution?.op, 'plan.kind=', remote.plan?.kind, 'data=', remote.execution?.data);
            this.state.memory = remote.memory || this.state.memory;
            envelope = remote.envelope;
            execution = remote.execution;
            mergeInfo = remote.merge || null;
            kernelTrace = remote.kernelTrace || this.deriveKernelTrace(execution, remote.route);
            safePlan = UIPlanSchema.normalize(remote.plan);
            plannerSource = remote.planner || plannerSource;
            this.state.session.sessionId = remote.sessionId || this.state.session.sessionId;
            this.state.session.revision = Number(remote.revision || this.state.session.revision);
            this.state.session.presence = remote.presence || this.state.session.presence;
            this.writeSessionIdToUrl(this.state.session.sessionId);
            this.openWebSocketSync();
            const updatedContent = remote.execution?.data?.updatedContent;
            if (updatedContent !== undefined && this.state.activeSurface) {
                this._applyUpdatedContent(updatedContent);
            }
        } catch (error) {
            if (error?.kind === 'revision_conflict') {
                const snapshot = await RemoteTurnService.getSession(this.state.session.sessionId);
                this.state.session.revision = Number(snapshot.revision || this.state.session.revision);
                this.state.memory = snapshot.memory || this.state.memory;
                this.state.session.handoff = snapshot.handoff || this.state.session.handoff;
                this.state.session.presence = snapshot.presence || this.state.session.presence;
                if (snapshot.lastTurn?.envelope && snapshot.lastTurn?.plan) {
                    const plan = UIPlanSchema.normalize(snapshot.lastTurn.plan);
                    this.state.session.lastKernelTrace = snapshot.lastTurn.kernelTrace || this.deriveKernelTrace(snapshot.lastTurn.execution, snapshot.lastTurn.route);
                    this.render(plan, snapshot.lastTurn.envelope, this.state.session.lastKernelTrace);
                } else {
                    this.refreshSurface();
                }
                this.updateStatus('CONFLICT');
                this.showToast('Write blocked: session changed on another device. State refreshed.', 'warn', 3200);
                this.saveState();
                this.container.classList.remove('refracting', 'turn-thinking');
                this.container.classList.add('turn-error');
                setTimeout(() => this.container.classList.remove('turn-error'), 700);
                this.inputContainer.classList.remove('active-intent');
                SoundEngine.error();
                return;
            }
            if (error?.kind === 'permission_denied' && error?.detail?.execution && error?.detail?.plan && error?.detail?.envelope) {
                const remote = error.detail;
                const domain = remote.envelope?.uiIntent?.kind || '';
                if (domain) SemanticCache.invalidate(domain);
                this._applyRemote(text, remote, startTime, 'remote');
                const missingScopes = Array.isArray(remote.missingConnectorScopes)
                    ? remote.missingConnectorScopes.map((item) => String(item || '').trim()).filter(Boolean)
                    : [];
                const scopeLabel = missingScopes.length ? missingScopes[0] : '';
                const message = String(
                    remote.message
                    || remote.execution?.message
                    || (scopeLabel ? `Permission required: ${scopeLabel}` : 'Permission required for connector action.')
                ).trim();
                this.showToast(message, 'warn', 3600);
                this.container.classList.remove('refracting', 'turn-thinking');
                this.inputContainer.classList.remove('active-intent');
                SoundEngine.error();
                return;
            }
            this.container.classList.add('turn-error');
            setTimeout(() => this.container.classList.remove('turn-error'), 700);
            this.handleTransportFailure('turn', error);
            envelope = IntentLayerCompiler.compile(text, this.state.memory);
            execution = ActionExecutor.run(envelope.stateIntent.writeOperations, this.state.memory);
            kernelTrace = this.deriveKernelTrace(execution, { target: 'local-fallback', reason: 'backend unavailable', model: null });
            const uiPlan = UIPlanner.build(envelope, this.state.memory, execution);
            safePlan = UIPlanSchema.normalize(uiPlan);
            SoundEngine.error();
        } finally {
            this.state.session.isApplyingLocalTurn = false;
        }

        this.state.session.lastEnvelope = envelope;
        this.state.session.lastExecution = execution;
        this.state.session.lastKernelTrace = kernelTrace;
        const latestOps = Array.isArray(execution?.toolResults) ? execution.toolResults.map((item) => String(item?.op || '')).filter(Boolean) : [];
        if (latestOps.includes('clear_continuity_alerts') && this.status) {
            delete this.status.dataset.alert;
        }
        this.render(safePlan, envelope, kernelTrace);
        SoundEngine.transition();
        this.pushHistory(text, envelope, execution, safePlan, plannerSource, kernelTrace, mergeInfo);

        this.container.classList.remove('refracting');
        this.inputContainer.classList.remove('active-intent');

        this.state.metrics.latency = Math.round(performance.now() - startTime);
        this.state.metrics.entropy = 0.01 + Math.random() * 0.04;
        this.updateStatus(execution.ok ? `STABLE:${plannerSource.toUpperCase()}` : 'NEEDS INPUT');
        this.showToast(
            execution.message || (execution.ok ? 'Intent applied.' : 'Intent needs refinement.'),
            execution.ok ? 'ok' : 'warn'
        );
        if (execution.ok) SoundEngine.success();
        if (mergeInfo?.rebased) {
            this.showToast('Merged with newer session revision.', 'info', 2200);
        }
        this.saveState();
    },

    // Shared helper — apply a remote response object to state + render (used by cache hit path)
    _applyRemote(text, remote, startTime, plannerSource = 'remote') {
        this.state.memory = remote.memory || this.state.memory;
        this.state.session.sessionId = remote.sessionId || this.state.session.sessionId;
        this.state.session.revision = Number(remote.revision || this.state.session.revision);
        this.state.session.presence = remote.presence || this.state.session.presence;
        this.state.session.handoff = remote.handoff || this.state.session.handoff;
        const envelope = remote.envelope;
        const execution = remote.execution;
        const kernelTrace = remote.kernelTrace || this.deriveKernelTrace(execution, remote.route);
        const safePlan = UIPlanSchema.normalize(remote.plan);
        this.writeSessionIdToUrl(this.state.session.sessionId);
        this.openWebSocketSync();
        const updatedContent = remote.execution?.data?.updatedContent;
        if (updatedContent !== undefined && this.state.activeSurface) {
            this._applyUpdatedContent(updatedContent);
        }
        this.state.session.lastEnvelope = envelope;
        this.state.session.lastExecution = execution;
        this.state.session.lastKernelTrace = kernelTrace;
        this.render(safePlan, envelope, kernelTrace);
        SoundEngine.transition();
        this.pushHistory(text, envelope, execution, safePlan, plannerSource, kernelTrace, remote.merge || null);
        this.state.metrics.latency = Math.round(performance.now() - startTime);
        this.state.metrics.entropy = 0.01 + Math.random() * 0.04;
        this.updateStatus(execution?.ok ? `STABLE:${plannerSource.toUpperCase()}` : 'NEEDS INPUT');
        this.saveState();
    },

    async showReasoning(steps) {
        const overlay = document.getElementById('reasoning-overlay');
        overlay.style.display = 'flex';
        overlay.innerHTML = '';

        for (const step of steps) {
            const node = document.createElement('div');
            node.className = 'reasoning-step';
            node.innerText = step;
            overlay.appendChild(node);
            setTimeout(() => node.classList.add('visible'), 20);
            await sleep(230);
        }

        await sleep(120);
        overlay.style.display = 'none';
    },

    render(plan, envelope, kernelTrace = this.state.session.lastKernelTrace) {
        // Boot guard: OS always starts at Latent Surface. Block all renders for
        // the first 600ms after page load — long enough for all boot-time sync
        // paths (WS, SSE, poll) to fire and be ignored.
        if (this._bootGuardUntil && Date.now() < this._bootGuardUntil) return;
        this.container.classList.remove('turn-thinking');
        this.teardownSceneGraphics();
        this.state.session.lastPlan = plan;
        this.applyPlanGraphSnapshot(plan);
        const hint = plan.suggestions?.[0] || 'add task Ship onboarding';
        this.input.placeholder = `Try: "${hint}"`;
        const blocks = this.composeFeedBlocks(plan, envelope, kernelTrace);
        const core = this.buildCoreSurface(plan, envelope, this.state.session.lastExecution);
        if (window.electronAPI?.isElectron && typeof window.electronAPI?.setIntentContext === 'function') {
            window.electronAPI.setIntentContext(this.contextFromLatest(core, this.state.session.lastExecution));
        }
        const visual = this.buildPrimaryVisual(core, this.state.session.lastExecution, envelope, plan);
        const priorDomain = String(this.state.sceneDock.activeDomain || 'generic');
        const nextDomain = String(core.kind || 'generic');
        this.state.sceneDock.lastTransition = this.computeSceneTransition(priorDomain, nextDomain);
        this.state.sceneDock.activeDomain = nextDomain;
        const sceneDock = this.renderSceneDock(nextDomain);
        const showCoreCopy = !['shopping', 'webdeck', 'social', 'banking', 'contacts', 'telephony', 'tasks', 'files', 'expenses', 'notes', 'sports', 'sports_manage', 'weather', 'weather_7day', 'weather_tomorrow', 'music', 'messaging', 'connections', 'network', 'document', 'spreadsheet', 'code', 'terminal', 'presentation', 'runtime'].includes(core.kind);
        const hud = this.buildImmersiveHud(core, plan, envelope, kernelTrace, this.state.session.lastExecution);
        const railBlocks = this.buildImmersiveRailBlocks(blocks);
        const darkScenes = new Set(['weather','weather_7day','weather_tomorrow','tasks','expenses','notes','music','banking','sports','sports_manage','messaging','files','connections','network','travel','health','reminders','reminders_list','social','contacts','telephony','domain','graph','location','calendar','email','code','terminal','document','spreadsheet','presentation','content','reference','clock','enterprise','github','jira','notion','asana','finance','gaming','arvr','dating','plan','runtime']);
        const sceneTone = darkScenes.has(core.kind) ? 'dark' : 'light';
        this.container.innerHTML = `
            <div class="workspace immersive" data-scene-tone="${escapeAttr(sceneTone)}">
                ${sceneDock}
                <section class="workspace-main">
                    <div class="surface-core immersive ${escapeAttr(core.kind || 'generic')} ${escapeAttr(core.theme || '')}">
                        ${visual}
                        ${showCoreCopy ? `
                            <div class="surface-label">Workspace</div>
                            <div class="core-intent ${escapeAttr(core.variant || '')}">${escapeHtml(core.headline)}</div>
                            <div class="core-summary">${escapeHtml(core.summary)}</div>
                        ` : ''}
                    </div>
                </section>
                <aside class="workspace-side immersive-rail">
                    <div class="feed-head">Context</div>
                    <div class="immersive-hud">${hud}</div>
                    ${railBlocks}
                </aside>
            </div>
        `;
        this.decorateQuickCommandTargets();
        this.applySceneEntryMotion();
        this.activateSceneGraphics();
        this._initFunctionalSurfaces();
        this._renderPhoneSuggestions(Array.isArray(plan?.suggestions) ? plan.suggestions : []);
        this.maybePrefetchGraphSnapshot(core);
    },

    decorateQuickCommandTargets() {
        const all = Array.from(this.container.querySelectorAll('[data-command]'));
        for (const node of all) {
            node.classList.remove('quick-hotkey-target');
            node.removeAttribute('data-hotkey-index');
        }
        all.slice(0, 9).forEach((node, idx) => {
            const index = idx + 1;
            node.classList.add('quick-hotkey-target');
            node.setAttribute('data-hotkey-index', String(index));
            const baseTitle = String(node.getAttribute('title') || '').trim();
            const suffix = `Alt+${index}`;
            node.setAttribute('title', baseTitle ? `${baseTitle} | ${suffix}` : suffix);
        });
    },

    _renderPhoneSuggestions(suggestions) {
        if (window.innerWidth > 600) {
            const el = document.getElementById('phone-suggestions');
            if (el) el.style.display = 'none';
            return;
        }
        let el = document.getElementById('phone-suggestions');
        if (!el) {
            el = document.createElement('div');
            el.id = 'phone-suggestions';
            el.className = 'phone-suggestions';
            document.body.appendChild(el);
        }
        el.style.display = '';
        const chips = (Array.isArray(suggestions) ? suggestions : []).slice(0, 4);
        el.innerHTML = chips.map((s) =>
            `<button class="phone-suggestion-chip" data-command="${escapeAttr(String(s))}">${escapeHtml(String(s).slice(0, 36))}</button>`
        ).join('');
    },

    applySceneEntryMotion() {
        const surface = this.container.querySelector('.surface-core');
        if (surface) {
            surface.classList.remove('scene-enter');
            surface.classList.remove(
                'scene-domain-transition',
                'scene-domain-transition-active',
                'scene-workspace',
                'scene-portal',
                'scene-atmosphere',
                'scene-morph',
                'scene-forward',
                'scene-back'
            );
            // Force restart of CSS keyframe when scene type changes.
            void surface.offsetWidth;
            surface.classList.add('scene-enter');
            const transition = this.state.sceneDock.lastTransition || null;
            if (transition && transition.changed) {
                surface.classList.add(
                    'scene-domain-transition',
                    `scene-${transition.kind}`,
                    `scene-${transition.direction}`
                );
                surface.setAttribute('data-scene-from', transition.from);
                surface.setAttribute('data-scene-to', transition.to);
                // Stage animation in next frame so class toggles reliably across rapid scene switches.
                requestAnimationFrame(() => surface.classList.add('scene-domain-transition-active'));
                setTimeout(() => {
                    surface.classList.remove(
                        'scene-domain-transition',
                        'scene-domain-transition-active',
                        'scene-workspace',
                        'scene-portal',
                        'scene-atmosphere',
                        'scene-morph',
                        'scene-forward',
                        'scene-back'
                    );
                }, 420);
            } else {
                surface.removeAttribute('data-scene-from');
                surface.setAttribute('data-scene-to', String(this.state.sceneDock.activeDomain || 'generic'));
            }
        }
        const rail = this.container.querySelector('.workspace-side.immersive-rail');
        if (rail) {
            rail.classList.remove('rail-enter');
            rail.classList.remove('rail-domain-transition');
            void rail.offsetWidth;
            rail.classList.add('rail-enter');
            const transition = this.state.sceneDock.lastTransition || null;
            if (transition && transition.changed) {
                rail.classList.add('rail-domain-transition');
                setTimeout(() => rail.classList.remove('rail-domain-transition'), 360);
            }
        }
    },

    sceneDomainOptions() {
        return [
            { id: 'tasks', label: 'tasks' },
            { id: 'expenses', label: 'expenses' },
            { id: 'notes', label: 'notes' },
            { id: 'graph', label: 'graph' },
            { id: 'content', label: 'content' },
            { id: 'files', label: 'files' },
            { id: 'drive', label: 'drive' },
            { id: 'calendar', label: 'calendar' },
            { id: 'weather', label: 'weather' },
            { id: 'location', label: 'location' },
            { id: 'shopping', label: 'shopping' },
            { id: 'music', label: 'music' },
            { id: 'email', label: 'email' },
            { id: 'messaging', label: 'messages' },
            { id: 'health', label: 'health' },
            { id: 'smarthome', label: 'home' },
            { id: 'travel', label: 'travel' },
            { id: 'payments', label: 'pay' },
            { id: 'focus', label: 'focus' },
            { id: 'notifications', label: 'alerts' },
            { id: 'handoff', label: 'continuity' },
            { id: 'connectors', label: 'connections' },
            { id: 'runtime', label: 'runtime' },
            { id: 'webdeck', label: 'web' },
            { id: 'social', label: 'social' },
            { id: 'banking', label: 'banking' },
            { id: 'contacts', label: 'contacts' },
            { id: 'telephony', label: 'calls' },
            { id: 'generic', label: 'general' }
        ];
    },

    computeSceneTransition(fromDomain, toDomain) {
        const from = String(fromDomain || 'generic');
        const to = String(toDomain || 'generic');
        if (from === to) {
            return { from, to, changed: false, kind: 'morph', direction: 'forward' };
        }

        const workspace = new Set(['tasks', 'expenses', 'notes', 'graph', 'files']);
        const portal = new Set(['shopping', 'webdeck', 'social', 'banking', 'contacts', 'telephony']);
        const atmosphere = new Set(['weather', 'location']);

        let kind = 'morph';
        if (workspace.has(to)) kind = 'workspace';
        else if (portal.has(to)) kind = 'portal';
        else if (atmosphere.has(to)) kind = 'atmosphere';

        const order = this.sceneDomainOptions().map((item) => item.id);
        const fromIndex = Math.max(0, order.indexOf(from));
        const toIndex = Math.max(0, order.indexOf(to));
        const direction = toIndex >= fromIndex ? 'forward' : 'back';
        return { from, to, changed: true, kind, direction };
    },

    renderSceneDock(activeDomain) {
        const options = this.sceneDomainOptions();
        const current = String(activeDomain || 'generic');
        return `
            <div class="scene-dock" aria-label="scene domains">
                ${options.map((item) => `
                    <button
                        class="scene-dock-node ${item.id === current ? 'active' : ''}"
                        type="button"
                        data-scene-domain="${escapeAttr(item.id)}"
                        title="Switch to ${escapeAttr(item.label)} scene"
                    >${escapeHtml(item.label)}</button>
                `).join('')}
            </div>
        `;
    },

    inferHistoryEntryDomain(entry) {
        if (!entry || typeof entry !== 'object') return 'generic';
        const execution = entry.executionSnapshot || null;
        const core = this.buildCoreSurface(entry.plan || {}, entry.envelope || {}, execution);
        if (core?.kind === 'document' && core?.info?.storage === 'connector' && String(core?.info?.service || '').trim() === 'gdrive') {
            return 'drive';
        }
        return String(core?.kind || 'generic');
    },

    sceneDomainToIntent(domain) {
        const locationHint = this.resolveIntentLocationHint();
        // For weather, GPS coords in session.locationHint always win over last-execution location.
        const weatherHint = String(this.state.session.locationHint || '').trim() || locationHint;
        console.log('[weather-debug] sceneDomainToIntent domain=', domain, 'locationHint=', locationHint, 'session.locationHint=', this.state.session.locationHint, 'weatherHint=', weatherHint, '_fromGPS=', this.state.session._locationHintFromGPS);
        const byDomain = {
            tasks: 'show tasks',
            expenses: 'show expenses',
            notes: 'show notes',
            graph: 'show graph summary',
            content: 'show content history',
            files: 'show workspace files',
            drive: 'show my drive files',
            calendar: 'show my calendar',
            weather: weatherHint ? `show weather in ${weatherHint}` : "what's the weather where i am",
            location: 'where am i',
            shopping: 'show me running shoes',
            music: 'play my top songs',
            email: 'check my email',
            messaging: 'show my messages',
            health: 'show my health summary',
            smarthome: 'show home status',
            travel: 'show my travel itinerary',
            payments: 'show my payment history',
            focus: 'show my screen time stats',
            notifications: 'show my notifications',
            handoff: 'show continuity',
            connectors: 'show connections',
            runtime: 'show runtime health',
            webdeck: 'open example.com',
            social: 'show my social feed',
            banking: 'show account balances',
            contacts: 'show contacts',
            telephony: 'show call status',
            generic: 'show me what i can do'
        };
        return byDomain[String(domain || 'generic')] || byDomain.generic;
    },

    inferHistoryEntryShellTarget(entry) {
        if (!entry || typeof entry !== 'object') return { type: 'scene', value: 'generic', label: 'resume surface' };
        const execution = entry.executionSnapshot || null;
        const core = this.buildCoreSurface(entry.plan || {}, entry.envelope || {}, execution);
        const kind = String(core?.kind || 'generic').trim().toLowerCase();
        const info = (core?.info && typeof core.info === 'object') ? core.info : {};

        if (['document', 'spreadsheet', 'presentation'].includes(kind)) {
            const name = String(info.name || '').trim();
            if (name) {
                return {
                    type: 'repo',
                    domain: kind,
                    name,
                    branch: String(info.branch || this.state.session.workspace?.branch || 'main'),
                    label: `resume ${name}`
                };
            }
        }

        const sceneKinds = new Set(['content', 'files', 'drive', 'email', 'messaging', 'calendar', 'notifications', 'handoff', 'connectors', 'runtime']);
        if (sceneKinds.has(kind)) {
            const labelMap = {
                content: 'open content repo',
                files: 'open workspace files',
                drive: 'open drive',
                email: 'open email',
                messaging: 'open messages',
                calendar: 'open calendar',
                notifications: 'open notifications',
                handoff: 'inspect continuity',
                connectors: 'open connections',
                runtime: 'inspect runtime',
            };
            return { type: 'scene', value: kind, label: labelMap[kind] || `open ${kind}` };
        }

        const intent = String(entry?.intent || this.sceneDomainToIntent(kind)).trim() || 'resume surface';
        return { type: 'command', value: intent, label: intent };
    },

    buildShoppingRefineCommands(brandName, rawQuery = '', category = 'shoes') {
        const brand = String(brandName || '').trim();
        const query = String(rawQuery || '').trim().toLowerCase();
        const cat = String(category || 'shoes').trim().toLowerCase() || 'shoes';
        const gender = /\bwomen|womens|female|ladies\b/.test(query) ? 'for women' : 'for men';
        const sizeMatch = query.match(/\bsize\s*\d{1,2}(?:\s*(?:1\/2|\.5|½))?\b|\b\d{1,2}\s*(?:1\/2|\.5|½)\b/i);
        const sizeChunk = sizeMatch ? String(sizeMatch[0]).replace(/\s+/g, ' ').trim() : '';
        const sizePhrase = sizeChunk ? `${sizeChunk.startsWith('size') ? sizeChunk : `size ${sizeChunk}`} ` : '';
        const stem = [brand, cat].filter(Boolean).join(' ').trim() || cat;
        const seeds = [
            `show me ${sizePhrase}${stem} ${gender}`.replace(/\s+/g, ' ').trim(),
            `show me ${brand || ''} running ${cat} ${gender}`.replace(/\s+/g, ' ').trim(),
            `show me ${brand || ''} lifestyle ${cat} ${gender}`.replace(/\s+/g, ' ').trim(),
            `show me ${brand || ''} ${cat} on sale ${gender}`.replace(/\s+/g, ' ').trim(),
        ];
        const out = [];
        const seen = new Set();
        for (const item of seeds) {
            const cmd = String(item || '').trim();
            const key = cmd.toLowerCase();
            if (!cmd || seen.has(key)) continue;
            seen.add(key);
            out.push(cmd);
            if (out.length >= 4) break;
        }
        return out;
    },

    async switchToSceneDomain(domain) {
        const target = String(domain || '').trim().toLowerCase();
        if (!target) return;
        this.syncActiveSurfaceWorkspaceFocus();
        if (target === 'generic') {
            this.renderWelcome();
            this.state.sceneDock.lastTransition = this.computeSceneTransition(this.state.sceneDock.activeDomain, 'generic');
            this.state.sceneDock.activeDomain = 'generic';
            this.applySceneTransition();
            this.saveState();
            return;
        }
        if (target === 'notifications') {
            await this.showNotificationsInbox();
            return;
        }
        if (target === 'handoff' || target === 'continuity') {
            await this.showContinuitySurface();
            return;
        }
        if (target === 'connectors') {
            await this.showConnectionsPanel();
            return;
        }
        if (target === 'runtime') {
            await this.showRuntimeHealth();
            return;
        }
        if (target === 'email') {
            await this.showEmailInbox();
            return;
        }
        if (target === 'messaging') {
            await this.showMessagingInbox();
            return;
        }
        if (target === 'calendar') {
            await this.showCalendarAgenda();
            return;
        }
        if (target === 'content') {
            await this.showContentRepository();
            return;
        }
        if (target === 'drive') {
            await this.showDriveFiles();
            return;
        }
        if (target === 'files') {
            await this.showWorkspaceFiles();
            return;
        }
        // Weather is time-sensitive — never restore stale history, always fetch fresh.
        if (target !== 'weather') {
            let matchIndex = -1;
            for (let i = this.state.history.length - 1; i >= 0; i -= 1) {
                const entry = this.state.history[i];
                const entryDomain = this.inferHistoryEntryDomain(entry);
                if (entryDomain === target) { matchIndex = i; break; }
            }
            if (matchIndex >= 0) {
                this.restoreFromHistory(matchIndex);
                return;
            }
        }
        // For weather: ensure we have clean GPS coordinates, not a stale verbose city name.
        if (target === 'weather') {
            const hint = String(this.state.session.locationHint || '').trim();
            const isCoords = /^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$/.test(hint);
            console.log('[weather-debug] switchToSceneDomain weather check: hint=', hint, 'isCoords=', isCoords, '_fromGPS=', this.state.session._locationHintFromGPS);
            if (!hint || !isCoords) {
                // Missing or non-coordinate hint — clear it and re-detect via GPS.
                this.state.session.locationHint = '';
                this.state.session._locationHintFromGPS = false;
                console.log('[weather-debug] clearing stale hint, re-detecting GPS...');
                await this.primeRelativeLocationContext().catch(() => {});
                console.log('[weather-debug] after GPS detect: session.locationHint=', this.state.session.locationHint);
            }
        }
        this.handleIntent(this.sceneDomainToIntent(target));
    },

    stepSceneDomain(delta) {
        const options = this.sceneDomainOptions().map((item) => item.id);
        const current = String(this.state.sceneDock.activeDomain || 'generic');
        const index = Math.max(0, options.indexOf(current));
        const next = (index + options.length + Number(delta || 0)) % options.length;
        this.switchToSceneDomain(options[next]);
    },

    normalizeGraphSnapshot(snapshot) {
        if (!snapshot || typeof snapshot !== 'object') return null;
        return {
            entities: Array.isArray(snapshot.entities) ? snapshot.entities : [],
            relations: Array.isArray(snapshot.relations) ? snapshot.relations : [],
            events: Array.isArray(snapshot.events) ? snapshot.events : [],
            counts: snapshot.counts && typeof snapshot.counts === 'object' ? snapshot.counts : {},
            fetchedAt: Date.now(),
        };
    },

    applyPlanGraphSnapshot(plan) {
        const snapshot = this.normalizeGraphSnapshot(plan?.trace?.graphSnapshot);
        if (!snapshot) return;
        this.state.session.graphSnapshot = snapshot;
        this.state.session.graphSnapshotRevision = Number(this.state.session.revision || 0);
    },

    maybePrefetchGraphSnapshot(core) {
        if (String(core?.kind || '') !== 'graph') return;
        const needsFetch = !this.state.session.graphSnapshot
            || Number(this.state.session.graphSnapshotRevision || 0) < Number(this.state.session.revision || 0);
        if (!needsFetch) return;
        this.fetchGraphSnapshot().catch(() => { });
    },

    async fetchGraphSnapshot() {
        if (this.state.session.graphFetchInFlight) return;
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return;
        this.state.session.graphFetchInFlight = true;
        try {
            const resp = await fetch(`/api/session/${encodeURIComponent(sid)}/graph?limit=300`);
            if (!resp.ok) return;
            const payload = await resp.json();
            const snapshot = {
                entities: Array.isArray(payload?.entities) ? payload.entities : [],
                relations: Array.isArray(payload?.relations) ? payload.relations : [],
                events: Array.isArray(payload?.events) ? payload.events : [],
                counts: payload?.counts && typeof payload.counts === 'object' ? payload.counts : {},
                fetchedAt: Date.now(),
            };
            this.state.session.graphSnapshot = snapshot;
            this.state.session.graphSnapshotRevision = Number(this.state.session.revision || 0);
            if (this.state.session.lastPlan && this.state.session.lastEnvelope) {
                this.render(this.state.session.lastPlan, this.state.session.lastEnvelope, this.state.session.lastKernelTrace);
            }
        } catch {
            // Best-effort visual fetch.
        } finally {
            this.state.session.graphFetchInFlight = false;
        }
    },

    buildImmersiveRailBlocks(blocks) {
        const list = Array.isArray(blocks) ? blocks : [];
        const preferredOrder = ['trace-result', 'trace-events', 'trace-next', 'trace-system', 'trace-connectors'];
        const sorted = [...list].sort((a, b) => preferredOrder.indexOf(a.id) - preferredOrder.indexOf(b.id));
        return sorted.slice(0, 4).map((block) => this.renderFeedBlock(block)).join('');
    },

    buildImmersiveHud(core, plan, envelope, kernelTrace, execution) {
        const latest = this.latestToolResult(execution) || {};
        const route = kernelTrace?.route || {};
        const perf = kernelTrace?.runtime || {};
        const items = [];
        items.push(`mode ${String(core.kind || 'generic')}`);
        items.push(`route ${String(route.target || 'deterministic')} / ${String(route.reason || 'local')}`);
        items.push(`latency ${Math.round(Number(perf.totalMs || this.state.metrics.latency || 0))}ms`);
        if (latest?.op === 'shop_catalog_search') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const target = (data.sourceTarget && typeof data.sourceTarget === 'object') ? data.sourceTarget : null;
            if (String(target?.mode || '').toLowerCase() === 'direct') {
                items.push('route direct-source');
                items.push(String(target?.label || 'open source'));
                return items
                    .filter(Boolean)
                    .map((line) => `<div class="hud-line">${escapeHtml(String(line))}</div>`)
                    .join('');
            }
        }
        if (core.kind === 'webdeck') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const source = String(data.source || 'scaffold').trim();
            const itemsCount = Array.isArray(data.items) ? data.items.length : 0;
            const hasUrl = Boolean(String(data.url || '').trim());
            items.push(`source ${source}`);
            items.push(hasUrl ? 'mode page' : 'mode search');
            if (itemsCount > 0) items.push(`results ${itemsCount}`);
            return items
                .filter(Boolean)
                .map((line) => `<div class="hud-line">${escapeHtml(String(line))}</div>`)
                .join('');
        }
        if (latest?.message) items.push(String(latest.message));
        const preview = Array.isArray(latest?.previewLines) ? latest.previewLines.slice(0, 3) : [];
        for (const line of preview) items.push(String(line));
        return items
            .filter(Boolean)
            .map((line) => `<div class="hud-line">${escapeHtml(String(line))}</div>`)
            .join('');
    },

    humanizeIntentLabel(rawIntent) {
        const text = String(rawIntent || '').trim();
        if (!text) return 'State intent to synthesize your workspace.';
        const lower = text.toLowerCase();
        const mappings = [
            { pattern: /run state guidance run anomalies/, label: 'Run-state guidance anomalies' },
            { pattern: /run state guidance run metrics/, label: 'Run-state guidance metrics' },
            { pattern: /run state guidance run history/, label: 'Run-state guidance history' },
            { pattern: /run state guidance dry run|run state guidance apply/, label: 'Execute run-state guidance' },
            { pattern: /run state guidance$/, label: 'Run-state guidance' },
            { pattern: /run state anomalies/, label: 'Run-state anomalies' },
            { pattern: /run state metrics/, label: 'Run-state metrics' },
            { pattern: /run state history/, label: 'Run-state history' },
            { pattern: /run state summary/, label: 'Run-state summary' },
            { pattern: /run state guidance/, label: 'Run-state guidance' },
            { pattern: /^add task\b/, label: 'Add task' },
            { pattern: /^complete task\b/, label: 'Complete task' },
            { pattern: /^show graph summary\b/, label: 'Graph summary' }
        ];
        const mapped = mappings.find((item) => item.pattern.test(lower));
        if (mapped) return mapped.label;

        if (text.length > 72) {
            const compact = text
                .replace(/\s+window\s+\d+\s*(ms|s|m|h|d)\b/gi, '')
                .replace(/\s+limit\s+\d+\b/gi, '')
                .replace(/\s+/g, ' ')
                .trim();
            return `${compact.slice(0, 69)}...`;
        }
        return text;
    },

    hashText(value) {
        const text = String(value || '');
        let hash = 0;
        for (let i = 0; i < text.length; i += 1) {
            hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0;
        }
        return Math.abs(hash);
    },

    graphNodeLabel(entity, idx) {
        const title = String(entity?.title || '').trim();
        const text = String(entity?.text || '').trim();
        const category = String(entity?.category || '').trim();
        const kind = String(entity?.kind || 'entity').trim().toLowerCase();
        const primary = title || text || category || kind || `node-${idx + 1}`;
        return primary.slice(0, 14);
    },

    composeFeedBlocks(plan, envelope, kernelTrace) {
        const trace = kernelTrace || this.deriveKernelTrace(this.state.session.lastExecution, null);
        const execution = this.state.session.lastExecution || {};
        const resultItems = this.buildExecutionResultItems(execution);
        const latestOp = this.latestToolResult(execution);
        const policyCodes = Array.isArray(trace?.policy?.codes) && trace.policy.codes.length ? trace.policy.codes.join(', ') : 'none';
        const confirmCommand = this.extractConfirmationCommand(this.state.session.lastExecution?.message || '');
        const diff = trace?.diff || { tasks: 0, expenses: 0, notes: 0 };
        const runtimeItems = this.buildRuntimeJobItems(trace?.runtime);
        const perfItems = this.buildPerformanceItems(trace?.runtime);
        const sloItems = this.buildSloItems(trace?.runtime);
        const restoreItems = this.buildRestoreItems(trace?.runtime);
        const faultItems = this.buildFaultItems(trace?.runtime);
        const graphItems = this.buildGraphContextItems(trace?.graph, trace?.runtime);
        const handoffItems = this.buildHandoffItems(trace?.runtime);
        const journalItems = (trace?.journalTail || [])
            .slice(-3)
            .reverse()
            .map((entry) => `${entry.ok ? 'ok' : 'denied'} ${entry.op} | dT ${entry.diff?.tasks ?? 0}, dE ${entry.diff?.expenses ?? 0}, dN ${entry.diff?.notes ?? 0}`);
        const systemItems = this.buildSystemItems(trace, policyCodes, diff, envelope);
        const connectorAccessItems = this.buildConnectorAccessItems(this.state.session.lastExecution);
        const liveEventItems = this.buildLiveEventItems();

        const primary = [
            {
                id: 'trace-result',
                type: 'list',
                label: 'Result',
                items: resultItems
            },
            ...(liveEventItems.length ? [{
                id: 'trace-events',
                type: 'list',
                label: 'Live Events',
                items: liveEventItems
            }] : []),
            {
                id: 'trace-next',
                type: 'list',
                label: 'Next Moves',
                items: (plan?.suggestions || []).slice(0, 4).map((text) => ({ text, command: text }))
            },
            {
                id: 'trace-system',
                type: 'list',
                label: 'System',
                items: systemItems
            },
            {
                id: 'trace-connectors',
                type: 'list',
                label: 'Connector Access',
                items: connectorAccessItems
            },
        ];
        const diagnostic = [
            {
                id: 'trace-jobs',
                type: 'list',
                label: 'Jobs',
                items: runtimeItems
            },
            {
                id: 'trace-perf',
                type: 'list',
                label: 'Performance',
                items: perfItems
            },
            {
                id: 'trace-slo',
                type: 'list',
                label: 'SLO',
                items: sloItems
            },
            {
                id: 'trace-restore',
                type: 'list',
                label: 'Restore',
                items: restoreItems
            },
            {
                id: 'trace-faults',
                type: 'list',
                label: 'Faults',
                items: faultItems
            },
            {
                id: 'trace-journal',
                type: 'list',
                label: 'Journal',
                items: journalItems.length ? journalItems : ['No mutation events yet.']
            },
            {
                id: 'trace-graph',
                type: 'list',
                label: 'Graph Context',
                items: graphItems
            },
            {
                id: 'trace-handoff',
                type: 'list',
                label: 'Handoff',
                items: handoffItems
            },
            ...(confirmCommand ? [{
                id: 'trace-confirm',
                type: 'list',
                label: 'Required Confirmation',
                items: [{ text: confirmCommand, command: confirmCommand }]
            }] : [])
        ];
        if (latestOp?.ok && (latestOp.op === 'weather_forecast' || latestOp.op === 'location_status' || latestOp.op === 'shop_catalog_search' || latestOp.op === 'fetch_url' || latestOp.op === 'web_search' || latestOp.op === 'web_summarize')) {
            return primary.slice(0, 4);
        }
        return [...primary, ...diagnostic].slice(0, 10);
    },

    buildSystemItems(trace, policyCodes, diff, envelope) {
        const routeClass = String(trace?.route?.intentClass || 'unknown');
        const routeConfidence = Number(trace?.route?.confidence || 0);
        const pActive = Number(trace?.runtime?.presence?.activeCount || this.state.session.presence?.activeCount || 0);
        const pCount = Number(trace?.runtime?.presence?.count || this.state.session.presence?.count || 0);
        return [
            `route: ${trace?.route?.target || 'deterministic'} / ${trace?.route?.reason || 'default'}`,
            `intent class: ${routeClass} (${Math.round(routeConfidence * 100)}%)`,
            `policy: ${trace?.policy?.allAllowed ? 'allowed' : 'blocked'} | ${policyCodes}`,
            `diff: t${diff.tasks >= 0 ? '+' : ''}${diff.tasks} e${diff.expenses >= 0 ? '+' : ''}${diff.expenses} n${diff.notes >= 0 ? '+' : ''}${diff.notes}`,
            `presence: ${pActive}/${pCount} active`,
            `intent: ${String(envelope?.taskIntent?.operation || 'read').toUpperCase()} / ${(envelope?.stateIntent?.readDomains || ['tasks', 'expenses', 'notes']).join('+')}`,
            `link: ${String(this.state.session.syncTransport || 'idle').toUpperCase()} rev ${this.state.session.revision}`
        ];
    },

    buildConnectorAccessItems(execution) {
        const toolResults = Array.isArray(execution?.toolResults) ? execution.toolResults : [];
        const latest = toolResults.length ? toolResults[toolResults.length - 1] : null;
        const blocked = toolResults.find((item) => item && item.ok === false);
        const blockedCode = String(blocked?.policy?.code || '');
        const blockedReason = String(blocked?.policy?.reason || blocked?.message || '');
        const tryCommand = this.extractTryCommand(blockedReason);
        const locationHint = this.resolveIntentLocationHint();
        const locationWeatherCommand = locationHint ? `weather in ${locationHint}` : "weather today where i am";
        const locationWeatherLabel = locationHint ? 'weather in home location' : locationWeatherCommand;
        if (latest?.ok && latest?.op === 'weather_forecast') {
            return [
                'status: weather connector active',
                { text: "what's the weather where i am", command: "what's the weather where i am" },
                { text: 'weather tomorrow where i am', command: 'weather tomorrow where i am' },
                { text: '7-day forecast', command: 'weather this week where i am' },
                { text: locationWeatherLabel, command: locationWeatherCommand },
            ];
        }
        if (latest?.ok && latest?.op === 'location_status') {
            return [
                'status: location context active',
                { text: "what's the weather where i am", command: "what's the weather where i am" },
                { text: locationWeatherLabel, command: locationWeatherCommand },
                { text: 'weather tomorrow where i am', command: 'weather tomorrow where i am' },
                { text: 'show connector grants', command: 'show connector grants' }
            ];
        }
        if (latest?.ok && latest?.op === 'shop_catalog_search') {
            return [
                'status: shopping catalog active',
                { text: 'show me new running shoes', command: 'show me new running shoes' },
                { text: 'find me an office outfit', command: 'find me an office outfit' },
                { text: 'show streetwear sneakers', command: 'show streetwear sneakers' },
                { text: 'show me a casual outfit', command: 'show me a casual outfit' }
            ];
        }
        if (blockedCode === 'connector_scope_required') {
            const blockedOp = String(blocked?.op || '');
            const byOp = {
                weather_forecast: 'grant weather forecast',
                telephony_call_start: 'grant connector scope telephony.call.start',
                banking_balance_read: 'grant connector scope bank.account.balance.read',
                banking_transactions_read: 'grant connector scope bank.transaction.read',
                social_feed_read: 'grant connector scope social.feed.read',
                social_message_send: 'grant connector scope social.message.send'
            };
            const grant = tryCommand || byOp[blockedOp] || 'show connector grants';
            const ttlGrant = grant.startsWith('grant ') ? `${grant} for 10m` : '';
            return [
                'status: permission required for connector action',
                { text: grant, command: grant },
                ...(ttlGrant ? [{ text: ttlGrant, command: ttlGrant }] : []),
                { text: 'show connector grants', command: 'show connector grants' },
                { text: 'retry blocked intent', command: String(this.state.session.lastIntent || '').trim() || 'show connector grants' }
            ].slice(0, 5);
        }
        if (blockedCode === 'confirmation_required') {
            const confirm = tryCommand || this.extractConfirmationCommand(blockedReason);
            return [
                'status: confirmation required for write action',
                ...(confirm ? [{ text: confirm, command: confirm }] : []),
                { text: 'show connector grants', command: 'show connector grants' },
                { text: 'retry blocked intent', command: String(this.state.session.lastIntent || '').trim() || 'show connector grants' }
            ].slice(0, 5);
        }
        return [
            { text: "what's the weather where i am", command: "what's the weather where i am" },
            { text: locationWeatherLabel, command: locationWeatherCommand },
            { text: 'where am i', command: 'where am i' },
            { text: 'show connector grants', command: 'show connector grants' },
            { text: 'show me new shoes', command: 'show me new shoes' }
        ];
    },

    resolveIntentLocationHint() {
        const execution = this.state.session.lastExecution;
        const toolResults = Array.isArray(execution?.toolResults) ? execution.toolResults : [];
        const latest = toolResults.length ? toolResults[toolResults.length - 1] : null;
        const data = (latest?.data && typeof latest.data === 'object') ? latest.data : {};
        const info = Array.isArray(latest?.previewLines) ? this.parsePreviewMap(latest.previewLines) : {};
        const fromData = String(data.location || '').trim();
        if (fromData) return fromData;
        const fromInfo = String(info.location || '').trim();
        if (fromInfo) return fromInfo;
        const fromSession = String(this.state.session.locationHint || '').trim();
        if (fromSession) return fromSession;
        return '';
    },

    buildLiveEventItems() {
        const items = Array.isArray(this.state.runtimeEvents) ? this.state.runtimeEvents : [];
        return items
            .slice(-4)
            .reverse()
            .map((item) => {
                const when = Number(item?.ts || 0);
                const stamp = when ? new Date(when).toLocaleTimeString() : '';
                const title = String(item?.title || 'event').trim();
                const message = String(item?.message || '').trim();
                return `${stamp ? `${stamp} | ` : ''}${title}${message ? `: ${message}` : ''}`;
            });
    },

    buildExecutionResultItems(execution) {
        const toolResults = Array.isArray(execution?.toolResults) ? execution.toolResults : [];
        const latest = toolResults.length ? toolResults[toolResults.length - 1] : null;
        if (latest?.ok && latest?.op === 'shop_catalog_search') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const target = (data.sourceTarget && typeof data.sourceTarget === 'object') ? data.sourceTarget : null;
            if (String(target?.mode || '').toLowerCase() === 'direct') {
                const refs = Array.isArray(data.items) ? data.items.length : 0;
                return [
                    'route: direct source',
                    String(target?.label || 'open source'),
                    String(target?.url || ''),
                    `visual refs: ${refs}`,
                ].filter(Boolean);
            }
        }
        if (latest && Array.isArray(latest.previewLines) && latest.previewLines.length) {
            return latest.previewLines.slice(0, 5).map((line) => String(line));
        }
        if (latest && typeof latest.message === 'string' && latest.message.trim()) {
            return [latest.message.trim()];
        }
        const message = String(execution?.message || '').trim();
        return [message || 'No result yet.'];
    },

    latestToolResult(execution) {
        const toolResults = Array.isArray(execution?.toolResults) ? execution.toolResults : [];
        return toolResults.length ? toolResults[toolResults.length - 1] : null;
    },

    parsePreviewMap(previewLines) {
        const out = {};
        const items = Array.isArray(previewLines) ? previewLines : [];
        for (const line of items) {
            const value = String(line || '');
            const idx = value.indexOf(':');
            if (idx <= 0) continue;
            const key = value.slice(0, idx).trim().toLowerCase();
            const raw = value.slice(idx + 1).trim();
            if (!key || !raw) continue;
            out[key] = raw;
        }
        return out;
    },

    buildCoreSurface(plan, envelope, execution) {
        const fallbackHeadline = this.humanizeIntentLabel((envelope?.raw || this.state.session.lastIntent || '').trim());
        const fallbackSummary = plan.subtitle || 'Surface online.';
        const toolResults = Array.isArray(execution?.toolResults) ? execution.toolResults : [];
        const latest = toolResults.length ? toolResults[toolResults.length - 1] : null;
        const domains = Array.isArray(envelope?.stateIntent?.readDomains) ? envelope.stateIntent.readDomains : [];
        const domain = String(domains[0] || 'generic');
        const tolerantSurfaceOps = new Set([
            'gmail.list', 'gmail.search', 'gmail.send', 'gmail.trash',
            'email_read', 'email_search',
            'gcal.list', 'gcal.create', 'gcal.delete',
            'calendar_list', 'calendar_availability',
            'gdrive.list', 'gdrive.create', 'gdrive.open',
            'slack.list_channels', 'slack.send', 'slack.read',
            'slack_send', 'slack_read', 'slack_search', 'slack_status', 'slack_reaction',
            'messaging_send', 'messaging_read', 'messaging_reply', 'messaging_search',
            'connections_status',
        ]);

        // ── Multi-step plan: 2+ ops executed → plan surface ─────────────────
        if (execution?.isPlan && toolResults.length >= 2) {
            const allOk   = toolResults.every(r => r?.ok !== false);
            const anyFail = toolResults.some(r => r?.ok === false);
            const doneCount = toolResults.filter(r => r?.ok).length;
            const headline = anyFail
                ? `${doneCount}/${toolResults.length} steps completed`
                : `${toolResults.length} steps completed`;
            return {
                headline,
                summary: fallbackHeadline,
                variant: 'result',
                kind: 'plan',
                theme: anyFail ? 'theme-warn' : 'theme-plan',
                info: { steps: toolResults, intent: fallbackHeadline, allOk, anyFail },
            };
        }
        if (!latest) {
            if (domain === 'tasks') return { headline: 'Task Flow', summary: 'Generated task workspace', variant: 'result', kind: 'tasks', theme: 'theme-tasks' };
            if (domain === 'expenses') return { headline: 'Spend Pulse', summary: 'Generated expense workspace', variant: 'result', kind: 'expenses', theme: 'theme-expenses' };
            if (domain === 'notes') return { headline: 'Knowledge Stream', summary: 'Generated notes workspace', variant: 'result', kind: 'notes', theme: 'theme-notes' };
            if (domain === 'graph') return { headline: 'System Graph', summary: 'Generated relation workspace', variant: 'result', kind: 'graph', theme: 'theme-graph' };
            if (domain === 'content') return { headline: 'Content Repo', summary: 'Repository objects and history', variant: 'result', kind: 'content', theme: 'theme-content', info: { action: 'list', items: [], branch: this.state.session.workspace?.branch || 'main' } };
            if (domain === 'files') return { headline: 'Workspace Files', summary: 'External filesystem workspace', variant: 'result', kind: 'files', theme: 'theme-files', info: { storage: 'workspace', path: '.', items: [] } };
            if (domain === 'drive') return { headline: 'Google Drive', summary: 'Connector file storage', variant: 'result', kind: 'drive', theme: 'theme-files', info: { storage: 'connector', service: 'gdrive', items: [] } };
            if (domain === 'email') return { headline: 'Email', summary: 'Inbox and mail routing', variant: 'result', kind: 'email', theme: 'theme-email', info: { messages: [], provider: 'gmail' } };
            if (domain === 'notifications') return { headline: 'Notifications', summary: 'Shell event inbox', variant: 'result', kind: 'notifications', theme: 'theme-notifications', info: { items: Array.isArray(this.state.runtimeEvents) ? this.state.runtimeEvents : [] } };
            if (domain === 'handoff') return { headline: 'Continuity', summary: 'Cross-device runtime state', variant: 'result', kind: 'handoff', theme: 'theme-handoff', info: {} };
            if (domain === 'connectors') return { headline: 'Connections', summary: 'Manage connected services', variant: 'result', kind: 'connections', theme: 'theme-connections' };
            if (domain === 'runtime') return { headline: 'Runtime', summary: 'Shell health and latency', variant: 'result', kind: 'runtime', theme: 'theme-connections', info: {} };
            return { headline: fallbackHeadline, summary: fallbackSummary, variant: 'intent', kind: 'generic', theme: 'theme-neutral' };
        }
        if (!latest.ok && !tolerantSurfaceOps.has(String(latest.op || ''))) {
            return { headline: fallbackHeadline, summary: fallbackSummary, variant: 'intent', kind: 'generic', theme: 'theme-neutral' };
        }
        if (latest.op === 'weather_forecast') {
            const weatherData = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const info = this.parsePreviewMap(latest.previewLines);
            const temp = String(weatherData.temperatureF || info.temperature || '').replace(/\s*f$/i, '').trim();
            const condition = String(weatherData.condition || info.condition || '').trim();
            const location = String(weatherData.location || info.location || '').trim();
            const wind = String(weatherData.windMph || info.wind || '').trim();
            const source = String(weatherData.source || info.source || '').trim();
            const headline = temp && condition ? `${temp}F, ${condition}` : (latest.message || 'Weather');
            const summaryParts = [location, wind, source ? `source: ${source}` : ''].filter(Boolean);
            const lower = condition.toLowerCase();
            const theme = lower.includes('rain') || lower.includes('storm')
                ? 'theme-rain'
                : lower.includes('snow')
                    ? 'theme-snow'
                    : lower.includes('sun') || lower.includes('clear')
                        ? 'theme-sun'
                        : 'theme-cloud';
            const window = String(weatherData.window || 'now');
            const _isMultiDay = window === '7day' || window === 'weekend'
                || /^\d+day$/.test(window)
                || /^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$/.test(window);
            const _isTodHourly = window === 'tomorrow' || window === 'tonight'
                || /^tomorrow-/.test(window)
                || window === 'morning' || window === 'afternoon' || window === 'evening';
            const kind = _isMultiDay ? 'weather_7day' : _isTodHourly ? 'weather_tomorrow' : 'weather';
            const mergedInfo = {
                ...info, ...weatherData,
                forecast: Array.isArray(weatherData.forecast) ? weatherData.forecast : [],
                forecastTomorrow: Array.isArray(weatherData.forecastTomorrow) ? weatherData.forecastTomorrow : [],
                daily: Array.isArray(weatherData.daily) ? weatherData.daily : [],
            };
            return { headline, summary: summaryParts.join(' | '), variant: 'result', kind, theme, info: mergedInfo };
        }
        if (['sports_scores', 'sports_schedule', 'sports_standings', 'sports_my_teams'].includes(latest.op)) {
            const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const league = String(d.league || d.sport || '').toUpperCase() || 'Sports';
            const headline = latest.message || `${league} ${latest.op.replace('sports_', '').replace('_', ' ')}`;
            const kind = 'sports';
            return { headline, summary: league, variant: 'result', kind, theme: 'theme-sports', info: d };
        }
        if (['sports_follow_team', 'sports_unfollow_team'].includes(latest.op)) {
            const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const headline = latest.message || (latest.op === 'sports_follow_team' ? 'Following team' : 'Unfollowed team');
            return { headline, summary: String(d.sport || '').toUpperCase(), variant: 'result', kind: 'sports_manage', theme: 'theme-sports', info: d };
        }
        if (latest.op === 'network_view') {
            const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const nets = Array.isArray(d.networks) ? d.networks : [];
            const totalMsgs = Object.values(d.messagesByTopic || {}).reduce((a, v) => a + (Array.isArray(v) ? v.length : 0), 0);
            const headline = 'Genome Mesh';
            const summary = `${nets.length} network${nets.length !== 1 ? 's' : ''} · ${totalMsgs} message${totalMsgs !== 1 ? 's' : ''}`;
            return { headline, summary, variant: 'result', kind: 'network', theme: 'theme-network', info: d };
        }
        if (latest.op === 'location_status') {
            const info = this.parsePreviewMap(latest.previewLines);
            const location = String(info.location || '').trim();
            const headline = location || latest.message || 'Location context';
            return { headline, summary: 'Current location context for intent routing.', variant: 'result', kind: 'location', theme: 'theme-location', info };
        }
        if (latest.op === 'fetch_url' || latest.op === 'web_summarize') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const title = String(data.title || latest.message || 'Web page').trim();
            const excerpt = String(data.excerpt || '').trim();
            const url = String(data.url || '').trim();
            const favicon = String(data.favicon || '').trim();
            const thumbnail = String(data.thumbnail || '').trim();
            const source = String(data.source || 'scaffold').trim();
            return {
                headline: title || url || 'Web Surface',
                summary: excerpt.slice(0, 120) || url,
                variant: 'result',
                kind: 'webdeck',
                theme: 'theme-webdeck',
                info: { url, title, excerpt, favicon, thumbnail, source, op: latest.op },
            };
        }
        if (latest.op === 'web_search') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const items = Array.isArray(data.items) ? data.items : [];
            const query = String(data.query || '').trim();
            const source = String(data.source || 'scaffold').trim();
            const siteTarget = (data.sourceTarget && typeof data.sourceTarget === 'object') ? data.sourceTarget : null;
            return {
                headline: query ? `"${query}"` : 'Web Search',
                summary: `${items.length} results - ${source}`,
                variant: 'result',
                kind: 'webdeck',
                theme: 'theme-webdeck',
                info: { query, items, source, siteTarget, op: 'web_search' },
            };
        }
        if (latest.op === 'shop_catalog_search') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const items = Array.isArray(data.items) ? data.items : [];
            const category = String(data.category || '').trim();
            const label = category === 'shoes' ? 'shoes' : category === 'outfit' ? 'outfits' : 'products';
            const headline = `${items.length} ${label}`;
            const query = String(data.query || this.state.session.lastIntent || '').trim();
            return {
                headline,
                summary: query || 'Shopping results',
                variant: 'result',
                kind: 'shopping',
                theme: 'theme-shopping',
                info: { ...data, items },
            };
        }
        if (latest.op === 'list_files' || latest.op === 'read_file') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const items = Array.isArray(data.items) ? data.items : [];
            const path = String(data.path || '').trim();
            const excerpt = String(data.excerpt || '').trim();
            const lineCount = Number(data.lineCount || 0);
            return {
                headline: latest.op === 'list_files' ? `${items.length} entries` : `${lineCount || 0} lines`,
                summary: path || String(latest.message || 'File surface'),
                variant: 'result',
                kind: 'files',
                theme: 'theme-files',
                info: { ...data, items, excerpt, path, lineCount, op: latest.op },
            };
        }
        if (latest.op === 'social_feed_read' || latest.op === 'social_message_send') {
            const info = this.parsePreviewMap(latest.previewLines);
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const source = String(data.source || info.source || 'scaffold').trim();
            const items = Array.isArray(data.items) ? data.items : [];
            const delivery = String(data.delivery || info.delivery || '').trim();
            const message = String(data.message || info.message || '').trim();
            if (latest.op === 'social_feed_read') {
                return {
                    headline: `${items.length} social cards`,
                    summary: `source: ${source}`,
                    variant: 'result',
                    kind: 'social',
                    theme: 'theme-social',
                    info: { source, items, op: latest.op },
                };
            }
            return {
                headline: delivery ? `message ${delivery}` : 'message queued',
                summary: message.slice(0, 120) || latest.message || 'Social update',
                variant: 'result',
                kind: 'social',
                theme: 'theme-social',
                info: { source, items, message, delivery, op: latest.op },
            };
        }
        if (['social_notifications','social_trending','social_profile_read',
             'social_dm_send','social_react','social_comment','social_follow'].includes(latest.op)) {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const op = latest.op;
            if (op === 'social_notifications') {
                const items = Array.isArray(data.items) ? data.items : [];
                const unread = Number(data.unread || 0);
                return {
                    headline: unread > 0 ? `${unread} unread` : `${items.length} notifications`,
                    summary: `${items.length} total · bluesky`,
                    variant: 'result', kind: 'social', theme: 'theme-social',
                    info: { items, unread, op },
                };
            }
            if (op === 'social_trending') {
                const topics = Array.isArray(data.topics) ? data.topics : [];
                return {
                    headline: `${topics.length} trending`,
                    summary: topics.slice(0, 3).map(t => String(t.topic || '')).filter(Boolean).join(' · ') || 'bluesky trending',
                    variant: 'result', kind: 'social', theme: 'theme-social',
                    info: { topics, op },
                };
            }
            if (op === 'social_profile_read') {
                const handle = String(data.handle || data.actor || '');
                const displayName = String(data.displayName || handle);
                const followers = Number(data.followersCount || 0);
                const following = Number(data.followsCount || 0);
                const posts = Number(data.postsCount || 0);
                const bio = String(data.description || '').slice(0, 200);
                return {
                    headline: displayName || handle,
                    summary: `${followers.toLocaleString()} followers · ${posts} posts`,
                    variant: 'result', kind: 'social', theme: 'theme-social',
                    info: { handle, displayName, followers, following, posts, bio, op },
                };
            }
            if (op === 'social_dm_send') {
                const recipient = String(data.recipient || '');
                const convoId = String(data.convoId || '');
                return {
                    headline: `DM sent`,
                    summary: `to ${recipient}`,
                    variant: 'result', kind: 'social', theme: 'theme-social',
                    info: { recipient, convoId, action: 'dm_sent', op },
                };
            }
            if (op === 'social_react') {
                const uri = String(data.uri || '');
                return {
                    headline: 'liked',
                    summary: uri.slice(0, 80) || 'post liked',
                    variant: 'result', kind: 'social', theme: 'theme-social',
                    info: { uri, action: 'liked', op },
                };
            }
            if (op === 'social_comment') {
                const uri = String(data.uri || '');
                return {
                    headline: 'reply posted',
                    summary: uri.slice(0, 80) || 'reply sent',
                    variant: 'result', kind: 'social', theme: 'theme-social',
                    info: { uri, action: 'replied', op },
                };
            }
            if (op === 'social_follow') {
                const actor = String(data.actor || '');
                const action = String(data.action || 'followed');
                return {
                    headline: action,
                    summary: actor,
                    variant: 'result', kind: 'social', theme: 'theme-social',
                    info: { actor, action, op },
                };
            }
        }
        if (latest.op === 'banking_balance_read' || latest.op === 'banking_transactions_read') {
            const info = this.parsePreviewMap(latest.previewLines);
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const source = String(data.source || info.source || 'scaffold').trim();
            const items = Array.isArray(data.items) ? data.items : [];
            const availableRaw = data.available ?? info.available ?? '0';
            const ledgerRaw = data.ledger ?? info.ledger ?? '0';
            const currency = String(data.currency || info.currency || 'USD').trim();
            const available = Number(String(availableRaw).replace(/[^0-9.-]/g, '')) || 0;
            const ledger = Number(String(ledgerRaw).replace(/[^0-9.-]/g, '')) || available;
            const asOf = String(data.asOf || info['as of'] || '').trim();
            return {
                headline: `${formatCurrency(available)} available`,
                summary: `${currency} | source: ${source}${asOf ? ` | ${asOf}` : ''}`,
                variant: 'result',
                kind: 'banking',
                theme: 'theme-banking',
                info: { source, items, available, ledger, currency, asOf, op: latest.op },
            };
        }
        if (latest.op === 'contacts_lookup') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const items = Array.isArray(data.items) ? data.items : [];
            const source = String(data.source || 'scaffold').trim();
            const query = String(data.query || '').trim();
            return {
                headline: `${items.length} contacts`,
                summary: query ? `query: ${query}` : `source: ${source}`,
                variant: 'result',
                kind: 'contacts',
                theme: 'theme-contacts',
                info: { items, source, query, op: latest.op },
            };
        }
        if (latest.op === 'telephony_status' || latest.op === 'telephony_call_start') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const target = String(data.target || '').trim();
            const mode = String(data.mode || '').trim();
            const contactName = String(data.contactName || '').trim();
            return {
                headline: target ? `call ${target}` : 'telephony status',
                summary: [mode, contactName].filter(Boolean).join(' | ') || String(latest.message || 'Call handoff control'),
                variant: 'result',
                kind: 'telephony',
                theme: 'theme-telephony',
                info: { ...data, op: latest.op },
            };
        }
        // ── Personal data ops ─────────────────────────────────────────────────
        if (latest.op === 'graph_query') {
            const writeOps = Array.isArray(envelope?.stateIntent?.writeOperations)
                ? envelope.stateIntent.writeOperations : [];
            const gOp = writeOps.find((o) => String(o?.type || '') === 'graph_query');
            const qKind = String(gOp?.payload?.kind || '').trim().toLowerCase();
            if (qKind === 'task') {
                const tasks = this.state.memory?.tasks || [];
                const open = tasks.filter((t) => !t.done).length;
                return { headline: `${open} open task${open !== 1 ? 's' : ''}`, summary: `${tasks.length} total`, variant: 'result', kind: 'tasks', theme: 'theme-tasks' };
            }
            if (qKind === 'note') {
                const notes = this.state.memory?.notes || [];
                return { headline: `${notes.length} note${notes.length !== 1 ? 's' : ''}`, summary: 'knowledge stream', variant: 'result', kind: 'notes', theme: 'theme-notes' };
            }
            if (qKind === 'expense') {
                const expenses = this.state.memory?.expenses || [];
                const total = expenses.reduce((s, e) => s + Number(e.amount || 0), 0);
                return { headline: `${formatCurrency(total)} tracked`, summary: `${expenses.length} expense${expenses.length !== 1 ? 's' : ''}`, variant: 'result', kind: 'expenses', theme: 'theme-expenses' };
            }
        }
        if (latest.op === 'add_task' || latest.op === 'toggle_task' ||
            latest.op === 'delete_task' || latest.op === 'clear_completed') {
            const tasks = this.state.memory?.tasks || [];
            const open = tasks.filter((t) => !t.done).length;
            const msg = String(latest.message || '').trim();
            return { headline: msg || 'Tasks updated', summary: `${open} open`, variant: 'result', kind: 'tasks', theme: 'theme-tasks' };
        }
        if (latest.op === 'add_note') {
            const notes = this.state.memory?.notes || [];
            const msg = String(latest.message || '').trim();
            return { headline: msg || 'Note saved', summary: `${notes.length} note${notes.length !== 1 ? 's' : ''}`, variant: 'result', kind: 'notes', theme: 'theme-notes' };
        }
        if (latest.op === 'add_expense') {
            const expenses = this.state.memory?.expenses || [];
            const total = expenses.reduce((s, e) => s + Number(e.amount || 0), 0);
            const msg = String(latest.message || '').trim();
            return { headline: msg || 'Expense logged', summary: `${formatCurrency(total)} total`, variant: 'result', kind: 'expenses', theme: 'theme-expenses' };
        }
        if (latest.op === 'schedule_remind_once' || latest.op === 'list_reminders' ||
            latest.op === 'cancel_reminder' || latest.op === 'pause_reminder' ||
            latest.op === 'resume_reminder') {
            const lines = Array.isArray(latest.previewLines) ? latest.previewLines : [];
            const msg = String(latest.message || '').trim();
            return {
                headline: msg || 'Reminders',
                summary: lines.length ? String(lines[0]).slice(0, 80) : 'Reminder status',
                variant: 'result',
                kind: 'reminders',
                theme: 'theme-reminders',
                info: { lines, op: latest.op },
            };
        }
        const summary = String(latest.message || fallbackSummary);
        if (domain === 'tasks') {
            return { headline: 'Task Flow', summary: 'Generated task workspace', variant: 'result', kind: 'tasks', theme: 'theme-tasks' };
        }
        if (domain === 'expenses') {
            return { headline: 'Spend Pulse', summary: 'Generated expense workspace', variant: 'result', kind: 'expenses', theme: 'theme-expenses' };
        }
        if (domain === 'notes') {
            return { headline: 'Knowledge Stream', summary: 'Generated notes workspace', variant: 'result', kind: 'notes', theme: 'theme-notes' };
        }
        if (domain === 'graph') {
            return { headline: 'System Graph', summary: 'Generated relation workspace', variant: 'result', kind: 'graph', theme: 'theme-graph' };
        }
        if (domain === 'content') {
            return { headline: 'Content Repo', summary: 'Repository objects and history', variant: 'result', kind: 'content', theme: 'theme-content', info: { action: 'list', items: [], branch: this.state.session.workspace?.branch || 'main' } };
        }
        if (domain === 'files') {
            return { headline: 'Workspace Files', summary: 'External filesystem workspace', variant: 'result', kind: 'files', theme: 'theme-files' };
        }
        if (latest.op === 'mcp_tool_call') {
            const serverName = String(latest.serverName || latest.data?.serverName || 'Connected App').trim();
            const toolName   = String(latest.toolName   || latest.data?.toolName   || '').trim();
            const content    = Array.isArray(latest.data?.content) ? latest.data.content : [];
            const textItems  = content.filter(c => c.type === 'text').map(c => String(c.text || ''));
            const imageItems = content.filter(c => c.type === 'image');
            const headline   = serverName;
            const summaryLine = toolName || 'tool response';
            return {
                kind: 'mcp',
                headline,
                summary: summaryLine,
                variant: 'result',
                theme: 'theme-mcp',
                info: { serverName, toolName, content, textItems, imageItems, matchScore: latest.matchScore || 0 },
            };
        }
        // ── Music / Spotify ────────────────────────────────────────────────────
        {
            const _musicOps = new Set(['music_play','music_pause','music_skip','music_queue',
                'music_volume','music_like','music_playlist_add','music_playlist_create',
                'music_radio','music_discover','music_lyrics','music_cast','music_sleep_timer',
                'spotify.now_playing','spotify.play','spotify.pause','spotify.next','spotify.prev',
                'spotify.queue','spotify.volume']);
            if (_musicOps.has(latest.op)) {
                const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                if (d.connected === false || d.fallbackReason === 'no_active_device') {
                    const summary = d.connected === false
                        ? 'Connect Spotify to control playback'
                        : (d.error || 'Open Spotify on an active device');
                    return {
                        headline: 'Spotify',
                        summary,
                        variant: 'result',
                        kind: 'music',
                        theme: 'theme-music',
                        info: {
                            notConnected: true,
                            service: 'spotify',
                            source: d.source || 'live',
                            fallbackReason: d.fallbackReason || '',
                            error: d.error || '',
                            op: latest.op,
                        },
                    };
                }
                const track   = String(d.track  || '').trim();
                const artist  = String(d.artist || '').trim();
                const album   = String(d.album  || '').trim();
                const playing = d.is_playing !== false;
                const src     = String(d.source || 'scaffold');
                const headline = track || (playing ? 'Now Playing' : 'Paused');
                const summary  = artist ? `${artist}${album ? ' · ' + album : ''}` : (src === 'scaffold' ? 'Spotify scaffold' : 'Spotify');
                return { headline, summary, variant: 'result', kind: 'music', theme: 'theme-music',
                         info: { track, artist, album, is_playing: playing, album_art: d.album_art || '',
                                 progress_ms: d.progress_ms || 0, duration_ms: d.duration_ms || 0, source: src, op: latest.op } };
            }
        }
        // ── Gmail / Email with live data ───────────────────────────────────────
        {
            const _emailOps = new Set(['gmail.list','gmail.search','gmail.send','gmail.trash']);
            if (_emailOps.has(latest.op) || (['email_read','email_search'].includes(latest.op) && Array.isArray(latest.data?.messages))) {
                const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const provider = String(d.provider || (latest.op?.startsWith('gmail.') ? 'gmail' : 'email')).toLowerCase();
                const providerLabel = provider === 'email' ? 'Email' : provider.charAt(0).toUpperCase() + provider.slice(1);
                console.log('[EMAIL] buildCoreSurface op=', latest.op, 'd=', d, 'provider=', provider);
                if (d.connected === false) {
                    return { headline: providerLabel, summary: 'Connect to view inbox', variant: 'result', kind: 'email', theme: 'theme-email',
                             info: { messages: [], unread_count: 0, notConnected: true, service: provider, op: latest.op, provider, source: d.source || 'live', fallbackReason: d.fallbackReason || '', error: d.error || '' } };
                }
                const messages = Array.isArray(d.messages) ? d.messages : [];
                const unread   = Number(d.unread_count || messages.filter(m => m.unread).length);
                const first    = messages[0] || {};
                const headline = unread > 0 ? `${unread} unread` : 'Inbox';
                const summary  = d.ok === false ? (d.error || `${providerLabel} unavailable`) : (first.subject ? first.subject.slice(0, 60) : (d.source === 'scaffold' ? `${providerLabel} scaffold` : providerLabel));
                return { headline, summary, variant: 'result', kind: 'email', theme: 'theme-email',
                         info: { messages, unread_count: unread, source: d.source || 'scaffold', query: d.query || '', op: latest.op, provider, authoritative: d.authoritative !== false, error: d.error || '' } };
            }
        }
        // ── Google Calendar with live data ─────────────────────────────────────
        {
            const _calOps = new Set(['gcal.list','gcal.create','gcal.delete']);
            const _calEnriched = ['calendar_list','calendar_availability'].includes(latest.op) && Array.isArray(latest.data?.events);
            if (_calOps.has(latest.op) || _calEnriched) {
                const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                if (d.connected === false) {
                    return { headline: 'Google Calendar', summary: 'Connect to view events', variant: 'result', kind: 'calendar', theme: 'theme-calendar',
                             info: { events: [], notConnected: true, service: 'gcal', op: latest.op, source: d.source || 'live', fallbackReason: d.fallbackReason || '', error: d.error || '' } };
                }
                const events  = Array.isArray(d.events) ? d.events : [];
                const next    = events[0] || null;
                const headline = next ? (next.summary || 'Event').slice(0, 50) : (events.length + ' events');
                const summary  = d.ok === false ? (d.error || 'Calendar unavailable') : (next ? String(next.start || '').slice(0, 16) : (d.source === 'scaffold' ? 'Calendar scaffold' : 'Google Calendar'));
                return { headline: events.length ? headline : 'No upcoming events', summary,
                         variant: 'result', kind: 'calendar', theme: 'theme-calendar',
                         info: { events, source: d.source || 'scaffold', op: latest.op, authoritative: d.authoritative !== false, error: d.error || '' } };
            }
        }
        // ── Google Drive with live data ────────────────────────────────────────
        {
            const _driveOps = new Set(['gdrive.list','gdrive.create','gdrive.open']);
            if (_driveOps.has(latest.op)) {
                const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                if (d.connected === false) {
                    return { headline: 'Google Drive', summary: 'Connect to view files', variant: 'result', kind: 'drive', theme: 'theme-files',
                             info: { files: [], notConnected: true, service: 'gdrive', source: d.source || 'live', fallbackReason: d.fallbackReason || '', error: d.error || '', op: latest.op, storage: 'connector' } };
                }
                const files   = Array.isArray(d.files) ? d.files : [];
                const first   = files[0] || {};
                const headline = first.name ? first.name.slice(0, 50) : `${files.length} files`;
                const summary  = d.ok === false ? (d.error || 'Drive unavailable') : (files.length > 1 ? `${files.length} files in Drive` : (d.source === 'scaffold' ? 'Drive scaffold' : 'Google Drive'));
                return { headline, summary, variant: 'result', kind: 'drive', theme: 'theme-files',
                         info: { files, items: files, source: d.source || 'scaffold', op: latest.op, authoritative: d.authoritative !== false, error: d.error || '', service: 'gdrive', storage: 'connector' } };
            }
        }
        // ── Slack / Messaging with live data ───────────────────────────────────
        {
            const _slackOps = new Set(['slack.list_channels','slack.send','slack.read',
                'slack_send','slack_read','slack_search','slack_status','slack_reaction',
                'messaging_send','messaging_read','messaging_reply','messaging_search']);
            if (_slackOps.has(latest.op)) {
                const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                if (d.connected === false) {
                    return { headline: 'Slack', summary: 'Connect to view channels', variant: 'result', kind: 'messaging', theme: 'theme-messaging',
                             info: { channels: [], messages: [], unread: 0, notConnected: true, service: 'slack', source: d.source || 'live', fallbackReason: d.fallbackReason || '', error: d.error || '', op: latest.op } };
                }
                const channels = Array.isArray(d.channels) ? d.channels : [];
                const messages = Array.isArray(d.messages) ? d.messages : [];
                const unread   = channels.reduce((s, c) => s + Number(c.unread_count || 0), 0);
                const first    = channels[0] || messages[0] || {};
                const headline = channels.length ? `#${first.name || 'general'}` : (messages.length ? 'Messages' : 'Slack');
                const summary  = d.ok === false ? (d.error || 'Slack unavailable') : (unread > 0 ? `${unread} unread` : (d.source === 'scaffold' ? 'Slack scaffold' : 'Up to date'));
                return { headline, summary, variant: 'result', kind: 'messaging', theme: 'theme-messaging',
                         info: { channels, messages, unread, source: d.source || 'scaffold',
                                 channel: d.channel || '', op: latest.op, authoritative: d.authoritative !== false, error: d.error || '', service: 'slack' } };
            }
        }
        // ── GitHub ops ────────────────────────────────────────────────────────
        {
            const _ghOps = new Set(['github_my_prs','github_pr_view','github_repo_search',
                'github_issue_create','github_commit']);
            if (_ghOps.has(latest.op)) {
                const d      = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const items  = Array.isArray(d.items) ? d.items : [];
                const repo   = String(d.repo || '').trim();
                const op     = latest.op;
                if (op === 'github_issue_create') {
                    return { headline: `#${d.number || '?'} created`,
                             summary: String(d.title || '').slice(0, 50) || repo,
                             variant: 'result', kind: 'github', theme: 'theme-enterprise',
                             info: { ...d, op, items } };
                }
                const itemKind = String(d.kind || 'prs');
                const label    = itemKind === 'issues' ? 'issues' : 'PRs';
                return { headline: items.length ? `${items.length} open ${label}` : 'GitHub',
                         summary: repo ? `repo: ${repo}` : (d.source === 'scaffold' ? 'GitHub scaffold' : 'GitHub'),
                         variant: 'result', kind: 'github', theme: 'theme-enterprise',
                         info: { items, repo, kind: itemKind, source: d.source || 'scaffold', op } };
            }
        }
        // ── Jira ops ──────────────────────────────────────────────────────────
        {
            const _jiraOps = new Set(['jira_my_issues','jira_sprint','jira_view',
                'jira_create','jira_update']);
            if (_jiraOps.has(latest.op)) {
                const d      = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const issues = Array.isArray(d.issues) ? d.issues : [];
                const op     = latest.op;
                if (op === 'jira_create') {
                    return { headline: d.key || 'Created',
                             summary: String(d.summary || '').slice(0, 50),
                             variant: 'result', kind: 'jira', theme: 'theme-enterprise',
                             info: { ...d, op, issues: [] } };
                }
                if (op === 'jira_update') {
                    return { headline: d.key || 'Updated',
                             summary: String(d.status || 'status updated'),
                             variant: 'result', kind: 'jira', theme: 'theme-enterprise',
                             info: { ...d, op, issues: [] } };
                }
                const view = String(d.view || op).replace('jira_', '').replace(/_/g, ' ');
                return { headline: `${issues.length} issues`, summary: view,
                         variant: 'result', kind: 'jira', theme: 'theme-enterprise',
                         info: { issues, view, source: d.source || 'scaffold', op } };
            }
        }
        // ── Notion ops ────────────────────────────────────────────────────────
        {
            const _notionOps = new Set(['notion_find','notion_create','notion_database','notion_update']);
            if (_notionOps.has(latest.op)) {
                const d     = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const pages = Array.isArray(d.pages) ? d.pages : [];
                const op    = latest.op;
                if (op === 'notion_create' || op === 'notion_update') {
                    const title = String(d.title || '').slice(0, 50) || 'page';
                    return { headline: op === 'notion_create' ? 'Page created' : 'Page updated',
                             summary: title,
                             variant: 'result', kind: 'notion', theme: 'theme-enterprise',
                             info: { ...d, op, pages: [] } };
                }
                const query = String(d.query || '');
                return { headline: `${pages.length} pages`,
                         summary: query || 'notion workspace',
                         variant: 'result', kind: 'notion', theme: 'theme-enterprise',
                         info: { pages, query, source: d.source || 'scaffold', op } };
            }
        }
        // ── Asana ops ─────────────────────────────────────────────────────────
        {
            const _asanaOps = new Set(['asana_my_tasks','asana_create','asana_project','asana_update']);
            if (_asanaOps.has(latest.op)) {
                const d     = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const tasks = Array.isArray(d.tasks) ? d.tasks : [];
                const op    = latest.op;
                if (op === 'asana_create' || op === 'asana_update') {
                    const name = String(d.name || '').slice(0, 50) || 'task';
                    return { headline: op === 'asana_create' ? 'Task created' : 'Task updated',
                             summary: name,
                             variant: 'result', kind: 'asana', theme: 'theme-enterprise',
                             info: { ...d, op, tasks: [] } };
                }
                const open = tasks.filter(t => !t.completed).length;
                return { headline: `${tasks.length} tasks`, summary: `${open} open`,
                         variant: 'result', kind: 'asana', theme: 'theme-enterprise',
                         info: { tasks, source: d.source || 'scaffold', op } };
            }
        }
        // ── Alarm / Clock ops ─────────────────────────────────────────────────
        {
            const _alarmOps = new Set(['alarm_set','alarm_delete','alarm_list','alarm_snooze',
                'clock_timer_start','clock_timer_stop','clock_stopwatch']);
            if (_alarmOps.has(latest.op)) {
                const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const alarms = Array.isArray(d.alarms) ? d.alarms : [];
                const time   = String(d.time || (alarms[0] && alarms[0].time) || '').trim();
                const label  = String(d.label || (alarms[0] && alarms[0].label) || 'Alarm').trim();
                const action = String(d.action || '').trim();
                const headline = time ? `${time}` : (action === 'list' ? `${alarms.length} alarms` : 'Alarm');
                return { headline, summary: label || action, variant: 'result', kind: 'alarm', theme: 'theme-alarm',
                         info: { alarms, time, label, action, source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Health / Fitness ops ───────────────────────────────────────────────
        {
            const _healthOps = new Set(['health_activity','health_log_workout','health_log_weight',
                'health_log_water','health_log_sleep','health_history',
                'health_steps','health_heart_rate','health_sleep','health_workout_log','health_workout_start',
                'health_food_log','health_water','health_weight','health_goals','health_mood',
                'health_medication','health_hrv','health_cycle','health_streak']);
            if (_healthOps.has(latest.op)) {
                const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const metrics = (d.metrics && typeof d.metrics === 'object') ? d.metrics : {};
                const hero    = String(d.hero || '');
                const sumLine = String(d.summary_line || '');
                const steps   = Number(metrics.steps || 0);
                const goal    = Number(metrics.steps_goal || 10000);
                const headline = hero || (steps ? `${steps.toLocaleString()}` : 'Activity');
                const summary  = sumLine || (goal ? `of ${goal.toLocaleString()} steps` : 'health tracking');
                return { headline, summary, variant: 'result', kind: 'health', theme: 'theme-health',
                         info: { metrics, hero, summary_line: sumLine, source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Podcast ops ────────────────────────────────────────────────────────
        {
            const _podcastOps = new Set(['podcast_play','podcast_pause','podcast_next',
                'podcast_list','podcast_subscribe','podcast_search']);
            if (_podcastOps.has(latest.op)) {
                const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const show    = String(d.show    || '').trim();
                const episode = String(d.episode || '').trim();
                return { headline: episode || show || 'Podcast', summary: show || 'Now listening', variant: 'result',
                         kind: 'podcast', theme: 'theme-podcast',
                         info: { ...d, source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Video streaming ops ────────────────────────────────────────────────
        {
            const _vidOps = new Set(['video.play','video.search','video.watchlist','video.browse',
                'video.recommend','video.cast','video.continue','video.rate']);
            if (_vidOps.has(latest.op)) {
                const d       = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const title   = String(d.title   || '').trim();
                const show    = String(d.show    || '').trim();
                const service = String(d.service || '').trim();
                const action  = String(d.action  || latest.op.replace('video.', '')).trim();
                const results = Array.isArray(d.results) ? d.results : [];
                const headline = title || show || (results[0]?.title) || 'Video';
                const summary  = service ? `${service} · ${action}` : action;
                return { headline, summary, variant: 'result', kind: 'video', theme: 'theme-video',
                         info: { title, show, service, action, results,
                                 progress_ms: d.progress_ms || 0, duration_ms: d.duration_ms || 0,
                                 thumbnail: d.thumbnail || '', source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Food delivery ops ──────────────────────────────────────────────────
        {
            const _foodOps = new Set(['food_delivery.order','food_delivery.track',
                'food_delivery.reorder','food_delivery.browse']);
            if (_foodOps.has(latest.op)) {
                const d          = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const restaurant = String(d.restaurant || '').trim();
                const eta        = String(d.eta        || '').trim();
                const status     = String(d.status     || d.action || 'order').trim();
                const items      = Array.isArray(d.items) ? d.items : [];
                const headline   = restaurant || 'Food Delivery';
                const summary    = eta ? `ETA ${eta}` : status;
                return { headline, summary, variant: 'result', kind: 'food_delivery', theme: 'theme-food',
                         info: { restaurant, eta, status, items, total: d.total || 0,
                                 source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Rideshare ops ──────────────────────────────────────────────────────
        {
            const _rideOps = new Set(['rideshare.book','rideshare.track','rideshare.cancel',
                'rideshare.estimate','rideshare.history']);
            if (_rideOps.has(latest.op)) {
                const d       = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const dest    = String(d.destination || d.to || '').trim();
                const driver  = String(d.driver      || '').trim();
                const eta     = String(d.eta         || '').trim();
                const status  = String(d.status      || d.action || 'booking').trim();
                const price   = d.price ? `$${Number(d.price).toFixed(2)}` : '';
                const headline = dest || 'Ride';
                const summary  = driver ? `${driver}${eta ? ' · ' + eta : ''}` : (eta ? `ETA ${eta}` : status);
                return { headline, summary, variant: 'result', kind: 'rideshare', theme: 'theme-rideshare',
                         info: { dest, driver, eta, status, price,
                                 vehicle: d.vehicle || '', source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Camera ops ────────────────────────────────────────────────────────
        {
            const _camOps = new Set(['camera.photo','camera.video','camera.scan',
                'camera.selfie','camera.settings','camera_open','camera_capture']);
            if (_camOps.has(latest.op)) {
                const d      = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const mode   = String(d.mode   || latest.op.replace('camera.', '')).trim() || 'photo';
                const action = String(d.action || '').trim();
                return { headline: mode === 'scan' ? 'Scanner' : mode === 'video' ? 'Video' : 'Camera',
                         summary: action || mode, variant: 'result', kind: 'camera', theme: 'theme-camera',
                         info: { mode, action, source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Photos ops ────────────────────────────────────────────────────────
        {
            const _photoOps = new Set(['photos.browse','photos.search','photos.album',
                'photos.share','photos.edit','photos.delete','photos_open','photos_search']);
            if (_photoOps.has(latest.op)) {
                const d       = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const count   = Number(d.count  || (Array.isArray(d.photos) ? d.photos.length : 0));
                const album   = String(d.album  || '').trim();
                const query   = String(d.query  || '').trim();
                const headline = album || query || (count ? `${count} photos` : 'Photos');
                const summary  = count ? `${count} photos` : 'photo library';
                return { headline, summary, variant: 'result', kind: 'photos', theme: 'theme-photos',
                         info: { count, album, query, photos: d.photos || [],
                                 source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Clock / Timer ops ──────────────────────────────────────────────────
        {
            const _clockOps = new Set(['clock.timer','clock.stopwatch','clock.world_time',
                'clock_timer_start','clock_timer_stop','clock_stopwatch',
                'clock_world_time','clock_countdown',
                'clock_world','clock_bedtime','alarm_list',
                'date_age','date_countdown','date_day_of','date_days_until']);
            if (_clockOps.has(latest.op)) {
                const d   = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const op  = latest.op;
                // world clocks
                if (op === 'clock_world') {
                    const clocks = Array.isArray(d.clocks) ? d.clocks : [];
                    return { headline: `${clocks.length} zones`, summary: clocks.slice(0,3).map(c => c.city).join(' · ') || 'world time',
                             variant: 'result', kind: 'clock', theme: 'theme-clock',
                             info: { clocks, op } };
                }
                // bedtime
                if (op === 'clock_bedtime') {
                    const bedtimes = Array.isArray(d.bedtimes) ? d.bedtimes : [];
                    const best = bedtimes[1] || bedtimes[0] || {};
                    return { headline: best.time || 'Bedtime', summary: `${best.cycles || 5} cycles · ${best.hours || 7.5}h`,
                             variant: 'result', kind: 'clock', theme: 'theme-clock',
                             info: { bedtimes, wake_time: d.wake_time || '', op } };
                }
                // alarm list
                if (op === 'alarm_list') {
                    const alarms = Array.isArray(d.alarms) ? d.alarms : [];
                    const next = alarms[0] || {};
                    return { headline: next.time || `${alarms.length} alarms`, summary: next.label || 'alarms',
                             variant: 'result', kind: 'alarm', theme: 'theme-alarm',
                             info: { alarms, time: next.time || '', label: next.label || '', action: 'list', op } };
                }
                // date calc
                if (['date_age','date_countdown','date_day_of','date_days_until'].includes(op)) {
                    const delta  = d.delta_days != null ? Number(d.delta_days) : null;
                    const dayName = String(d.day_name || '');
                    const ageYears = d.age_years != null ? Number(d.age_years) : null;
                    const headline = ageYears != null ? `${ageYears} years old`
                                   : dayName   ? dayName
                                   : delta != null ? `${Math.abs(delta)} days`
                                   : latest.message || 'Date';
                    const summary  = d.date || d.target || '';
                    return { headline, summary, variant: 'result', kind: 'clock', theme: 'theme-clock',
                             info: { ...d, op } };
                }
                const dur      = Number(d.duration_ms || d.remaining_ms || 0);
                const elapsed  = Number(d.elapsed_ms  || 0);
                const mode     = String(d.mode || (op.includes('stopwatch') ? 'stopwatch' : 'timer')).trim();
                const label    = String(d.label || '').trim();
                const fmtMs    = (ms) => { const s = Math.floor(ms/1000); const m = Math.floor(s/60); const h = Math.floor(m/60); return h > 0 ? `${h}:${String(m%60).padStart(2,'0')}:${String(s%60).padStart(2,'0')}` : `${m}:${String(s%60).padStart(2,'0')}`; };
                const headline = dur ? fmtMs(dur) : (elapsed ? fmtMs(elapsed) : (mode === 'stopwatch' ? '0:00' : 'Timer'));
                const summary  = label || mode;
                return { headline, summary, variant: 'result', kind: 'clock', theme: 'theme-clock',
                         info: { mode, label, duration_ms: dur, elapsed_ms: elapsed,
                                 running: Boolean(d.running), source: d.source || 'scaffold', op } };
            }
        }
        // ── Reference / Dictionary / Currency ops ─────────────────────────────
        {
            const _refOps = new Set(['dict_define','dict_etymology','dict_thesaurus','dict_wikipedia',
                'currency_rates','currency_convert','unit_convert']);
            if (_refOps.has(latest.op)) {
                const d  = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const op = latest.op;
                if (op === 'dict_define' || op === 'dict_etymology') {
                    const word     = String(d.word || '');
                    const phonetic = String(d.phonetic || '');
                    const meanings = Array.isArray(d.meanings) ? d.meanings : [];
                    const origin   = String(d.origin || '');
                    const firstDef = String(d.first_def || (meanings[0]?.definitions?.[0]) || '');
                    return { headline: word, summary: phonetic || firstDef.slice(0,60) || op,
                             variant: 'result', kind: 'reference', theme: 'theme-reference',
                             info: { word, phonetic, meanings, origin, op } };
                }
                if (op === 'dict_thesaurus') {
                    const word     = String(d.word || '');
                    const synonyms = Array.isArray(d.synonyms) ? d.synonyms : [];
                    return { headline: word, summary: synonyms.slice(0,4).join(', ') || 'thesaurus',
                             variant: 'result', kind: 'reference', theme: 'theme-reference',
                             info: { word, synonyms, antonyms: d.antonyms || [], op } };
                }
                if (op === 'dict_wikipedia') {
                    const title   = String(d.title || '');
                    const desc    = String(d.description || '');
                    const extract = String(d.extract || '');
                    return { headline: title, summary: desc || extract.slice(0,80),
                             variant: 'result', kind: 'reference', theme: 'theme-reference',
                             info: { title, desc, extract, thumbnail: d.thumbnail || '', url: d.url || '', op } };
                }
                if (op === 'currency_rates') {
                    const base  = String(d.base || 'USD');
                    const major = (d.major && typeof d.major === 'object') ? d.major : {};
                    const pairs = Object.entries(major).slice(0, 4).map(([k,v]) => `${k} ${Number(v).toFixed(3)}`).join(' · ');
                    return { headline: `${base} rates`, summary: pairs || 'exchange rates',
                             variant: 'result', kind: 'reference', theme: 'theme-reference',
                             info: { base, rates: d.rates || {}, major, op } };
                }
                if (op === 'currency_convert') {
                    const result = Number(d.result || 0);
                    const from_c = String(d.from || '');
                    const to_c   = String(d.to   || '');
                    const amount = Number(d.amount || 1);
                    return { headline: `${result.toLocaleString(undefined,{maximumFractionDigits:2})} ${to_c}`,
                             summary: `${amount} ${from_c} → ${to_c}`,
                             variant: 'result', kind: 'reference', theme: 'theme-reference',
                             info: { amount, from: from_c, to: to_c, result, rate: d.rate || 0, op } };
                }
                if (op === 'unit_convert') {
                    const result = Number(d.result || 0);
                    const from_u = String(d.from || '');
                    const to_u   = String(d.to   || '');
                    const value  = Number(d.value || 1);
                    return { headline: `${result} ${to_u}`, summary: `${value} ${from_u} → ${to_u}`,
                             variant: 'result', kind: 'reference', theme: 'theme-reference',
                             info: { value, from: from_u, to: to_u, result, category: d.category || '', op } };
                }
            }
        }
        // ── Recipe ops ────────────────────────────────────────────────────────
        {
            const _recipeOps = new Set(['recipe.search','recipe.view','recipe.save','recipe.cook',
                'recipe_search','recipe_view','recipe_save']);
            if (_recipeOps.has(latest.op)) {
                const d        = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const name     = String(d.name     || d.title || '').trim();
                const cuisine  = String(d.cuisine  || '').trim();
                const time_min = Number(d.time_min || d.cook_time_min || 0);
                const results  = Array.isArray(d.results) ? d.results : [];
                const headline = name || (results[0]?.name) || 'Recipe';
                const summary  = [cuisine, time_min ? `${time_min} min` : ''].filter(Boolean).join(' · ') || 'cooking';
                return { headline, summary, variant: 'result', kind: 'recipe', theme: 'theme-recipe',
                         info: { name, cuisine, time_min, servings: d.servings || 0,
                                 ingredients: d.ingredients || [], results,
                                 source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Grocery ops ───────────────────────────────────────────────────────
        {
            const _grocOps = new Set(['grocery.add','grocery.list','grocery.remove',
                'grocery.check','grocery_add','grocery_list','grocery_check']);
            if (_grocOps.has(latest.op)) {
                const d      = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const items  = Array.isArray(d.items) ? d.items : [];
                const done   = items.filter(it => it.checked || it.done).length;
                const action = String(d.action || '').trim();
                const headline = `${items.length} item${items.length !== 1 ? 's' : ''}`;
                const summary  = done > 0 ? `${done} checked` : (action || 'grocery list');
                return { headline, summary, variant: 'result', kind: 'grocery', theme: 'theme-grocery',
                         info: { items, done, action, source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Translate ops ──────────────────────────────────────────────────────
        {
            const _transOps = new Set(['translate.text','translate.phrase','translate.detect',
                'translate_text','translate_phrase','translate_detect']);
            if (_transOps.has(latest.op)) {
                const d      = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const src    = String(d.source_text   || d.text   || '').trim();
                const result = String(d.translated    || d.result || '').trim();
                const from   = String(d.from_language || d.from   || '').trim();
                const to     = String(d.to_language   || d.to     || '').trim();
                const headline = result || src || 'Translation';
                const summary  = from && to ? `${from} → ${to}` : (from || to || 'translate');
                return { headline, summary, variant: 'result', kind: 'translate', theme: 'theme-translate',
                         info: { src, result, from, to, source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Books ops ─────────────────────────────────────────────────────────
        {
            const _bookOps = new Set(['book.search','book.read','book.library',
                'book.recommend','book_search','book_read','book_library']);
            if (_bookOps.has(latest.op)) {
                const d        = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const title    = String(d.title   || '').trim();
                const author   = String(d.author  || '').trim();
                const progress = Number(d.progress_pct || 0);
                const results  = Array.isArray(d.results) ? d.results : [];
                const headline = title || (results[0]?.title) || 'Books';
                const summary  = author || (progress ? `${progress}% complete` : 'reading');
                return { headline, summary, variant: 'result', kind: 'book', theme: 'theme-book',
                         info: { title, author, progress, results,
                                 source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Smarthome ops ─────────────────────────────────────────────────────
        {
            const _shOps = new Set(['smarthome.lights','smarthome.thermostat','smarthome.lock',
                'smarthome.appliance','smarthome.scene','smarthome.camera','smarthome.energy']);
            if (_shOps.has(latest.op)) {
                const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const devices = Array.isArray(d.devices) ? d.devices : [];
                const action  = String(d.action || '').trim();
                const active  = devices.filter(dv => dv.state === 'on' || dv.state === 'locked').length;
                const temp    = Number(d.temp || (devices.find(dv => dv.type === 'thermostat')?.temp) || 72);
                const headline = action === 'thermostat' ? `${temp}°` : (active > 0 ? `${active} on` : 'Home');
                const summary  = devices.length ? `${devices.length} devices` : 'smart home';
                return { headline, summary, variant: 'result', kind: 'smarthome', theme: 'theme-smarthome',
                         info: { devices, action, temp, unit: d.unit || 'F', source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Travel ops ────────────────────────────────────────────────────────
        {
            const _tvOps = new Set(['travel.flight_search','travel.flight_status','travel.itinerary',
                'travel.hotel_search','travel.hotel_book','travel.checkin','travel.car_rental','travel.boarding_pass']);
            if (_tvOps.has(latest.op)) {
                const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const flights = Array.isArray(d.flights) ? d.flights : [];
                const hotels  = Array.isArray(d.hotels)  ? d.hotels  : [];
                const first   = flights[0] || {};
                const headline = first.dest || first.origin
                    ? `${first.origin || '?'} → ${first.dest || '?'}`
                    : (hotels[0]?.name || 'Travel');
                const summary  = first.airline
                    ? `${first.airline}${first.depart ? ' · ' + first.depart : ''}`
                    : (first.status || (hotels.length ? `${hotels.length} hotels` : 'upcoming trip'));
                return { headline, summary, variant: 'result', kind: 'travel', theme: 'theme-travel',
                         info: { flights, hotels, action: d.action || latest.op, source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Payments ops ──────────────────────────────────────────────────────
        {
            const _pmOps = new Set(['payments.send','payments.request','payments.split',
                'payments.history','payments.balance']);
            if (_pmOps.has(latest.op)) {
                const d      = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const amount = Number(d.amount || 0);
                const heroAmt = amount ? `$${amount.toFixed(2)}` : (d.balance ? `$${Number(d.balance).toFixed(2)}` : '—');
                const headline = heroAmt;
                const summary  = d.recipient ? `→ ${d.recipient}` : String(d.action || 'payment').replace(/_/g, ' ');
                return { headline, summary, variant: 'result', kind: 'payments', theme: 'theme-payments',
                         info: { ...d, source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Focus ops ─────────────────────────────────────────────────────────
        {
            const _focusOps = new Set(['focus.pomodoro','focus.session',
                'focus_start','focus_end','focus_schedule','focus_status','focus_apps']);
            if (_focusOps.has(latest.op)) {
                const d   = (latest.data && typeof latest.data === 'object') ? latest.data : {};
                const dur = Number(d.duration_min || 25);
                const mode = String(d.mode || latest.op.replace('focus.', '')).trim();
                const headline = dur ? `${dur}m` : 'Focus';
                const summary  = mode || 'deep work';
                return { headline, summary, variant: 'result', kind: 'focus', theme: 'theme-focus',
                         info: { ...d, source: d.source || 'scaffold', op: latest.op } };
            }
        }
        // ── Connections management ─────────────────────────────────────────────
        if (latest.op === 'connections_status') {
            const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const services = (d.services && typeof d.services === 'object') ? d.services : {};
            const grants = Array.isArray(d.grants) ? d.grants : [];
            const grantsSummary = (d.grantsSummary && typeof d.grantsSummary === 'object') ? d.grantsSummary : {};
            const connectedCount = Object.values(services).filter(s => s && s.connected).length;
            const total = Object.keys(services).length || 6;
            const headline = 'Connections';
            const summary = connectedCount > 0 ? `${connectedCount} of ${total} connected` : 'No services connected';
            const targetService = String(d.targetService || '').trim() || null;
            return { headline, summary, variant: 'result', kind: 'connections', theme: 'theme-connections',
                     info: { services, grants, grantsSummary, op: 'connections_status', targetService } };
        }
        if (['list_connector_grants', 'grant_connector_scope', 'revoke_connector_scope'].includes(latest.op)) {
            const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const grants = Array.isArray(d.items) ? d.items : [];
            const grantsSummary = {
                count: Number(d.count || grants.length || 0),
                granted: Number(d.granted || grants.filter((item) => item?.granted).length || 0),
            };
            return {
                headline: 'Connections',
                summary: String(latest.message || 'Connector grants'),
                variant: 'result',
                kind: 'connections',
                theme: 'theme-connections',
                info: {
                    services: {},
                    grants,
                    grantsSummary,
                    op: latest.op,
                    targetService: null,
                }
            };
        }
        if (['runtime_profile', 'self_check'].includes(latest.op)) {
            const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const health = (d.health && typeof d.health === 'object') ? d.health : {};
            const selfCheck = (d.selfCheck && typeof d.selfCheck === 'object') ? d.selfCheck : {};
            const profile = (d.profile && typeof d.profile === 'object') ? d.profile : {};
            const services = (d.services && typeof d.services === 'object') ? d.services : {};
            const totalMs = Number(health.performance?.totalMs || profile.latencyMs?.avg || 0);
            const headline = totalMs > 0 ? `${totalMs}ms` : 'Runtime';
            const summary = Boolean(selfCheck.overallOk) ? 'healthy' : 'attention needed';
            return {
                headline,
                summary,
                variant: 'result',
                kind: 'runtime',
                theme: 'theme-connections',
                info: { health, selfCheck, profile, services, op: latest.op }
            };
        }
        if (['continuity_alerts', 'clear_continuity_alerts'].includes(latest.op)) {
            const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const continuity = (d.continuity && typeof d.continuity === 'object') ? d.continuity : {};
            const alerts = (d.alerts && typeof d.alerts === 'object') ? d.alerts : {};
            const handoff = (d.handoff && typeof d.handoff === 'object') ? d.handoff : {};
            const presence = (d.presence && typeof d.presence === 'object') ? d.presence : {};
            const health = (continuity.health && typeof continuity.health === 'object') ? continuity.health : {};
            return {
                headline: String(health.status || 'continuity'),
                summary: String(latest.message || 'Cross-device runtime state'),
                variant: 'result',
                kind: 'handoff',
                theme: 'theme-handoff',
                info: { continuity, alerts, handoff, presence, op: latest.op }
            };
        }
        if (['notifications_view', 'notifications_clear', 'notifications_clear_app', 'notifications_mark_read', 'notifications_settings'].includes(latest.op)) {
            const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const items = Array.isArray(d.notifications) ? d.notifications : (Array.isArray(this.state.runtimeEvents) ? this.state.runtimeEvents : []);
            const headline = items.length ? `${items.length} notifications` : 'Notifications';
            const summary = String(latest.message || 'Shell event inbox');
            return { headline, summary, variant: 'result', kind: 'notifications', theme: 'theme-notifications',
                     info: { items, op: latest.op, source: d.source || 'live' } };
        }
        // ── Computer layer ops ─────────────────────────────────────────────────
        {
            const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const action = String(d.action || '').trim();
            const name   = String(d.name   || '').trim();
            const topic  = String(d.topic  || '').trim();
            if (['document_create', 'document_edit', 'document_open'].includes(latest.op)) {
                return { headline: name || topic || (action === 'edit' ? 'Edit document' : 'New document'), summary: action || 'document', variant: 'result', kind: 'document', theme: 'theme-document', info: d };
            }
            if (['spreadsheet_create', 'spreadsheet_edit', 'spreadsheet_open'].includes(latest.op)) {
                return { headline: name || (action === 'edit' ? 'Edit spreadsheet' : 'New spreadsheet'), summary: action || 'spreadsheet', variant: 'result', kind: 'spreadsheet', theme: 'theme-spreadsheet', info: d };
            }
            if (['presentation_create', 'presentation_edit', 'presentation_open'].includes(latest.op)) {
                const slides = d.slides ? `${d.slides} slides` : '';
                return { headline: name || topic || (action === 'edit' ? 'Edit presentation' : 'New presentation'), summary: [action, slides].filter(Boolean).join(' · ') || 'presentation', variant: 'result', kind: 'presentation', theme: 'theme-presentation', info: d };
            }
            if (['code_create', 'code_edit', 'code_explain', 'code_debug', 'code_run'].includes(latest.op)) {
                const language = String(d.language || '').trim();
                return { headline: name || topic || action || 'code', summary: [language, action].filter(Boolean).join(' · ') || 'code', variant: 'result', kind: 'code', theme: 'theme-code', info: d };
            }
            if (latest.op === 'terminal_run') {
                const command = String(d.command || '').trim();
                return { headline: command || 'terminal', summary: 'shell', variant: 'result', kind: 'terminal', theme: 'theme-terminal', info: d };
            }
            if (['calendar_create', 'calendar_list', 'calendar_cancel'].includes(latest.op)) {
                const title = String(d.title || '').trim();
                const date  = String(d.date  || '').trim();
                return { headline: title || action || 'calendar', summary: date || 'calendar', variant: 'result', kind: 'calendar', theme: 'theme-calendar', info: d };
            }
            if (['email_compose', 'email_read', 'email_reply', 'email_search'].includes(latest.op)) {
                const to      = String(d.to      || '').trim();
                const subject = String(d.subject || d.query || '').trim();
                return { headline: (action === 'compose' || action === 'reply') ? (to ? `→ ${to}` : 'compose') : (subject || 'inbox'), summary: action || 'email', variant: 'result', kind: 'email', theme: 'theme-email', info: d };
            }
            if (['content_find', 'content_list', 'content_history', 'content_branch', 'content_merge', 'content_revert', 'content_worktrees', 'content_attach', 'content_activate', 'content_detach', 'content_share'].includes(latest.op)) {
                const type = String(d.type || '').trim();
                const items = Array.isArray(d.items) ? d.items : [];
                const history = Array.isArray(d.history) ? d.history : [];
                const worktrees = Array.isArray(d.worktrees) ? d.worktrees : [];
                const itemCount = action === 'history' ? history.length : items.length;
                return {
                    headline: name || (itemCount ? `${itemCount} object${itemCount === 1 ? '' : 's'}` : action || 'content'),
                    summary: [type, action, d.branch].filter(Boolean).join(' · ') || 'content',
                    variant: 'result',
                    kind: 'content',
                    theme: 'theme-content',
                    info: {
                        ...d,
                        items,
                        history,
                        worktrees,
                        source: d.source || 'live',
                        connected: d.connected !== false,
                        authoritative: d.authoritative !== false,
                    }
                };
            }
        }
        // ── Computer domain fallbacks (when latest.ok but op not specifically mapped) ──
        if (domain === 'document')     return { headline: 'Document',     summary: 'Generated document workspace',     variant: 'result', kind: 'document',     theme: 'theme-document'     };
        if (domain === 'spreadsheet')  return { headline: 'Spreadsheet',  summary: 'Generated spreadsheet workspace',  variant: 'result', kind: 'spreadsheet',  theme: 'theme-spreadsheet'  };
        if (domain === 'presentation') return { headline: 'Presentation', summary: 'Generated presentation workspace', variant: 'result', kind: 'presentation', theme: 'theme-presentation' };
        if (domain === 'code')         return { headline: 'Code',         summary: 'Generated code workspace',         variant: 'result', kind: 'code',         theme: 'theme-code'         };
        if (domain === 'terminal')     return { headline: 'Terminal',     summary: 'Shell workspace',                  variant: 'result', kind: 'terminal',     theme: 'theme-terminal'     };
        if (domain === 'calendar')     return { headline: 'Calendar',     summary: 'Generated calendar workspace',     variant: 'result', kind: 'calendar',     theme: 'theme-calendar'     };
        if (domain === 'email')        return { headline: 'Email',        summary: 'Generated email workspace',        variant: 'result', kind: 'email',        theme: 'theme-email'        };
        if (domain === 'content')      return { headline: 'Content',      summary: 'Generated content workspace',      variant: 'result', kind: 'content',      theme: 'theme-content'      };
        if (domain === 'files')        return { headline: 'Files',        summary: 'External filesystem workspace',    variant: 'result', kind: 'files',        theme: 'theme-files', info: { storage: 'workspace', path: '.', items: [] } };
        if (domain === 'drive')        return { headline: 'Drive',        summary: 'Connector file storage',          variant: 'result', kind: 'drive',        theme: 'theme-files', info: { storage: 'connector', service: 'gdrive', items: [] } };
        // ── Wave-3 domain fallbacks ──────────────────────────────────────────────────
        if (domain === 'music')         return { headline: 'Music',         summary: 'Music player',            variant: 'result', kind: 'music',         theme: 'theme-music'         };
        if (domain === 'messaging')     return { headline: 'Messages',      summary: 'Messaging',               variant: 'result', kind: 'messaging',     theme: 'theme-messaging'     };
        if (domain === 'phone')         return { headline: 'Phone',         summary: 'Phone & calls',           variant: 'result', kind: 'phone',         theme: 'theme-phone'         };
        if (domain === 'camera')        return { headline: 'Camera',        summary: 'Camera',                  variant: 'result', kind: 'camera',        theme: 'theme-camera'        };
        if (domain === 'photos')        return { headline: 'Photos',        summary: 'Photo library',           variant: 'result', kind: 'photos',        theme: 'theme-photos'        };
        if (domain === 'smarthome')     return { headline: 'Home',          summary: 'Smart home controls',    variant: 'result', kind: 'smarthome',     theme: 'theme-smarthome'     };
        if (domain === 'payments')      return { headline: 'Payments',      summary: 'Payments & transfers',   variant: 'result', kind: 'payments',      theme: 'theme-payments'      };
        if (domain === 'food_delivery') return { headline: 'Food Delivery', summary: 'Food delivery',          variant: 'result', kind: 'food_delivery', theme: 'theme-food'          };
        if (domain === 'rideshare')     return { headline: 'Ride',          summary: 'Rideshare',              variant: 'result', kind: 'rideshare',     theme: 'theme-rideshare'     };
        if (domain === 'travel')        return { headline: 'Travel',        summary: 'Travel & trips',         variant: 'result', kind: 'travel',        theme: 'theme-travel'        };
        if (domain === 'video')         return { headline: 'Video',         summary: 'Video streaming',        variant: 'result', kind: 'video',         theme: 'theme-video'         };
        if (domain === 'health')        return { headline: 'Health',        summary: 'Health & fitness',       variant: 'result', kind: 'health',        theme: 'theme-health'        };
        if (domain === 'alarm')         return { headline: 'Alarm',         summary: 'Alarms & clock',         variant: 'result', kind: 'alarm',         theme: 'theme-alarm'         };
        if (domain === 'clock')         return { headline: 'Clock',         summary: 'Clock & timers',         variant: 'result', kind: 'clock',         theme: 'theme-clock'         };
        if (domain === 'podcast')       return { headline: 'Podcasts',      summary: 'Podcast player',         variant: 'result', kind: 'podcast',       theme: 'theme-podcast'       };
        if (domain === 'recipe')        return { headline: 'Recipes',       summary: 'Recipes & cooking',      variant: 'result', kind: 'recipe',        theme: 'theme-recipe'        };
        if (domain === 'grocery')       return { headline: 'Grocery',       summary: 'Grocery list',           variant: 'result', kind: 'grocery',       theme: 'theme-grocery'       };
        if (domain === 'translate')     return { headline: 'Translate',     summary: 'Translation',            variant: 'result', kind: 'translate',     theme: 'theme-translate'     };
        if (domain === 'book')          return { headline: 'Books',         summary: 'Books & reading',        variant: 'result', kind: 'book',          theme: 'theme-book'          };
        // ── Wave-4 domain fallbacks ──────────────────────────────────────────────────
        if (domain === 'notifications') return { headline: 'Notifications', summary: 'Notification center',    variant: 'result', kind: 'notifications', theme: 'theme-notifications' };
        if (domain === 'handoff')       return { headline: 'Continuity',    summary: 'Cross-device handoff',   variant: 'result', kind: 'handoff',       theme: 'theme-handoff'       };
        if (domain === 'jira')          return { headline: 'Jira',          summary: 'Issue tracker',          variant: 'result', kind: 'enterprise',    theme: 'theme-enterprise'    };
        if (domain === 'github')        return { headline: 'GitHub',        summary: 'Code & pull requests',   variant: 'result', kind: 'enterprise',    theme: 'theme-enterprise'    };
        if (domain === 'slack')         return { headline: 'Slack',         summary: 'Team messaging',         variant: 'result', kind: 'enterprise',    theme: 'theme-enterprise'    };
        if (domain === 'notion')        return { headline: 'Notion',        summary: 'Pages & databases',      variant: 'result', kind: 'enterprise',    theme: 'theme-enterprise'    };
        if (domain === 'asana')         return { headline: 'Asana',         summary: 'Project management',     variant: 'result', kind: 'enterprise',    theme: 'theme-enterprise'    };
        if (domain === 'wallet')        return { headline: 'Wallet',        summary: 'Passes & cards',         variant: 'result', kind: 'wallet',        theme: 'theme-wallet'        };
        if (domain === 'vpn')           return { headline: 'VPN',           summary: 'VPN connection',         variant: 'result', kind: 'vpn',           theme: 'theme-vpn'           };
        if (domain === 'focus')         return { headline: 'Focus',         summary: 'Focus & screen time',    variant: 'result', kind: 'focus',         theme: 'theme-focus'         };
        if (domain === 'dictionary')    return { headline: 'Dictionary',    summary: 'Word lookup',            variant: 'result', kind: 'dictionary',    theme: 'theme-dictionary'    };
        if (domain === 'password')      return { headline: 'Passwords',     summary: 'Password manager',       variant: 'result', kind: 'password',      theme: 'theme-password'      };
        if (domain === 'app')           return { headline: 'App Store',     summary: 'Apps',                   variant: 'result', kind: 'app',           theme: 'theme-app'           };
        if (domain === 'reading')       return { headline: 'Reading List',  summary: 'Saved articles',         variant: 'result', kind: 'reading',       theme: 'theme-reading'       };
        if (domain === 'date')          return { headline: 'Date',          summary: 'Date calculator',        variant: 'result', kind: 'date',          theme: 'theme-date'          };
        if (domain === 'screen')        return { headline: 'Screen',        summary: 'Screen capture',         variant: 'result', kind: 'screen',        theme: 'theme-screen'        };
        if (domain === 'print')         return { headline: 'Print',         summary: 'Print & scan',           variant: 'result', kind: 'print',         theme: 'theme-print'         };
        if (domain === 'backup')        return { headline: 'Backup',        summary: 'Device backup',          variant: 'result', kind: 'backup',        theme: 'theme-backup'        };
        if (domain === 'accessibility') return { headline: 'Accessibility', summary: 'Accessibility settings', variant: 'result', kind: 'accessibility', theme: 'theme-accessibility' };
        if (domain === 'shortcuts')     return { headline: 'Shortcuts',     summary: 'Automations',            variant: 'result', kind: 'shortcuts',     theme: 'theme-shortcuts'     };
        if (domain === 'currency')      return { headline: 'Currency',      summary: 'Exchange rates',              variant: 'result', kind: 'currency',      theme: 'theme-currency'      };
        if (domain === 'connectors')    return { headline: 'Connections',   summary: 'Manage connected services',   variant: 'result', kind: 'connections',   theme: 'theme-connections'   };
        if (domain === 'gaming')        return { headline: 'Gaming',        summary: 'Games & achievements',        variant: 'result', kind: 'gaming',        theme: 'theme-gaming'        };
        if (domain === 'arvr')          return { headline: 'AR / VR',       summary: 'Spatial computing',          variant: 'result', kind: 'arvr',          theme: 'theme-arvr'          };
        if (domain === 'dating')        return { headline: 'Dating',        summary: 'Matches & messages',         variant: 'result', kind: 'dating',        theme: 'theme-dating'        };
        return { headline: summary, summary: fallbackSummary, variant: 'result', kind: 'generic', theme: 'theme-neutral' };
    },

    buildPrimaryVisual(core, execution, envelope, plan) {
        // ── Multi-step plan scene ─────────────────────────────────────────────
        if (core.kind === 'plan') {
            const { steps = [], intent = '', allOk, anyFail } = core.info || {};
            const domainFor = (op) => {
                const o = String(op || '').toLowerCase();
                if (!o) return 'generic';
                if (o.includes('gmail') || o.includes('email')) return 'email';
                if (o.includes('gcal') || o.includes('calendar')) return 'calendar';
                if (o.includes('gdrive')) return 'drive';
                if (o.includes('slack') || o.includes('message')) return 'messaging';
                if (o.includes('document') || o.includes('spreadsheet') || o.includes('presentation') || o.includes('content_')) return 'content';
                if (o.includes('notify') || o.includes('alert')) return 'notifications';
                if (o.includes('connect')) return 'connectors';
                if (o.includes('handoff') || o.includes('continuity') || o.includes('presence')) return 'handoff';
                if (o.includes('runtime') || o.includes('self_check')) return 'runtime';
                if (o.includes('file')) return 'files';
                if (o.includes('task')) return 'tasks';
                if (o.includes('note')) return 'notes';
                if (o.includes('expense')) return 'expenses';
                return 'generic';
            };
            const iconFor = (op) => {
                if (!op) return '○';
                const o = String(op).toLowerCase();
                if (o.includes('weather')) return '☁';
                if (o.includes('music') || o.includes('spotify')) return '♫';
                if (o.includes('calendar') || o.includes('gcal')) return '📅';
                if (o.includes('gmail') || o.includes('email')) return '✉';
                if (o.includes('task')) return '✓';
                if (o.includes('note')) return '✎';
                if (o.includes('remind')) return '⏰';
                if (o.includes('slack') || o.includes('message')) return '💬';
                if (o.includes('search') || o.includes('web')) return '⌕';
                if (o.includes('file')) return '📄';
                if (o.includes('travel') || o.includes('flight')) return '✈';
                if (o.includes('shop') || o.includes('cart')) return '🛍';
                return '▷';
            };
            const stepRows = steps.map((s, i) => {
                const ok = s?.ok !== false;
                const msg = String(s?.message || s?.op || '').slice(0, 80);
                const opLabel = String(s?.op || '').replace(/_/g, ' ');
                const statusClass = ok ? 'plan-step-ok' : 'plan-step-fail';
                const icon = iconFor(s?.op);
                return `
                    <div class="plan-step ${statusClass}">
                        <span class="plan-step-num">${i + 1}</span>
                        <span class="plan-step-icon">${escapeHtml(icon)}</span>
                        <div class="plan-step-body">
                            <div class="plan-step-op">${escapeHtml(opLabel)}</div>
                            <div class="plan-step-msg">${escapeHtml(msg)}</div>
                        </div>
                        <span class="plan-step-status">${ok ? '✓' : '✗'}</span>
                    </div>`;
            }).join('');
            const domainCards = Array.from(new Map(steps.map((step) => {
                const domain = domainFor(step?.op);
                return [domain, {
                    domain,
                    ok: step?.ok !== false,
                    op: String(step?.op || '').replace(/_/g, ' '),
                    message: String(step?.message || step?.op || '').slice(0, 64),
                }];
            })).values())
                .filter((item) => item.domain !== 'generic')
                .slice(0, 4)
                .map((item) => `
                    <button class="cnt-item" type="button" data-scene-domain="${escapeAttr(item.domain)}">
                        <div class="cnt-item-icon ${escapeAttr(item.domain)}"></div>
                        <div class="cnt-item-name">${escapeHtml(item.domain)}</div>
                        <div class="cnt-item-type">${item.ok ? 'ready' : 'attention'}</div>
                        <div class="cnt-item-ver">${escapeHtml(item.op)}</div>
                        <div class="cnt-item-time">${escapeHtml(item.message)}</div>
                    </button>
                `).join('');
            const summaryLine = anyFail
                ? `<span class="plan-summary-warn">${steps.filter(s => s?.ok === false).length} step(s) failed</span>`
                : `<span class="plan-summary-ok">All steps completed</span>`;
            return `
                <div class="plan-scene">
                    <div class="plan-intent">${escapeHtml(intent)}</div>
                    ${domainCards ? `<div class="cnt-history">${domainCards}</div>` : ''}
                    <div class="plan-steps">${stepRows}</div>
                    <div class="plan-summary">${summaryLine}</div>
                </div>`;
        }

        if (core.kind === 'shopping') {
            const info = core.info || {};
            const items = Array.isArray(info.items) ? info.items.slice(0, 24) : [];
            const dominantBrand = items.length ? String(items[0].brand || '').trim() : '';
            const sourceTarget = (info.sourceTarget && typeof info.sourceTarget === 'object') ? info.sourceTarget : null;
            const sourceHost = String(sourceTarget?.host || '').trim().toLowerCase();
            const brandLink = String(sourceTarget?.url || '').trim() || `https://www.google.com/search?q=${encodeURIComponent(`${dominantBrand || 'shopping'} ${String(info.query || '')}`)}`;
            const brandLabel = String(sourceTarget?.label || '').trim() || `open ${dominantBrand || 'brand'} site`;
            const directMode = String(sourceTarget?.mode || '').toLowerCase() === 'direct';
            if (directMode) {
                const directItems = sourceHost
                    ? items.filter((item) => String(item?.sourceHost || '').toLowerCase().includes(sourceHost))
                    : items;
                const sourceItems = directItems.length ? directItems : items;
                const brandName    = String(sourceTarget?.brandName          || dominantBrand || '').trim();
                const brandTheme   = String(sourceTarget?.brandTheme         || 'neutral').trim();
                const brandPrimary = String(sourceTarget?.brandColors?.primary || '#1d1d1b').trim();
                const brandAccent  = String(sourceTarget?.brandColors?.accent  || '#00a651').trim();
                const fitSignal    = String(info.query || this.state.session.lastIntent || '').trim();
                const refineCommands = this.buildShoppingRefineCommands(brandName || dominantBrand, fitSignal, String(info.category || 'shoes'));
                const refineHtml = refineCommands.map((cmd) => `
                    <button type="button" class="shop-refine-chip" data-command="${escapeAttr(cmd)}">${escapeHtml(cmd.replace(/^show me\s+/i, ''))}</button>
                `).join('');
                // Semantic activity detection — drives the canvas environment
                const titleCorpus = sourceItems.map(i => String(i.title || '')).join(' ');
                const activityStr = (fitSignal + ' ' + titleCorpus).toLowerCase();
                const activity =
                    /\brunning\b|marathon|foreverrun|deviate\s*nitro|velocity\s*nitro|magnify|jog|pace|race\b/.test(activityStr) ? 'running'
                    : /basketball|\bmb\.\d|hoop|\bnba\b|court\s+shoe/.test(activityStr) ? 'basketball'
                    : /soccer|cleat|firm\s*ground|artificial\s*ground|futsal|leadcat/.test(activityStr) ? 'soccer'
                    : /hiking|trail\s+shoe|mountain|terrain/.test(activityStr) ? 'trail'
                    : /training|cross.?train|\bgym\b|workout|fitness/.test(activityStr) ? 'training'
                    : /casual|lifestyle|suede\b|palermo|caven|future\s*rider|retro|streetwear|classic\s*sneaker/.test(activityStr) ? 'lifestyle'
                    : 'sport';
                const hero         = sourceItems[0] || {};
                // Swap Cloudinary white bg (fafafa) for brand dark primary so hero looks like a stage
                const heroBgHex    = brandPrimary.replace('#', '').toLowerCase();
                const heroImage    = String(hero.imageUrl || '').trim()
                                       .replace(/b_rgb:[0-9a-fA-F]{3,6}/, `b_rgb:${heroBgHex}`)
                                       .replace(/,w_\d+/, ',w_900');
                const heroTitle    = String(hero.title || `${brandName} picks`).trim();
                const heroPrice    = hero.priceUsd ? formatCurrency(Number(hero.priceUsd)) : '';
                const heroUrl      = String(hero.url || brandLink).trim();
                const liveFrameUrl = brandLink;
                const railItems    = sourceItems.slice(1).map((item) => {
                    const imgSrc  = String(item.imageUrl || '').trim()
                                      .replace(/b_rgb:[0-9a-fA-F]{3,6}/, `b_rgb:${heroBgHex}`);
                    const ttl     = String(item.title    || 'item').trim();
                    const prc     = item.priceUsd ? formatCurrency(Number(item.priceUsd)) : '';
                    const itemUrl = String(item.url      || brandLink).trim();
                    const host    = String(item.sourceHost || '').trim();
                    return `
                        <a class="shop-stage-tile" href="${safeUrl(itemUrl)}" target="_blank" rel="noopener noreferrer">
                            ${imgSrc ? `<img src="${safeUrl(imgSrc)}" alt="${escapeAttr(ttl)}" loading="lazy" />` : '<div class="shop-stage-tile-img-fallback"></div>'}
                            <div class="shop-stage-tile-text">
                                <span class="shop-stage-tile-label">${escapeHtml(ttl)}</span>
                                <span class="shop-stage-tile-price">${escapeHtml([prc, host].filter(Boolean).join(' | '))}</span>
                            </div>
                        </a>
                    `;
                }).join('');
                return `
                    <div class="scene scene-shopping scene-shopping-stage interactive theme-${escapeAttr(brandTheme)}">
                        <canvas class="scene-canvas shopping-canvas"
                                data-scene="shopping"
                                data-brand-theme="${escapeAttr(brandTheme)}"
                                data-brand-primary="${escapeAttr(brandPrimary)}"
                                data-brand-accent="${escapeAttr(brandAccent)}"
                                data-fit-signal="${escapeAttr(fitSignal)}"
                                data-activity="${escapeAttr(activity)}"></canvas>
                        <div class="shop-brand-bar">
                            <div class="shop-brand-name">${escapeHtml(brandName)}</div>
                            ${fitSignal ? `<div class="shop-fit-signal">${escapeHtml(fitSignal)}</div>` : ''}
                            <a class="scene-chip scene-chip-link shop-brand-cta" href="${safeUrl(brandLink)}" target="_blank" rel="noopener noreferrer">
                                ${escapeHtml(brandLabel)}
                            </a>
                        </div>
                        <div class="shop-source-strip">
                            <span>source ${escapeHtml(String(sourceHost || sourceTarget?.host || 'direct'))}</span>
                            <span>${escapeHtml(String(sourceItems.length))} products</span>
                            <span>intent matched</span>
                        </div>
                        ${refineHtml ? `<div class="shop-refine-row">${refineHtml}</div>` : ''}
                        <div class="shop-stage-body">
                            <div class="shop-stage-hero">
                                ${liveFrameUrl ? `<iframe class="shop-stage-live-frame" src="${safeUrl(liveFrameUrl)}" title="${escapeAttr(`${brandName || 'brand'} live source`)}" loading="eager" referrerpolicy="no-referrer" sandbox="allow-scripts allow-forms allow-popups allow-top-navigation-by-user-activation"></iframe>` : ''}
                                ${heroImage ? `<img class="shop-stage-hero-fallback-image" src="${safeUrl(heroImage)}" alt="${escapeAttr(heroTitle)}" loading="lazy" />` : '<div class="shop-stage-hero-fallback"></div>'}
                                <div class="shop-stage-hero-tint"></div>
                                <div class="shop-stage-hero-meta">
                                    <div class="shop-stage-hero-title">${escapeHtml(heroTitle)}</div>
                                    ${heroPrice ? `<div class="shop-stage-hero-price">${escapeHtml(heroPrice)}</div>` : ''}
                                    <a class="shop-stage-open-live" href="${safeUrl(heroUrl)}" target="_blank" rel="noopener noreferrer">open product</a>
                                </div>
                            </div>
                            <div class="shop-stage-rail">
                                ${railItems}
                            </div>
                        </div>
                    </div>
                `;
            }
            const cards = items.map((item, idx) => {
                const title    = String(item.title    || 'Product');
                const brand    = String(item.brand    || '');
                const price    = Number(item.priceUsd || 0);
                const imageUrl = String(item.imageUrl || '').trim();
                const url      = String(item.url      || '').trim() || '#';
                const host     = String(item.sourceHost || '').trim();
                const sizeMod  = idx === 0 ? ' shop-card-featured' : (idx % 5 === 2 ? ' shop-card-tall' : '');
                return `
                    <a class="shop-card${sizeMod}" href="${safeUrl(url)}" target="_blank" rel="noopener noreferrer">
                        <div class="shop-image-wrap">
                            ${imageUrl ? `<img class="shop-image" src="${safeUrl(imageUrl)}" alt="${escapeAttr(title)}" loading="${idx < 3 ? 'eager' : 'lazy'}" />` : '<div class="shop-image-placeholder"></div>'}
                        </div>
                        <div class="shop-meta">
                            <div class="shop-title">${escapeHtml(title)}</div>
                            <div class="shop-sub">${escapeHtml(brand)}${price ? ` · ${escapeHtml(formatCurrency(price))}` : ''}${host ? ` · ${escapeHtml(host)}` : ''}</div>
                        </div>
                    </a>
                `;
            }).join('');
            const refineCommands = this.buildShoppingRefineCommands(dominantBrand, String(info.query || this.state.session.lastIntent || ''), String(info.category || 'shoes'));
            const refineHtml = refineCommands.map((cmd) => `
                <button type="button" class="shop-refine-chip" data-command="${escapeAttr(cmd)}">${escapeHtml(cmd.replace(/^show me\s+/i, ''))}</button>
            `).join('');
            return `
                <div class="scene scene-shopping interactive">
                    <div class="scene-orb orb-a"></div>
                    <div class="scene-orb orb-b"></div>
                    <div class="scene-grid"></div>
                    <div class="scene-chip-row immersive-scene-head">
                        <div class="scene-chip">visual catalog</div>
                        <a class="scene-chip scene-chip-link" href="${safeUrl(brandLink)}" target="_blank" rel="noopener noreferrer">
                            ${escapeHtml(brandLabel)}
                        </a>
                    </div>
                    ${refineHtml ? `<div class="shop-refine-row shop-refine-row-catalog">${refineHtml}</div>` : ''}
                    <div class="shop-gallery shop-gallery-masonry">${cards}</div>
                </div>
            `;
        }
        if (core.kind === 'social') {
            const info = core.info || {};
            const op = String(info.op || '');

            // ── notifications ──────────────────────────────────────────────
            if (op === 'social_notifications') {
                const items = Array.isArray(info.items) ? info.items.slice(0, 20) : [];
                const unread = Number(info.unread || 0);
                const reasonIcon = { like: '♥', repost: '↺', follow: '+', mention: '@', reply: '↩', quote: '❝' };
                const rows = items.map((n, i) => {
                    const reason = String(n?.reason || 'activity');
                    const author = String(n?.author || n?.handle || '—');
                    const age = String(n?.age || n?.indexedAt || '');
                    const isNew = i < unread;
                    const icon = reasonIcon[reason] || '·';
                    return `<div class="sn-row${isNew ? ' sn-new' : ''}">
                        <div class="sn-icon">${escapeHtml(icon)}</div>
                        <div class="sn-author">${escapeHtml(author)}</div>
                        <div class="sn-reason">${escapeHtml(reason)}</div>
                        <div class="sn-age">${escapeHtml(age)}</div>
                    </div>`;
                }).join('');
                return `
                    <div class="scene scene-social interactive">
                        <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                        <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                        <div class="scene-grid"></div>
                        <div class="social-head">
                            <div class="social-title">notifications</div>
                            <div class="social-source">${unread > 0 ? `<span class="sn-badge">${unread}</span> unread` : 'all read'}</div>
                        </div>
                        <div class="sn-stream">
                            ${rows || '<div class="sn-row"><div class="sn-reason">no notifications</div></div>'}
                        </div>
                    </div>`;
            }

            // ── trending ───────────────────────────────────────────────────
            if (op === 'social_trending') {
                const topics = Array.isArray(info.topics) ? info.topics.slice(0, 24) : [];
                const pills = topics.map((t, i) => {
                    const label = String(t?.topic || t?.displayName || '—');
                    const rank = i + 1;
                    const hot = i < 3 ? ' st-hot' : '';
                    return `<div class="st-pill${hot}"><span class="st-rank">${rank}</span>${escapeHtml(label)}</div>`;
                }).join('');
                return `
                    <div class="scene scene-social interactive">
                        <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                        <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                        <div class="scene-grid"></div>
                        <div class="social-head">
                            <div class="social-title">trending</div>
                            <div class="social-source">bluesky · ${topics.length} topics</div>
                        </div>
                        <div class="st-grid">
                            ${pills || '<div class="st-pill">no trending topics</div>'}
                        </div>
                    </div>`;
            }

            // ── profile ────────────────────────────────────────────────────
            if (op === 'social_profile_read') {
                const handle = String(info.handle || '');
                const displayName = String(info.displayName || handle);
                const bio = String(info.bio || '').slice(0, 180);
                const followers = Number(info.followers || 0);
                const following = Number(info.following || 0);
                const posts = Number(info.posts || 0);
                const initial = (displayName || handle || '?')[0].toUpperCase();
                return `
                    <div class="scene scene-social interactive">
                        <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                        <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                        <div class="scene-grid"></div>
                        <div class="sp-shell">
                            <div class="sp-avatar-wrap">
                                <div class="sp-avatar">${escapeHtml(initial)}</div>
                            </div>
                            <div class="sp-name">${escapeHtml(displayName)}</div>
                            <div class="sp-handle">@${escapeHtml(handle)}</div>
                            ${bio ? `<div class="sp-bio">${escapeHtml(bio)}</div>` : ''}
                            <div class="sp-stats">
                                <div class="sp-stat"><span>${followers.toLocaleString()}</span><em>followers</em></div>
                                <div class="sp-stat"><span>${following.toLocaleString()}</span><em>following</em></div>
                                <div class="sp-stat"><span>${posts.toLocaleString()}</span><em>posts</em></div>
                            </div>
                        </div>
                    </div>`;
            }

            // ── action receipts: dm / react / comment / follow ─────────────
            if (['social_dm_send','social_react','social_comment','social_follow'].includes(op)) {
                const actionMeta = {
                    social_dm_send:  { icon: '✉', label: 'direct message sent', color: 'rgba(122,192,255,0.9)' },
                    social_react:    { icon: '♥', label: 'post liked',           color: 'rgba(255,110,130,0.9)' },
                    social_comment:  { icon: '↩', label: 'reply posted',         color: 'rgba(130,220,160,0.9)' },
                    social_follow:   { icon: '+', label: String(info.action || 'followed'), color: 'rgba(190,150,255,0.9)' },
                };
                const meta = actionMeta[op];
                const target = String(info.recipient || info.actor || info.uri || '').slice(0, 80);
                return `
                    <div class="scene scene-social interactive">
                        <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                        <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                        <div class="scene-grid"></div>
                        <div class="sa-shell">
                            <div class="sa-icon" style="color:${meta.color}">${escapeHtml(meta.icon)}</div>
                            <div class="sa-label">${escapeHtml(meta.label)}</div>
                            ${target ? `<div class="sa-target">${escapeHtml(target)}</div>` : ''}
                            <div class="sa-check">✓ delivered</div>
                        </div>
                    </div>`;
            }

            // ── feed (default) ─────────────────────────────────────────────
            const items = Array.isArray(info.items) ? info.items.slice(0, 12) : [];
            const source = String(info.source || 'scaffold').trim();
            const message = String(info.message || '').trim();
            const delivery = String(info.delivery || '').trim();
            const cards = items.map((item, idx) => {
                const author = String(item?.author || `user ${idx + 1}`).trim();
                const summary = String(item?.summary || item?.text || '').trim();
                const age = String(item?.age || item?.time || '').trim();
                const initial = author ? author[0].toUpperCase() : 'U';
                return `<article class="social-card">
                    <div class="social-card-head">
                        <div class="social-avatar">${escapeHtml(initial)}</div>
                        <div class="social-author">${escapeHtml(author)}</div>
                        <div class="social-age">${escapeHtml(age || 'now')}</div>
                    </div>
                    <div class="social-summary">${escapeHtml(summary || 'No summary')}</div>
                </article>`;
            }).join('');
            const composer = message
                ? `<div class="social-composer"><span>message</span><strong>${escapeHtml(message.slice(0, 180))}</strong><em>${escapeHtml(delivery || 'queued')}</em></div>`
                : '<div class="social-composer"><span>feed mode</span><strong>Live social context cards</strong><em>read only</em></div>';
            return `
                <div class="scene scene-social interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                    <div class="scene-orb orb-a"></div>
                    <div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="social-head">
                        <div class="social-title">social intent stream</div>
                        <div class="social-source">${escapeHtml(source)}</div>
                    </div>
                    ${composer}
                    <div class="social-feed-grid">
                        ${cards || '<article class="social-card"><div class="social-summary">No social cards yet.</div></article>'}
                    </div>
                </div>
            `;
        }
        if (core.kind === 'banking') {
            const info = core.info || {};
            const items = Array.isArray(info.items) ? info.items.slice(0, 8) : [];
            const available = Number(info.available || 0);
            const currency = String(info.currency || 'USD').trim();
            const asOf = String(info.asOf || '').trim();
            const txRows = items.map((item, i) => {
                const merchant = String(item?.merchant || item?.name || '-').trim();
                const amount = Number(item?.amount || 0);
                const direction = amount < 0 ? 'debit' : 'credit';
                const opacity = Math.max(0.12, 0.55 - i * 0.07);
                return `<div class="bank-stream-item" style="opacity:${opacity}">
                    <div class="bank-stream-merchant">${escapeHtml(merchant)}</div>
                    <div class="bank-stream-amount ${direction}">${escapeHtml(formatCurrency(Math.abs(amount)))}</div>
                </div>`;
            }).join('');
            return `<div class="scene scene-banking interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                <div class="bank-shell">
                    <div class="bank-eyebrow">${escapeHtml(currency)}${asOf ? ` · ${escapeHtml(asOf)}` : ''}</div>
                    <div class="bank-hero">${escapeHtml(formatCurrency(available))}</div>
                    <div class="bank-hero-label">available balance</div>
                    ${txRows ? `<div class="bank-stream">${txRows}</div>` : ''}
                </div>
            </div>`;
        }
        if (core.kind === 'contacts') {
            const info = core.info || {};
            const items = Array.isArray(info.items) ? info.items.slice(0, 12) : [];
            const focused = items[0] || null;
            const focusName = String(focused?.name || '').trim();
            const focusPhone = String(focused?.phone || '').trim();
            const focusLabel = String(focused?.label || '').trim();
            const streamHtml = items.slice(1).map((item, i) => {
                const name = String(item?.name || '').trim();
                const meta = String(item?.phone || item?.label || '').trim();
                const opacity = Math.max(0.1, 0.5 - i * 0.06);
                return `<div class="contact-stream-item" style="opacity:${opacity}">
                    <span class="contact-stream-name">${escapeHtml(name)}</span>
                    ${meta ? `<span class="contact-stream-meta">${escapeHtml(meta)}</span>` : ''}
                </div>`;
            }).join('');
            return `<div class="scene scene-contacts interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                <div class="contact-shell">
                    <div class="contact-eyebrow">contacts · ${items.length}</div>
                    <div class="contact-hero">${escapeHtml(focusName || 'No contact found')}</div>
                    ${(focusPhone || focusLabel) ? `<div class="contact-hero-meta">${escapeHtml([focusLabel, focusPhone].filter(Boolean).join(' · '))}</div>` : ''}
                    ${focusPhone ? `<button class="contact-call-btn" data-command="${escapeAttr(`confirm call ${focusPhone}`)}" type="button">prepare call</button>` : ''}
                    ${streamHtml ? `<div class="contact-stream">${streamHtml}</div>` : ''}
                </div>
            </div>`;
        }
        if (core.kind === 'telephony') {
            const info = core.info || {};
            const callSid = String(info.callSid || '').trim();
            const target = String(info.target || info.to || '').trim();
            const contactName = String(info.contactName || '').trim();
            const initState = String(info.state || info.mode || 'ringing').trim();
            const showSetup = !callSid || initState === 'setup_twilio';
            if (showSetup) {
                // No active call — show onboarding steps
                const lines = Array.isArray(info.steps) ? info.steps : [
                    'grant connector scope telephony.call.start',
                    'confirm call <target>',
                    'open mobile and claim handoff token',
                ];
                const items = lines.slice(0, 6).map((l) => `<li>${escapeHtml(String(l))}</li>`).join('');
                return `
                    <div class="scene scene-telephony interactive">
                        <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                        <div class="scene-orb orb-b"></div>
                        <div class="scene-grid"></div>
                        <div class="telephony-head">
                            <div class="telephony-title">telephony</div>
                            <div class="telephony-meta">setup required</div>
                        </div>
                        <div class="telephony-panel">
                            <div class="telephony-label">next actions</div>
                            <ol class="telephony-steps">${items}</ol>
                        </div>
                    </div>
                `;
            }
            return `
                <div class="scene scene-telephony interactive" data-func-domain="telephony"
                     data-call-sid="${escapeAttr(callSid)}"
                     data-call-state="${escapeAttr(initState)}">
                    <canvas class="scene-canvas phone-canvas" data-scene="phone"></canvas>
                    <div class="scene-orb orb-b"></div>
                    <div class="phone-avatar" aria-hidden="true">
                        <div class="phone-avatar-ring"></div>
                        <div class="phone-avatar-initials">${escapeHtml((contactName || target).slice(0, 2).toUpperCase())}</div>
                    </div>
                    <div class="telephony-head">
                        <div class="telephony-title">${escapeHtml(contactName || target || 'unknown')}</div>
                        <div class="telephony-meta">${escapeHtml(contactName ? target : '')}</div>
                    </div>
                    <div class="phone-state-row">
                        <span class="phone-state-badge" data-state="${escapeAttr(initState)}">${escapeHtml(initState)}</span>
                        <span class="phone-timer" data-start="" data-active="${initState === 'active' ? '1' : '0'}">--:--</span>
                    </div>
                    <div class="phone-controls">
                        <button class="phone-btn phone-btn-mute" data-muted="0" title="Mute">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a4 4 0 0 1 4 4v6a4 4 0 0 1-8 0V5a4 4 0 0 1 4-4z"/><path d="M19 10a7 7 0 0 1-14 0"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
                        </button>
                        <button class="phone-btn phone-btn-hold" data-held="0" title="Hold">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
                        </button>
                        <button class="phone-btn phone-btn-hangup" title="End call">
                            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M6.6 10.8c1.4 2.8 3.8 5.1 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1C10.6 21 3 13.4 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1L6.6 10.8z"/></svg>
                        </button>
                        <button class="phone-btn phone-btn-dtmf" title="Keypad">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="4" height="4" rx="1"/><rect x="10" y="3" width="4" height="4" rx="1"/><rect x="17" y="3" width="4" height="4" rx="1"/><rect x="3" y="10" width="4" height="4" rx="1"/><rect x="10" y="10" width="4" height="4" rx="1"/><rect x="17" y="10" width="4" height="4" rx="1"/><rect x="3" y="17" width="4" height="4" rx="1"/><rect x="10" y="17" width="4" height="4" rx="1"/><rect x="17" y="17" width="4" height="4" rx="1"/></svg>
                        </button>
                    </div>
                    <div class="phone-dtmf-pad" hidden>
                        ${['1','2','3','4','5','6','7','8','9','*','0','#'].map((d) =>
                            `<button class="phone-dtmf-key" data-digit="${escapeAttr(d)}">${escapeHtml(d)}</button>`
                        ).join('')}
                    </div>
                </div>
            `;
        }
        if (core.kind === 'weather') {
            const info = core.info || {};
            const condition = String(info.condition || '').toLowerCase();
            const location = String(info.location || '').trim() || String(this.state.session.lastIntent || '').trim();
            const forecastItems = this.buildWeatherForecastPoints(info).slice(0, 5);
            const forecastStrip = forecastItems.map((p) => {
                const precip = Number(p.precip || 0);
                const wet = precip >= 45 ? 'wet' : precip >= 20 ? 'mixed' : 'dry';
                const cardIcon = this.conditionIcon(p.condition);
                return `
                    <div class="weather-forecast-card ${wet}">
                        <div class="wfc-time">${escapeHtml(String(p.hour || ''))}</div>
                        <div class="wfc-icon">${cardIcon}</div>
                        <div class="wfc-temp">${escapeHtml(String(Math.round(Number(p.temp || 0))))}F</div>
                        <div class="wfc-bar"><div class="wfc-bar-fill" style="width:${Math.min(100, precip)}%"></div></div>
                    </div>
                `;
            }).join('');
            const temperature = Number(info.temperatureF || String(info.temperature || '').replace(/[^0-9.-]/g, '')) || 60;
            const windMph = Number(info.windMph || 0);
            const weatherTarget = (info.sourceTarget && typeof info.sourceTarget === 'object') ? info.sourceTarget : null;
            const weatherTargetUrl = String(weatherTarget?.url || '').trim();
            const weatherTargetLabel = String(weatherTarget?.label || 'open weather source').trim();
            return `
                <div class="scene scene-weather ${escapeAttr(core.theme || '')}">
                    <div class="weather-hero-tint"></div>
                    <canvas
                        class="scene-canvas weather-canvas"
                        data-scene="weather"
                        data-condition="${escapeAttr(condition)}"
                        data-temp="${escapeAttr(String(temperature))}"
                        data-wind="${escapeAttr(String(windMph))}"
                        data-hour="${escapeAttr(String(info.localHour ?? -1))}"
                        data-sunrise="${escapeAttr(String(info.sunriseHour ?? 6))}"
                        data-sunset="${escapeAttr(String(info.sunsetHour ?? 19))}"
                        data-terrain="${escapeAttr(this.locationTerrain(location))}"
                    ></canvas>
                    <div class="weather-radar" aria-hidden="true">
                        <div class="radar-ring ring-1"></div>
                        <div class="radar-ring ring-2"></div>
                        <div class="radar-ring ring-3"></div>
                        <div class="radar-sweep"></div>
                    </div>
                    <div class="scene-orb orb-a"></div>
                    <div class="scene-orb orb-b"></div>
                    <div class="scene-grid"></div>
                    <div class="weather-hero">
                        <div class="weather-hero-temp">${escapeHtml(String(Math.round(temperature)))}°</div>
                        <div class="weather-hero-cond">${escapeHtml(condition)}</div>
                        <div class="weather-hero-meta">
                            <span>${escapeHtml(String(Math.round(windMph)))} mph</span>
                            <span class="wh-sep">·</span>
                            <span>${escapeHtml(location)}</span>
                            ${weatherTargetUrl ? `<a class="wh-link" href="${safeUrl(weatherTargetUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(weatherTargetLabel)}</a>` : ''}
                        </div>
                    </div>
                    <div class="weather-forecast-strip">${forecastStrip}</div>
                </div>
            `;
        }
        if (core.kind === 'weather_tomorrow') {
            const info = core.info || {};
            const condition = String(info.condition || '').toLowerCase();
            const location  = String(info.location || '').trim();
            const daily     = Array.isArray(info.daily) ? info.daily : [];
            const tmr       = daily[1] || daily[0] || {};
            const tmrCond   = String(tmr.condition || condition).toLowerCase();
            const tmrMax    = Math.round(Number(tmr.maxTempF || info.temperatureF || 0));
            const tmrMin    = Math.round(Number(tmr.minTempF || 0));
            const tmrWind   = Math.round(Number(tmr.windMph  || info.windMph || 0));
            const tmrPrecip = Number(tmr.precipChance || 0);
            const tmrIcon   = this.conditionIcon(tmrCond);
            const tomorrow  = (info.window === 'tonight') ? 'Tonight' : 'Tomorrow';
            const hourlyData = Array.isArray(info.forecastTomorrow) && info.forecastTomorrow.length
                ? info.forecastTomorrow : (Array.isArray(info.forecast) ? info.forecast : []);
            const tmrStrip = hourlyData.slice(0, 6).map((p) => {
                const precip = Number(p.precipChance || 0);
                const wet = precip >= 45 ? 'wet' : precip >= 20 ? 'mixed' : 'dry';
                return `<div class="weather-forecast-card ${wet}">
                    <div class="wfc-time">${escapeHtml(String(p.hourLabel || ''))}</div>
                    <div class="wfc-icon">${this.conditionIcon(p.condition)}</div>
                    <div class="wfc-temp">${escapeHtml(String(Math.round(Number(p.tempF || 0))))}F</div>
                    <div class="wfc-bar"><div class="wfc-bar-fill" style="width:${Math.min(100, precip)}%"></div></div>
                </div>`;
            }).join('');
            const weatherTarget = (info.sourceTarget && typeof info.sourceTarget === 'object') ? info.sourceTarget : null;
            const weatherTargetUrl = String(weatherTarget?.url || '').trim();
            const weatherTargetLabel = String(weatherTarget?.label || 'open forecast').trim();
            const tmrSunrise = Number(tmr.sunriseHour ?? info.sunriseHour ?? 6);
            const tmrSunset  = Number(tmr.sunsetHour  ?? info.sunsetHour  ?? 19);
            return `
                <div class="scene scene-weather theme-tomorrow ${escapeAttr(core.theme || '')}">
                    <canvas class="scene-canvas weather-canvas"
                        data-scene="weather"
                        data-condition="${escapeAttr(tmrCond)}"
                        data-temp="${escapeAttr(String(tmrMax))}"
                        data-wind="${escapeAttr(String(tmrWind))}"
                        data-hour="${escapeAttr(String(tmrSunrise + 3))}"
                        data-sunrise="${escapeAttr(String(tmrSunrise))}"
                        data-sunset="${escapeAttr(String(tmrSunset))}"
                        data-terrain="${escapeAttr(this.locationTerrain(location))}"
                    ></canvas>
                    <div class="weather-radar" aria-hidden="true">
                        <div class="radar-ring ring-1"></div>
                        <div class="radar-ring ring-2"></div>
                        <div class="radar-ring ring-3"></div>
                        <div class="radar-sweep"></div>
                    </div>
                    <div class="scene-orb orb-a"></div>
                    <div class="scene-grid"></div>
                    <div class="weather-hero">
                        <div class="weather-hero-label">${escapeHtml(tomorrow)}</div>
                        <div class="weather-hero-temp">${escapeHtml(String(tmrMax))}°</div>
                        <div class="weather-hero-cond">${escapeHtml(tmrCond)} ${tmrIcon}</div>
                        <div class="weather-hero-meta">
                            <span>${escapeHtml(String(tmrMin))}° low</span>
                            <span class="wh-sep">·</span>
                            <span>${escapeHtml(String(tmrWind))} mph</span>
                            <span class="wh-sep">·</span>
                            <span>${escapeHtml(String(tmrPrecip))}% rain</span>
                            ${weatherTargetUrl ? `<a class="wh-link" href="${safeUrl(weatherTargetUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(weatherTargetLabel)}</a>` : ''}
                        </div>
                    </div>
                    <div class="weather-forecast-strip">${tmrStrip}</div>
                </div>
            `;
        }
        if (core.kind === 'weather_7day') {
            const info     = core.info || {};
            const location = String(info.location || '').trim();
            const daily    = Array.isArray(info.daily) ? info.daily : [];
            const source   = String(info.source || '').trim();
            const weatherTarget = (info.sourceTarget && typeof info.sourceTarget === 'object') ? info.sourceTarget : null;
            const weatherTargetUrl   = String(weatherTarget?.url   || '').trim();
            const weatherTargetLabel = String(weatherTarget?.label || 'open forecast').trim();
            const dayCards = daily.map((d, i) => {
                const precip = Number(d.precipChance || 0);
                const wet = precip >= 50 ? 'wet' : precip >= 25 ? 'mixed' : 'dry';
                const icon = this.conditionIcon(d.condition);
                const label = i === 0 ? 'Today' : i === 1 ? 'Tomorrow' : escapeHtml(String(d.dayName || ''));
                return `<div class="wx7-card ${wet}">
                    <div class="wx7-day">${label}</div>
                    <div class="wx7-icon">${icon}</div>
                    <div class="wx7-cond">${escapeHtml(String(d.condition || ''))}</div>
                    <div class="wx7-temps">
                        <span class="wx7-hi">${escapeHtml(String(Math.round(Number(d.maxTempF || 0))))}°</span>
                        <span class="wx7-lo">${escapeHtml(String(Math.round(Number(d.minTempF || 0))))}°</span>
                    </div>
                    <div class="wx7-bar"><div class="wx7-bar-fill" style="width:${Math.min(100, precip)}%"></div></div>
                    <div class="wx7-wind">${escapeHtml(String(Math.round(Number(d.windMph || 0))))} mph</div>
                </div>`;
            }).join('');
            // Dominant condition for canvas (use most common or today's)
            const domCond = String((daily[0] || {}).condition || info.condition || '').toLowerCase();
            return `
                <div class="scene scene-weather scene-weather-7day ${escapeAttr(core.theme || '')}">
                    <canvas class="scene-canvas weather-7day-canvas"
                        data-scene="weather-7day"
                        data-condition="${escapeAttr(domCond)}"
                        data-temp="${escapeAttr(String(Math.round(Number((daily[0] || {}).maxTempF || info.temperatureF || 60))))}"
                        data-terrain="${escapeAttr(this.locationTerrain(location))}"
                    ></canvas>
                    <div class="scene-orb orb-a"></div>
                    <div class="scene-grid"></div>
                    <div class="wx7-header">
                        <div class="wx7-title">${escapeHtml(String(info.windowLabel || '7-day forecast'))}</div>
                        <div class="wx7-location">${escapeHtml(location)}</div>
                        ${weatherTargetUrl ? `<a class="wh-link" href="${safeUrl(weatherTargetUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(weatherTargetLabel)}</a>` : ''}
                    </div>
                    <div class="wx7-strip">${dayCards}</div>
                </div>
            `;
        }
        if (core.kind === 'sports') {
            const info = core.info || {};
            const league = String(info.league || info.sport || '').toUpperCase() || 'Sports';
            const op = String(info.op || '');
            const events = Array.isArray(info.events) ? info.events : [];
            const standings = info.standings || null;
            const focusTeam = String(info.team || info.abbrev || '').toLowerCase();

            // Helper: pull validated hex (no #) from ESPN team object
            const teamHex = (team) => {
                const c = String(team.color || '').replace(/^#/, '').trim();
                return /^[0-9a-fA-F]{6}$/.test(c) ? c : null;
            };

            // Extract both competitors' colors + venue from the first event for the canvas
            let awayHex = null, homeHex = null, venueImgUrl = '';
            if (events.length > 0) {
                const firstComp0 = (events[0].competitions || [{}])[0] || {};
                const firstComps = firstComp0.competitors || [];
                const awayC = firstComps.find(c => c.homeAway === 'away') || firstComps[0];
                const homeC = firstComps.find(c => c.homeAway === 'home') || firstComps[1];
                if (awayC) awayHex = teamHex(awayC.team || {});
                if (homeC) homeHex = teamHex(homeC.team || {});
            }
            // Prefer backend-resolved venue image URL (from ESPN venues API)
            venueImgUrl = String(info.venueImageUrl || '').trim();

            // ── Duel layout: full-width dramatic two-halves for a single focused game ──
            // Shared game data extraction
            const gameData = (ev) => {
                const comps = (ev.competitions || [{}])[0] || {};
                const competitors = comps.competitors || [];
                const status = ev.status || {};
                const statusType = status.type || {};
                const state = String(statusType.state || 'pre').toLowerCase();
                const isFinal = state === 'post';
                const isLive = state === 'in';
                const clock = String(status.displayClock || '');
                const period = Number(status.period || 0);
                const shortDetail = String(statusType.shortDetail || '');
                const venue = String((comps.venue || {}).fullName || '');
                const away = competitors.find(c => c.homeAway === 'away') || competitors[0] || {};
                const home = competitors.find(c => c.homeAway === 'home') || competitors[1] || {};
                const statusText = isFinal ? (shortDetail || 'FINAL')
                    : isLive ? `${period ? `${period}P` : 'LIVE'} ${clock}`.trim()
                    : (shortDetail || String(ev.date || '').slice(0, 10));
                const statusClass = isFinal ? 'sp-duel-status-final' : isLive ? 'sp-duel-status-live' : 'sp-duel-status-pre';
                return { comps, competitors, isFinal, isLive, clock, period, shortDetail, venue, away, home, statusText, statusClass };
            };

            // ── Box score: full-screen layout when no venue image ──
            const renderBoxScore = (ev) => {
                const { isFinal, isLive, venue, away, home, statusText, statusClass } = gameData(ev);
                const mkTeam = (comp, align) => {
                    const t = comp.team || {};
                    const name  = escapeHtml(String(t.displayName || t.shortDisplayName || t.abbreviation || '?'));
                    const abbr  = escapeHtml(String(t.abbreviation || '?'));
                    const score = escapeHtml(String(comp.score || '–'));
                    const winner = Boolean(comp.winner);
                    const hex   = teamHex(t);
                    const logo  = String(t.logo || '').trim();
                    const logoEl = logo
                        ? `<img class="sp-bs-logo" src="${safeUrl(logo)}" alt="${abbr}" onerror="this.style.display='none'">`
                        : `<div class="sp-bs-logo-fb" style="${hex ? `background:#${hex}` : ''}">${abbr}</div>`;
                    const accentStyle = hex ? `style="--tc:#${hex}"` : '';
                    return `<div class="sp-bs-team sp-bs-${align}${winner ? ' sp-bs-winner' : ''}" ${accentStyle}>
                        ${logoEl}
                        <div class="sp-bs-names">
                            <div class="sp-bs-fullname">${name}</div>
                            <div class="sp-bs-abbr-label">${abbr} · ${align === 'away' ? 'AWAY' : 'HOME'}</div>
                        </div>
                        <div class="sp-bs-score">${score}</div>
                    </div>`;
                };

                // Line score (inning/period breakdown) — ESPN provides linescores per competitor
                const mkLinescore = () => {
                    const awayLs = Array.isArray(away.linescores) ? away.linescores : [];
                    const homeLs = Array.isArray(home.linescores) ? home.linescores : [];
                    if (!awayLs.length && !homeLs.length) return '';
                    const periods = Math.max(awayLs.length, homeLs.length);
                    const awayAbbr = escapeHtml(String((away.team || {}).abbreviation || 'AWY'));
                    const homeAbbr = escapeHtml(String((home.team || {}).abbreviation || 'HME'));
                    const awayHex2 = teamHex(away.team || {});
                    const homeHex2 = teamHex(home.team || {});
                    const headerCells = Array.from({length: periods}, (_, i) => `<th>${i + 1}</th>`).join('');
                    const awayScoreCells = awayLs.map(ls => `<td>${escapeHtml(String(ls.value ?? ls.displayValue ?? ''))}</td>`).join('');
                    const homeScoreCells = homeLs.map(ls => `<td>${escapeHtml(String(ls.value ?? ls.displayValue ?? ''))}</td>`).join('');
                    // Final totals from main score
                    const awayTot = escapeHtml(String(away.score || ''));
                    const homeTot = escapeHtml(String(home.score || ''));
                    const awayStyle = awayHex2 ? `style="color:#${awayHex2}"` : '';
                    const homeStyle = homeHex2 ? `style="color:#${homeHex2}"` : '';
                    return `<div class="sp-bs-linescore-wrap">
                        <table class="sp-bs-linescore">
                            <thead><tr><th></th>${headerCells}<th class="sp-bs-ls-total">R</th></tr></thead>
                            <tbody>
                                <tr><td class="sp-bs-ls-team" ${awayStyle}>${awayAbbr}</td>${awayScoreCells}<td class="sp-bs-ls-total">${awayTot}</td></tr>
                                <tr><td class="sp-bs-ls-team" ${homeStyle}>${homeAbbr}</td>${homeScoreCells}<td class="sp-bs-ls-total">${homeTot}</td></tr>
                            </tbody>
                        </table>
                    </div>`;
                };

                // Score bar
                const awayN = Number(away.score || 0), homeN = Number(home.score || 0), tot = awayN + homeN;
                const barHtml = (isFinal || isLive) && tot > 0 ? (() => {
                    const pct = Math.round((awayN / tot) * 100);
                    const aHex = teamHex(away.team || {}) || 'ffffff';
                    const hHex = teamHex(home.team || {}) || 'ffffff';
                    return `<div class="sp-score-bar">
                        <div class="sp-score-bar-away" style="width:${pct}%;background:#${aHex}"></div>
                        <div class="sp-score-bar-home" style="width:${100-pct}%;background:#${hHex}"></div>
                    </div>`;
                })() : '';

                return `<div class="sp-boxscore">
                    ${mkTeam(away, 'away')}
                    <div class="sp-bs-divider">
                        <div class="sp-duel-status ${statusClass}">${escapeHtml(statusText)}</div>
                        ${venue ? `<div class="sp-duel-venue">${escapeHtml(venue)}</div>` : ''}
                    </div>
                    ${mkTeam(home, 'home')}
                    ${mkLinescore()}
                    ${barHtml}
                </div>`;
            };

            // ── Duel layout: overlaid on stadium image ──
            const renderDuel = (ev) => {
                const { isFinal, isLive, clock, period, shortDetail, venue, away, home, statusText, statusClass } = gameData(ev);

                const renderHalf = (comp, side) => {
                    const t = comp.team || {};
                    const name = escapeHtml(String(t.shortDisplayName || t.displayName || t.abbreviation || '?'));
                    const abbr = escapeHtml(String(t.abbreviation || '?'));
                    const score = String(comp.score || '');
                    const winner = Boolean(comp.winner);
                    const hex = teamHex(t) || (side === 'away' ? '1a1a2e' : '0f2027');
                    const logoUrl = String(t.logo || '').trim();
                    const logoHtml = logoUrl
                        ? `<img class="sp-duel-logo" src="${safeUrl(logoUrl)}" alt="${abbr}" onerror="this.style.display='none'">`
                        : `<div class="sp-duel-abbr">${abbr}</div>`;
                    return `<div class="sp-duel-half sp-duel-${side}${winner ? ' sp-duel-winner' : ''}" style="--tc:#${hex}">
                        <div class="sp-duel-glow"></div>
                        ${logoHtml}
                        <div class="sp-duel-name">${name}</div>
                        <div class="sp-duel-score${score ? '' : ' sp-duel-score-empty'}">${escapeHtml(score) || '–'}</div>
                        <div class="sp-duel-ha">${side === 'away' ? 'AWAY' : 'HOME'}</div>
                    </div>`;
                };

                const awayN = Number(away.score || 0), homeN = Number(home.score || 0), tot = awayN + homeN;
                const barHtml = (isFinal || isLive) && tot > 0 ? (() => {
                    const pct = Math.round((awayN / tot) * 100);
                    const aHex = teamHex(away.team || {}) || 'ffffff';
                    const hHex = teamHex(home.team || {}) || 'ffffff';
                    return `<div class="sp-score-bar">
                        <div class="sp-score-bar-away" style="width:${pct}%;background:#${aHex}"></div>
                        <div class="sp-score-bar-home" style="width:${100-pct}%;background:#${hHex}"></div>
                    </div>`;
                })() : '';

                return `<div class="sp-duel">
                    ${renderHalf(away, 'away')}
                    <div class="sp-duel-center">
                        <div class="sp-duel-status ${statusClass}">${escapeHtml(statusText)}</div>
                        ${venue ? `<div class="sp-duel-venue">${escapeHtml(venue)}</div>` : ''}
                    </div>
                    ${renderHalf(home, 'home')}
                    ${barHtml}
                </div>`;
            };

            // ── Compact card for multi-game list ──
            const renderCompactCard = (ev) => {
                const comps = (ev.competitions || [{}])[0] || {};
                const competitors = comps.competitors || [];
                const status = ev.status || {};
                const statusType = status.type || {};
                const state = String(statusType.state || 'pre').toLowerCase();
                const isFinal = state === 'post';
                const isLive = state === 'in';
                const clock = String(status.displayClock || '');
                const period = Number(status.period || 0);
                const shortDetail = escapeHtml(String(statusType.shortDetail || ''));

                const away = competitors.find(c => c.homeAway === 'away') || competitors[0] || {};
                const home = competitors.find(c => c.homeAway === 'home') || competitors[1] || {};

                const renderTeamRow = (comp, align) => {
                    const t = comp.team || {};
                    const abbr = escapeHtml(String(t.abbreviation || '?'));
                    const name = escapeHtml(String(t.shortDisplayName || t.displayName || abbr));
                    const score = escapeHtml(String(comp.score || ''));
                    const winner = Boolean(comp.winner);
                    const hex = teamHex(t);
                    const logoUrl = String(t.logo || '').trim();
                    const borderSide = align === 'away' ? 'border-left' : 'border-right';
                    const borderStyle = hex ? `${borderSide}:3px solid #${hex}` : '';
                    return `<div class="sp-compact-team sp-compact-${align}${winner ? ' sp-compact-winner' : ''}" style="${borderStyle}">
                        ${logoUrl ? `<img class="sp-compact-logo" src="${safeUrl(logoUrl)}" alt="${abbr}" onerror="this.style.display='none'">` : ''}
                        <span class="sp-compact-name">${name}</span>
                        ${score ? `<span class="sp-compact-score">${score}</span>` : ''}
                    </div>`;
                };

                const stateLabel = isFinal
                    ? `<span class="sp-compact-state sp-compact-final">${shortDetail || 'F'}</span>`
                    : isLive
                        ? `<span class="sp-compact-state sp-compact-live">● ${period ? `${period}P` : ''} ${clock}</span>`
                        : `<span class="sp-compact-state sp-compact-pre">${shortDetail || String(ev.date || '').slice(0, 10)}</span>`;

                return `<div class="sp-compact-card">
                    ${renderTeamRow(away, 'away')}
                    <div class="sp-compact-mid">${stateLabel}</div>
                    ${renderTeamRow(home, 'home')}
                </div>`;
            };

            let contentHtml = '';
            if (op === 'sports_standings' && standings) {
                const groups = standings.children || [];
                const rows = groups.flatMap(g => {
                    const groupName = escapeHtml(String(g.name || g.abbreviation || ''));
                    const entries = (g.standings && Array.isArray(g.standings.entries)) ? g.standings.entries.slice(0, 8) : [];
                    const entryRows = entries.map(entry => {
                        const team = entry.team || {};
                        const abbr = escapeHtml(String(team.abbreviation || '?'));
                        const fullName = escapeHtml(String(team.shortDisplayName || team.displayName || abbr));
                        const hex = teamHex(team);
                        const dotStyle = hex ? `style="background:#${hex}"` : '';
                        const stats = {};
                        (entry.stats || []).forEach(s => { if (s && s.name) stats[s.name] = s.displayValue || ''; });
                        const w = escapeHtml(stats.wins || '');
                        const l = escapeHtml(stats.losses || '');
                        const pct = escapeHtml(stats.winPercent || stats.gamesBehind || '');
                        return `<tr><td><span class="sp-dot" ${dotStyle}></span>${abbr}</td><td>${fullName}</td><td class="sp-wl">${w}-${l}</td><td class="sp-pct">${pct}</td></tr>`;
                    }).join('');
                    return groupName ? [`<tr class="sp-group-hdr"><td colspan="4">${groupName}</td></tr>`, entryRows] : [entryRows];
                }).join('');
                contentHtml = `<table class="sp-table">${rows}</table>`;
            } else if (op === 'sports_schedule') {
                contentHtml = events.map(ev => renderCompactCard(ev)).join('') || `<div class="sp-empty">No upcoming games found</div>`;
            } else {
                // scores: single focused game → duel (with venue img) or box score (fallback); multi → compact list
                if (events.length === 1) {
                    contentHtml = venueImgUrl ? renderDuel(events[0]) : renderBoxScore(events[0]);
                } else {
                    contentHtml = events.map(ev => renderCompactCard(ev)).join('') || `<div class="sp-empty">No scores found</div>`;
                }
            }

            const titleMap = { sports_scores: 'Scores', sports_schedule: 'Schedule', sports_standings: 'Standings', sports_my_teams: 'My Teams' };
            const titleSuffix = titleMap[op] || op.replace('sports_', '').replace(/_/g, ' ');
            const focusLabel = focusTeam ? escapeHtml(info.team || info.abbrev || '') : '';
            return `
                <div class="scene scene-sports ${escapeAttr(core.theme || '')}${events.length === 1 && op === 'sports_scores' ? ' scene-sports-duel' : ''}">
                    <canvas class="scene-canvas" data-scene="sports"
                        data-away="${escapeAttr(awayHex || '')}"
                        data-home="${escapeAttr(homeHex || '')}"
                        data-venue-img="${escapeAttr(venueImgUrl)}"></canvas>
                    <div class="sp-header">
                        <span class="sp-league">${escapeHtml(league)}</span>
                        <span class="sp-title">${escapeHtml(titleSuffix)}${focusLabel ? ` · ${focusLabel}` : ''}</span>
                    </div>
                    <div class="sp-content">${contentHtml}</div>
                </div>
            `;
        }
        if (core.kind === 'sports_manage') {
            const info = core.info || {};
            const team = escapeHtml(String(info.team || ''));
            const sport = escapeHtml(String(info.sport || '').toUpperCase());
            const op = String(info.op || '');
            const teams = Array.isArray(info.teams) ? info.teams : [];
            const actionLabel = op === 'sports_follow_team' ? `Now following the ${team || 'team'}` : `Unfollowed ${team || 'team'}`;
            const chipHtml = teams.map(t => `<div class="sp-chip">${escapeHtml(String(t.team || t.abbrev || ''))} <span class="sp-chip-sport">${escapeHtml(String(t.sport || '').toUpperCase())}</span></div>`).join('');
            return `
                <div class="scene scene-sports-manage ${escapeAttr(core.theme || '')}">
                    <div class="scene-orb orb-a"></div>
                    <div class="scene-grid"></div>
                    <div class="sp-manage-card">
                        <div class="sp-manage-action">${escapeHtml(actionLabel)}</div>
                        ${sport ? `<div class="sp-manage-badge">${sport}</div>` : ''}
                        ${teams.length ? `<div class="sp-chip-row">${chipHtml}</div>` : '<div class="sp-empty">No teams followed</div>'}
                    </div>
                </div>
            `;
        }
        if (core.kind === 'location') {
            const location = String((core.info || {}).location || core.headline || '').trim();
            return `
                <div class="scene scene-location">
                    <div class="scene-orb orb-a"></div>
                    <div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="scene-pin"></div>
                    <div class="scene-chip-row">
                        <div class="scene-chip">location context</div>
                        <div class="scene-chip">${escapeHtml(location)}</div>
                    </div>
                </div>
            `;
        }
        if (core.kind === 'tasks') {
            const tasks = (this.state.memory?.tasks || []).slice(0, 8);
            const openCount = tasks.filter((t) => !t.done).length;
            const doneCount = tasks.filter((t) => t.done).length;
            const progress = tasks.length ? Math.round((doneCount / Math.max(1, tasks.length)) * 100) : 0;
            const topOpen = tasks.filter((t) => !t.done).slice(0, 6);
            const topDone = tasks.filter((t) => t.done).slice(0, 4);
            const focusTask = topOpen[0] || null;
            const restOpen = topOpen.slice(1);
            const itemsHtml = restOpen.map((item, idx) => `
                <div class="tasks-item">
                    <span class="tasks-item-num">${escapeHtml(String(idx + 2))}</span>
                    <span class="tasks-item-title">${escapeHtml(String(item.title || 'Task'))}</span>
                    <button class="tasks-item-done" type="button" data-command="${escapeAttr(`complete task ${idx + 2}`)}">done</button>
                </div>
            `).join('');
            const doneChips = topDone.map((item) =>
                `<span class="tasks-done-chip">${escapeHtml(String(item.title || 'Task').slice(0, 32))}</span>`
            ).join('');
            return `<div class="scene scene-domain scene-tasks interactive">
                <canvas class="scene-canvas domain-canvas" data-scene="tasks" data-open="${escapeAttr(String(openCount))}" data-done="${escapeAttr(String(doneCount))}"></canvas>
                <div class="tasks-shell">
                    <div class="tasks-progress-wrap">
                        <div class="tasks-progress-label">completion</div>
                        <div class="tasks-progress-value">${escapeHtml(String(progress))}%</div>
                        <div class="tasks-progress-sub">${escapeHtml(String(doneCount))} done · ${escapeHtml(String(openCount))} open</div>
                    </div>
                    ${focusTask ? `<div class="tasks-focus">
                        <div class="tasks-focus-eyebrow">up next</div>
                        <div class="tasks-focus-title">${escapeHtml(String(focusTask.title || 'Task'))}</div>
                        <button class="tasks-focus-done" type="button" data-command="${escapeAttr('complete task 1')}">mark complete</button>
                    </div>` : ''}
                    <div class="tasks-list">${itemsHtml}</div>
                    ${topDone.length ? `<div class="tasks-done-strip">${doneChips}</div>` : ''}
                </div>
            </div>`;
        }
        if (core.kind === 'files') {
            const info = core.info || {};
            if (info.notConnected) {
                return `<div class="scene scene-files interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                    <div class="scene-grid"></div>
                    <div class="files-head">
                        <div class="files-title">${escapeHtml(String(info.service || 'files'))}</div>
                        <div class="files-meta">connector</div>
                    </div>
                    <div class="files-body">
                        <div class="connector-cta">
                            <div class="connector-cta-label">Connect ${escapeHtml(String(info.service || 'drive'))} to view files</div>
                            <button class="connector-cta-btn" data-oauth-connect="${escapeAttr(String(info.service || 'gdrive'))}">Connect</button>
                        </div>
                        <pre class="files-preview">${escapeHtml(String(info.error || info.fallbackReason || 'No live files available.'))}</pre>
                    </div>
                </div>`;
            }
            const items = Array.isArray(info.items) ? info.items.slice(0, 28) : [];
            const op = String(info.op || '').trim();
            const path = String(info.path || '').trim();
            const excerpt = String(info.excerpt || '').trim();
            const lineCount = Number(info.lineCount || 0);
            const storage = String(info.storage || (info.service ? 'connector' : 'workspace')).trim();
            const title = storage === 'connector'
                ? String(info.service || 'connector').trim()
                : (path || 'workspace');
            const meta = storage === 'connector'
                ? `${String(info.service || 'files')} | ${String(items.length || lineCount)}`
                : `${String(op || 'files')} | ${String(items.length || lineCount)}`;
            const storageLabel = storage === 'connector' ? 'connector storage' : 'workspace filesystem';
            const treeHtml = items.map((item) => {
                const name = String(item?.name || item || '').trim();
                const type = String(item?.type || (name.endsWith('/') ? 'dir' : 'file')).trim();
                const cleanBase = path && path !== '.' ? String(path).replace(/\/+$/g, '') : '';
                const cleanName = name.replace(/\/+$/g, '');
                const nodePath = cleanBase ? `${cleanBase}/${cleanName}` : cleanName;
                if (storage === 'connector') {
                    return `<button class="files-node ${escapeAttr(type)}" type="button" data-live-surface="drive" data-drive-query="${escapeAttr(cleanName)}">${escapeHtml(name || '(item)')}</button>`;
                }
                if (type === 'dir') {
                    return `<button class="files-node ${escapeAttr(type)}" type="button" data-command="${escapeAttr(`list files ${nodePath}`)}">${escapeHtml(name || '(item)')}</button>`;
                }
                return `<button class="files-node ${escapeAttr(type)}" type="button" data-command="${escapeAttr(`read file ${nodePath}`)}">${escapeHtml(name || '(item)')}</button>`;
            }).join('');
            return `<div class="scene scene-files interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                <div class="scene-grid"></div>
                <div class="files-head">
                    <div class="files-title">${escapeHtml(title)}</div>
                    <div class="files-meta">${escapeHtml(meta)}</div>
                </div>
                <div class="files-body">
                    <div class="cnt-header">
                        <div class="cnt-action-badge">${escapeHtml(storageLabel)}</div>
                        ${storage === 'workspace' ? `<button class="cnt-type-badge" type="button" data-scene-domain="files">root</button>` : ''}
                        ${storage === 'connector' ? `<button class="cnt-type-badge" type="button" data-scene-domain="connectors">connections</button>` : ''}
                    </div>
                    <div class="files-tree">${treeHtml || `<div class="files-node">${escapeHtml(storage === 'connector' ? 'No connected files' : 'No entries')}</div>`}</div>
                    <pre class="files-preview">${escapeHtml(excerpt || (storage === 'connector' ? 'Select a connected file source to inspect content.' : 'No file preview loaded.'))}</pre>
                </div>
            </div>`;
        }
        if (core.kind === 'expenses') {
            const expenses = (this.state.memory?.expenses || []).slice(0, 12);
            const total = expenses.reduce((sum, item) => sum + Number(item.amount || 0), 0);
            const byCategory = new Map();
            for (const e of expenses) {
                const k = String(e?.category || 'misc').trim().toLowerCase() || 'misc';
                byCategory.set(k, (byCategory.get(k) || 0) + Number(e?.amount || 0));
            }
            const topCats = Array.from(byCategory.entries()).sort((a, b) => b[1] - a[1]).slice(0, 6);
            const avg = expenses.length ? total / expenses.length : 0;
            const recentHtml = expenses.slice(0, 5).map((e) => {
                const cat = String(e?.category || 'misc').trim();
                const note = String(e?.note || '').trim();
                const amt = Number(e?.amount || 0);
                return `<div class="expenses-entry">
                    <span class="expenses-entry-cat">${escapeHtml(cat)}</span>
                    ${note ? `<span class="expenses-entry-note">${escapeHtml(note)}</span>` : ''}
                    <span class="expenses-entry-amt">${escapeHtml(formatCurrency(amt))}</span>
                </div>`;
            }).join('');
            const catsHtml = topCats.map(([cat, amt]) => {
                const pct = Math.round((amt / Math.max(1, total)) * 100);
                return `<div class="expenses-cat-label-row">
                    <span class="expenses-cat-name">${escapeHtml(cat)}</span>
                    <span class="expenses-cat-pct">${pct}%</span>
                </div>`;
            }).join('');
            return `<div class="scene scene-domain scene-expenses interactive">
                <canvas class="scene-canvas domain-canvas" data-scene="expenses" data-total="${escapeAttr(String(total.toFixed(2)))}" data-items="${escapeAttr(String(expenses.length))}"></canvas>
                <div class="expenses-shell">
                    <div class="expenses-hero">
                        <div class="expenses-hero-label">total spend</div>
                        <div class="expenses-hero-value">${escapeHtml(formatCurrency(total))}</div>
                        <div class="expenses-hero-sub">${escapeHtml(String(expenses.length))} entries &middot; avg ${escapeHtml(formatCurrency(avg))}</div>
                    </div>
                    <div class="expenses-breakdown">${catsHtml || '<div class="expenses-cat-label-row"><span class="expenses-cat-name">no spend yet</span></div>'}</div>
                    <div class="expenses-recent">${recentHtml || '<div class="expenses-entry"><span class="expenses-entry-cat">No expenses yet</span></div>'}</div>
                </div>
            </div>`;
        }
        if (core.kind === 'notes') {
            const notes = (this.state.memory?.notes || []).slice(0, 12);
            // Position fragments across the canvas in a loose grid with offsets
            const cols = 3, rows = 4;
            const fragmentsHtml = notes.map((n, idx) => {
                const text = String(n.text || '').trim();
                const words = text.split(/\s+/).slice(0, 14).join(' ');
                const col = idx % cols;
                const row = Math.floor(idx / cols) % rows;
                const leftPct = 6 + col * 30 + (idx % 2 === 0 ? 2 : -2);
                const topPct  = 14 + row * 20 + (idx % 3 === 0 ? 3 : 0);
                const opacity = Math.max(0.45, 1 - idx * 0.07);
                const size    = idx === 0 ? 18 : idx < 3 ? 14 : 12;
                return `<div class="notes-fragment" style="left:${leftPct}%;top:${topPct}%;opacity:${opacity};font-size:${size}px;">${escapeHtml(words || 'note')}</div>`;
            }).join('');
            const countLabel = notes.length ? `${notes.length} notes` : 'no notes yet';
            return `<div class="scene scene-domain scene-notes interactive">
                <canvas class="scene-canvas domain-canvas" data-scene="notes" data-count="${escapeAttr(String(notes.length))}"></canvas>
                <div class="notes-shell">
                    <div class="notes-count-label">${escapeHtml(countLabel)}</div>
                    <div class="notes-field">${fragmentsHtml || '<div class="notes-fragment" style="left:8%;top:20%;opacity:0.5;font-size:16px;">No notes yet.</div>'}</div>
                </div>
            </div>`;
        }
        if (core.kind === 'reminders') {
            const info = core.info || {};
            const lines = Array.isArray(info.lines) ? info.lines : [];
            const isSet = String(info.op || '') === 'schedule_remind_once';
            const isEmpty = lines.length === 0 ||
                (lines.length === 1 && String(lines[0]).toLowerCase().includes('no'));
            const rowsHtml = (isEmpty ? ['No active reminders'] : lines).map((line, idx) => {
                const parts = String(line).split('|').map((s) => s.trim());
                const isHeader = parts.length < 3;
                const text  = isHeader ? String(line) : (parts[2] || parts[0]);
                const isOnce = String(parts[1] || '').toLowerCase().includes('once');
                const rid   = parts[4] || '';
                return `<div class="reminders-row">
                    <span class="reminders-row-index">${escapeHtml(String(idx + 1))}</span>
                    ${!isHeader ? `<span class="reminders-row-badge ${isOnce ? 'once' : 'repeat'}">${isOnce ? 'once' : 'repeat'}</span>` : ''}
                    <span class="reminders-row-text">${escapeHtml(text)}</span>
                    ${rid ? `<button class="reminders-row-cancel" type="button" data-command="${escapeAttr(`cancel reminder ${rid}`)}" title="Cancel">✕</button>` : ''}
                </div>`;
            }).join('');
            const ctaHtml = isSet
                ? `<div class="reminders-cta"><button class="reminders-cta-link" type="button" data-command="show my reminders">view all reminders</button></div>`
                : '';
            return `<div class="scene scene-domain scene-reminders interactive">
                <canvas class="scene-canvas domain-canvas" data-scene="reminders"
                    data-count="${escapeAttr(String(lines.length))}"></canvas>
                <div class="reminders-shell">
                    <div class="reminders-header">
                        <div class="reminders-title">scheduled reminders</div>
                        <div class="reminders-count">${escapeHtml(isEmpty ? '0 active' : `${lines.length} active`)}</div>
                    </div>
                    <div class="reminders-list">${rowsHtml}</div>
                    ${ctaHtml}
                </div>
            </div>`;
        }
        if (core.kind === 'graph') {
            const snapshot = this.state.session.graphSnapshot || {};
            const entitiesRaw = Array.isArray(snapshot.entities) ? snapshot.entities : [];
            const relationsRaw = Array.isArray(snapshot.relations) ? snapshot.relations : [];
            const nodes = entitiesRaw
                .slice(-30)
                .map((entity, idx) => ({
                    id: String(entity?.id || `node-${idx}`),
                    kind: String(entity?.kind || 'entity'),
                    label: this.graphNodeLabel(entity, idx),
                }));
            const nodeById = new Map(nodes.map((n) => [n.id, n]));
            const links = relationsRaw
                .slice(-70)
                .map((rel) => ({
                    sourceId: String(rel?.sourceId || ''),
                    targetId: String(rel?.targetId || ''),
                    relation: String(rel?.relation || 'link'),
                }))
                .filter((rel) => rel.sourceId && rel.targetId && nodeById.has(rel.sourceId) && nodeById.has(rel.targetId));
            const nodeCount = nodes.length;
            const centerX = 400;
            const centerY = 180;
            const radiusX = 282;
            const radiusY = 124;
            const pos = {};
            nodes.forEach((node, idx) => {
                const theta = (Math.PI * 2 * idx) / Math.max(1, nodeCount);
                const jitter = ((this.hashText(node.id) % 13) - 6) * 1.2;
                pos[node.id] = {
                    x: Math.round(centerX + Math.cos(theta) * (radiusX + jitter)),
                    y: Math.round(centerY + Math.sin(theta) * (radiusY + jitter * 0.5)),
                };
            });
            const linkSvg = links.map((link) => {
                const a = pos[link.sourceId];
                const b = pos[link.targetId];
                if (!a || !b) return '';
                return `<line class="graph-link" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"></line>`;
            }).join('');
            const nodeSvg = nodes.map((node) => {
                const p = pos[node.id];
                const r = node.kind === 'task' ? 13 : node.kind === 'note' ? 10 : 11;
                const cls = `graph-node graph-node-${escapeAttr(node.kind.toLowerCase())}`;
                return `<g class="${cls}">
                    <circle cx="${p.x}" cy="${p.y}" r="${r}"></circle>
                    <text x="${p.x}" y="${p.y + r + 12}" text-anchor="middle">${escapeHtml(node.label)}</text>
                </g>`;
            }).join('');
            const summary = snapshot.counts || {};
            const hasGraph = nodeCount > 0;
            return `
                <div class="scene scene-domain scene-graph">
                    <canvas class="scene-canvas graph-canvas" data-scene="graph"></canvas>
                    <div class="graph-summary-strip">
                        <div class="graph-summary-chip">entities ${escapeHtml(String(summary.entities || nodeCount || 0))}</div>
                        <div class="graph-summary-chip">relations ${escapeHtml(String(summary.relations || links.length || 0))}</div>
                        <div class="graph-summary-chip">events ${escapeHtml(String(summary.events || 0))}</div>
                    </div>
                    ${hasGraph ? `<svg class="graph-svg graph-live" viewBox="0 0 800 360" preserveAspectRatio="none" aria-label="Graph relations">${linkSvg}${nodeSvg}</svg>` : '<div class="graph-empty">No graph entities yet. Try: add task ship onboarding</div>'}
                </div>
            `;
        }
        if (core.kind === 'mcp') {
            const info = core.info || {};
            const serverName = String(info.serverName || 'Connected App').trim();
            const toolName   = String(info.toolName || '').trim();
            const textItems  = Array.isArray(info.textItems) ? info.textItems : [];
            const imageItems = Array.isArray(info.imageItems) ? info.imageItems : [];
            const matchPct   = Math.round((Number(info.matchScore || 0)) * 100);

            const textHtml = textItems.length
                ? textItems.map(t => `<p class="mcp-text-block">${escapeHtml(t)}</p>`).join('')
                : '<p class="mcp-text-block mcp-empty">No text response from tool.</p>';

            const imgHtml = imageItems.map(img =>
                `<img class="mcp-image" src="${safeUrl(String(img.url || ''))}" alt="MCP image" loading="lazy" />`
            ).join('');

            return `
                <div class="scene scene-mcp">
                    <canvas class="scene-canvas mcp-canvas" data-scene="mcp"></canvas>
                    <div class="mcp-scene-body">
                        <div class="mcp-header">
                            <span class="mcp-server-chip">${escapeHtml(serverName)}</span>
                            ${toolName ? `<span class="mcp-tool-chip">${escapeHtml(toolName)}</span>` : ''}
                            ${matchPct ? `<span class="mcp-match-chip">${matchPct}% match</span>` : ''}
                        </div>
                        <div class="mcp-content">
                            ${imgHtml}
                            ${textHtml}
                        </div>
                    </div>
                </div>
            `;
        }
        if (core.kind === 'webdeck') {
            const info = core.info || {};
            const op = String(info.op || 'fetch_url');
            const isSearch = op === 'web_search';
            const isMobile = typeof window !== 'undefined' && window.innerWidth <= 600;
            const isElectron = Boolean(window.electronAPI?.isElectron);
            const webdeckMode = String(this.state.webdeck?.mode || 'surface');
            const isFullMode = webdeckMode === 'full';
            const url = String(info.url || '').trim();
            const title = String(info.title || (isSearch ? `Search: ${String(info.query || '').trim()}` : url)).trim();
            const excerpt = String(info.excerpt || '').trim();
            const favicon = String(info.favicon || '').trim();
            const thumbnail = String(info.thumbnail || '').trim();
            const source = String(info.source || 'scaffold').trim();
            const items = Array.isArray(info.items) ? info.items.slice(0, 12) : [];
            const siteTarget = (info.siteTarget && typeof info.siteTarget === 'object') ? info.siteTarget : null;

            const safeHostname = (u) => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return String(u || '').slice(0, 40); } };
            const displayUrl = url ? safeHostname(url) : (isSearch ? 'web search' : '');
            const barText = isSearch ? `Search: ${String(info.query || '')}` : (title.slice(0, 80) || displayUrl);
            const faviconHtml = favicon
                ? `<img class="webdeck-favicon" src="${safeUrl(favicon)}" alt="" loading="lazy" onerror="this.style.display='none'">`
                : '<div class="webdeck-favicon-fallback"></div>';
            const resultsHtml = isSearch
                ? items.map((item) => {
                    const itemTitle = String(item.title || '').trim().slice(0, 90);
                    const itemUrl = String(item.url || '').trim();
                    const itemSnippet = String(item.snippet || '').trim().slice(0, 160);
                    const itemHost = String(item.host || '').trim() || (itemUrl ? safeHostname(itemUrl) : '');
                    const itemThumb = String(item.thumbnail || '').trim();
                    const itemFav = String(item.favicon || '').trim();
                    return `<a class="webdeck-result-card" href="${safeUrl(itemUrl)}" target="_blank" rel="noopener noreferrer">
                        <div class="webdeck-result-media">
                            ${itemThumb ? `<img class="webdeck-result-thumb" src="${safeUrl(itemThumb)}" alt="${escapeAttr(itemTitle)}" loading="lazy" onerror="this.style.display='none'">` : `<div class="webdeck-result-thumb-fallback">${itemFav ? `<img class="webdeck-result-favicon" src="${safeUrl(itemFav)}" alt="" loading="lazy" onerror="this.style.display='none'">` : ''}</div>`}
                        </div>
                        <div class="webdeck-result-title">${escapeHtml(itemTitle)}</div>
                        <div class="webdeck-result-host">${escapeHtml(itemHost)}</div>
                        ${itemSnippet ? `<div class="webdeck-result-snippet">${escapeHtml(itemSnippet)}</div>` : ''}
                    </a>`;
                }).join('')
                : (excerpt
                    ? `<div class="webdeck-page-preview">
                        ${thumbnail ? `<img class="webdeck-page-hero" src="${safeUrl(thumbnail)}" alt="${escapeAttr(title || 'Page preview')}" loading="lazy" onerror="this.style.display='none'">` : ''}
                        <div class="webdeck-page-text">${escapeHtml(excerpt.slice(0, 400))}</div>
                    </div>`
                    : '');
            const directBtn = siteTarget
                ? `<a class="webdeck-direct-btn scene-chip scene-chip-link" href="${safeUrl(String(siteTarget.url || ''))}" target="_blank" rel="noopener noreferrer">${escapeHtml(String(siteTarget.label || 'Open source'))}</a>`
                : (url ? `<a class="webdeck-direct-btn scene-chip scene-chip-link" href="${safeUrl(url)}" target="_blank" rel="noopener noreferrer">Open page</a>` : '');
            const mobileClass = isMobile ? ' webdeck-mobile' : '';
            const modeClass = isFullMode ? ' webdeck-full' : '';
            const sourceUrl = String(siteTarget?.url || url || '').trim();
            const sourceHost = String(siteTarget?.host || '').trim() || (sourceUrl ? safeHostname(sourceUrl) : '');
            const frameUrl = sourceUrl || (items.length ? String(items[0]?.url || '').trim() : '');
            const topItemUrl = items.length ? String((items[0] || {}).url || '').trim() : '';
            const query = String(info.query || '').trim();
            const actionCommands = (() => {
                const actions = [];
                const seen = new Set();
                const add = (cmd) => {
                    const value = String(cmd || '').trim();
                    if (!value) return;
                    const key = value.toLowerCase();
                    if (seen.has(key)) return;
                    seen.add(key);
                    actions.push(value);
                };
                if (isSearch && query) {
                    add(`search web ${query}`);
                }
                if (topItemUrl) {
                    add(`summarize website ${topItemUrl}`);
                }
                if (url) {
                    add(`summarize website ${url}`);
                }
                if (!actions.length && query) {
                    add(`search web ${query}`);
                }
                return actions.slice(0, 3);
            })();
            const actionButtons = actionCommands.map((cmd) => {
                const label = cmd
                    .replace(/^search web\s+/i, 'search: ')
                    .replace(/^summarize website\s+/i, 'summarize: ');
                return `<button class="webdeck-action-btn" type="button" data-command="${escapeAttr(cmd)}">${escapeHtml(label)}</button>`;
            }).join('');
            const hostCounts = (() => {
                const counts = new Map();
                for (const item of items) {
                    const host = String(item?.host || '').trim() || (String(item?.url || '').trim() ? safeHostname(String(item.url)) : '');
                    if (!host) continue;
                    counts.set(host, (counts.get(host) || 0) + 1);
                }
                return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).slice(0, 4);
            })();
            const hostListHtml = hostCounts.length
                ? hostCounts.map(([host, count]) => `<div class="webdeck-host-row"><span>${escapeHtml(String(host))}</span><span>${escapeHtml(String(count))}</span></div>`).join('')
                : '<div class="webdeck-host-empty">No host breakdown</div>';
            const fullViewUrl = frameUrl || '';
            const liveSurfaceHtml = (isFullMode && fullViewUrl)
                ? (isElectron
                    ? `
                                <div class="webdeck-live-surface">
                                    <webview class="webdeck-live-frame webdeck-webview" src="${safeUrl(fullViewUrl)}" allowpopups partition="persist:genome-browser"></webview>
                                    <div class="webdeck-live-meta">
                                        <span>live source: ${escapeHtml(safeHostname(fullViewUrl))}</span>
                                        <a href="${safeUrl(fullViewUrl)}" target="_blank" rel="noopener noreferrer">open in tab</a>
                                    </div>
                                </div>
                            `
                    : `
                                <div class="webdeck-live-surface">
                                    <iframe class="webdeck-live-frame" src="${safeUrl(fullViewUrl)}" title="${escapeAttr(title || 'Web live surface')}" loading="eager" referrerpolicy="no-referrer" sandbox="allow-scripts allow-forms allow-popups allow-top-navigation-by-user-activation"></iframe>
                                    <div class="webdeck-live-meta">
                                        <span>live source: ${escapeHtml(safeHostname(fullViewUrl))}</span>
                                        <a href="${safeUrl(fullViewUrl)}" target="_blank" rel="noopener noreferrer">open in tab</a>
                                    </div>
                                </div>
                            `)
                : '';
            return `<div class="scene scene-webdeck interactive">
                <canvas class="scene-canvas webdeck-canvas" data-scene="webdeck"></canvas>
                <div class="webdeck${mobileClass}${modeClass}">
                    <div class="webdeck-chrome">
                        <div class="webdeck-nav-dots"><span></span><span></span><span></span></div>
                        ${faviconHtml}
                        <div class="webdeck-bar">${escapeHtml(barText)}</div>
                        <div class="webdeck-source-chip">${escapeHtml(source)}</div>
                        <button class="webdeck-mode-btn" type="button" data-webdeck-mode-toggle>${isFullMode ? 'surface view' : 'full view'}</button>
                        ${directBtn}
                    </div>
                    ${actionButtons ? `<div class="webdeck-actions">${actionButtons}</div>` : ''}
                    <div class="webdeck-body">
                        <div class="webdeck-results ${liveSurfaceHtml ? 'with-live-frame' : ''}">
                            ${liveSurfaceHtml}
                            ${resultsHtml || '<div class="webdeck-empty">No content preview.</div>'}
                        </div>
                        <aside class="webdeck-inspector">
                            <div class="webdeck-inspector-card">
                                <div class="webdeck-inspector-label">Route</div>
                                <div class="webdeck-inspector-value">${escapeHtml(String(siteTarget?.mode || (isSearch ? 'search' : 'page')))}</div>
                            </div>
                            <div class="webdeck-inspector-card">
                                <div class="webdeck-inspector-label">Source</div>
                                <div class="webdeck-inspector-value">${escapeHtml(sourceHost || source || 'unknown')}</div>
                                ${sourceUrl ? `<a class="webdeck-inspector-link" href="${safeUrl(sourceUrl)}" target="_blank" rel="noopener noreferrer">open source</a>` : ''}
                            </div>
                            <div class="webdeck-inspector-card">
                                <div class="webdeck-inspector-label">Hosts</div>
                                <div class="webdeck-host-list">${hostListHtml}</div>
                            </div>
                        </aside>
                    </div>
                </div>
            </div>`;
        }
        // ── Computer layer scenes ──────────────────────────────────────────────
        if (core.kind === 'document') {
            const info   = core.info || {};
            const action = String(info.action || 'create').trim();
            const name   = String(info.name  || '').trim();
            const topic  = String(info.topic || '').trim();
            const title  = name || topic || (action === 'edit' ? 'Existing Document' : 'New Document');
            const branch = String(info.branch || this.state.session.workspace?.activeContent?.branch || 'main').trim();
            const hash = String(info.hash || this.state.session.workspace?.activeContent?.hash || '').trim();
            return `<div class="scene scene-computer scene-document interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="document"></canvas>
                <div class="functional-surface">
                    <div class="func-toolbar" data-func-toolbar="document">
                        <button data-cmd="bold" title="Bold"><b>B</b></button>
                        <button data-cmd="italic" title="Italic"><i>I</i></button>
                        <button data-cmd="underline" title="Underline"><u>U</u></button>
                        <div class="func-toolbar-sep"></div>
                        <button data-cmd="formatBlock:h1" title="Heading 1">H1</button>
                        <button data-cmd="formatBlock:h2" title="Heading 2">H2</button>
                        <button data-cmd="insertHorizontalRule" title="Divider">—</button>
                        <button data-cmd="insertUnorderedList" title="Bullet list">•</button>
                        <button data-cmd="insertOrderedList" title="Numbered list">1.</button>
                        <span class="func-doc-name">${escapeHtml(title)}</span>
                        <button class="func-branch-badge" type="button" data-repo-branch="1" data-repo-domain="document" data-repo-name="${escapeAttr(name || title)}" data-repo-branch="${escapeAttr(branch)}">${escapeHtml(branch)}</button>
                        <button class="func-hash-badge" type="button" data-repo-history="1" data-repo-domain="document" data-repo-name="${escapeAttr(name || title)}">${escapeHtml(hash ? hash.slice(0, 8) : 'new')}</button>
                        <span class="func-save-status" data-save-status>Saved</span>
                    </div>
                    <div class="func-doc-body" contenteditable="true"
                         data-func-domain="document" data-func-name="${escapeAttr(name || title)}">
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'spreadsheet') {
            const info = core.info || {};
            const name = String(info.name || 'New Spreadsheet').trim();
            const branch = String(info.branch || this.state.session.workspace?.activeContent?.branch || 'main').trim();
            const hash = String(info.hash || this.state.session.workspace?.activeContent?.hash || '').trim();
            const COLS = ['A','B','C','D','E','F','G','H'];
            const ROWS = 20;
            const thHtml = `<th class="row-num-head"></th>` + COLS.map((c) => `<th>${c}</th>`).join('');
            const tdHtml = Array.from({length: ROWS}, (_, ri) =>
                `<tr><td class="row-num">${ri + 1}</td>` +
                COLS.map((c) => `<td contenteditable="true" data-cell="${c}${ri + 1}"></td>`).join('') +
                `</tr>`).join('');
            return `<div class="scene scene-computer scene-spreadsheet interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="spreadsheet"></canvas>
                <div class="functional-surface">
                    <div class="func-toolbar" data-func-toolbar="spreadsheet">
                        <span class="func-cell-ref" data-cell-ref>A1</span>
                        <input class="func-formula-bar" data-formula-bar placeholder="Value or formula…" />
                        <span class="func-doc-name">${escapeHtml(name)}</span>
                        <button class="func-branch-badge" type="button" data-repo-branch="1" data-repo-domain="spreadsheet" data-repo-name="${escapeAttr(name)}" data-repo-branch="${escapeAttr(branch)}">${escapeHtml(branch)}</button>
                        <button class="func-hash-badge" type="button" data-repo-history="1" data-repo-domain="spreadsheet" data-repo-name="${escapeAttr(name)}">${escapeHtml(hash ? hash.slice(0, 8) : 'new')}</button>
                        <span class="func-save-status" data-save-status>Saved</span>
                    </div>
                    <div class="func-sheet-wrap">
                        <table class="func-sheet-grid" data-func-domain="spreadsheet" data-func-name="${escapeAttr(name)}">
                            <thead><tr>${thHtml}</tr></thead>
                            <tbody>${tdHtml}</tbody>
                        </table>
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'presentation') {
            const info  = core.info || {};
            const name  = String(info.name  || '').trim();
            const topic = String(info.topic || '').trim();
            const title = name || topic || 'New Presentation';
            const branch = String(info.branch || this.state.session.workspace?.activeContent?.branch || 'main').trim();
            const hash = String(info.hash || this.state.session.workspace?.activeContent?.hash || '').trim();
            // One starter slide
            const thumbHtml = `<div class="func-pres-thumb-wrap">
                <div class="func-pres-thumb active" data-slide-index="0"></div>
                <div class="func-pres-thumb-num">1</div>
            </div>`;
            return `<div class="scene scene-computer scene-presentation interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="presentation"></canvas>
                <div class="functional-surface">
                    <div class="func-toolbar" data-func-toolbar="presentation">
                        <button data-slide-cmd="add" title="Add slide">+ Slide</button>
                        <button data-slide-cmd="delete" title="Delete slide">Delete</button>
                        <span class="func-doc-name">${escapeHtml(title)}</span>
                        <button class="func-branch-badge" type="button" data-repo-branch="1" data-repo-domain="presentation" data-repo-name="${escapeAttr(name || title)}" data-repo-branch="${escapeAttr(branch)}">${escapeHtml(branch)}</button>
                        <button class="func-hash-badge" type="button" data-repo-history="1" data-repo-domain="presentation" data-repo-name="${escapeAttr(name || title)}">${escapeHtml(hash ? hash.slice(0, 8) : 'new')}</button>
                        <span class="func-save-status" data-save-status>Saved</span>
                    </div>
                    <div class="func-pres-wrap">
                        <div class="func-pres-panel" data-pres-panel data-func-name="${escapeAttr(name || title)}">
                            ${thumbHtml}
                        </div>
                        <div class="func-pres-stage">
                            <div class="func-pres-slide" contenteditable="true"
                                 data-func-domain="presentation" data-func-name="${escapeAttr(name || title)}"
                                 data-slide-index="0">
                                <h1>${escapeHtml(title)}</h1>
                                <p>${escapeHtml(topic || 'Click to start editing')}</p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'code') {
            const info     = core.info || {};
            const language = String(info.language || 'text').trim();
            const name     = String(info.name     || info.topic || 'untitled').trim();
            return `<div class="scene scene-computer scene-code interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="code"></canvas>
                <div class="functional-surface">
                    <div class="func-toolbar" data-func-toolbar="code">
                        <span class="func-lang-badge">${escapeHtml(language)}</span>
                        <span class="func-doc-name">${escapeHtml(name)}</span>
                        <span class="func-save-status" data-save-status>Saved</span>
                    </div>
                    <div class="func-code-wrap" data-func-domain="code"
                         data-func-name="${escapeAttr(name)}" data-func-lang="${escapeAttr(language)}">
                        <textarea class="func-code-textarea" spellcheck="false" autocorrect="off" autocapitalize="off"></textarea>
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'terminal') {
            return `<div class="scene scene-computer scene-terminal interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="terminal"></canvas>
                <div class="functional-surface">
                    <div class="func-terminal-bar">
                        <div class="func-terminal-dots">
                            <div class="func-terminal-dot red"></div>
                            <div class="func-terminal-dot yellow"></div>
                            <div class="func-terminal-dot green"></div>
                        </div>
                        <div class="func-terminal-title">terminal</div>
                    </div>
                    <div class="func-terminal-output" data-terminal-output></div>
                    <div class="func-terminal-input-row">
                        <span class="func-terminal-prompt">$</span>
                        <input class="func-terminal-input" data-terminal-input
                               autocomplete="off" spellcheck="false" autocorrect="off" />
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'calendar') {
            const info   = core.info || {};
            if (info.notConnected) {
                return `<div class="scene scene-computer scene-calendar interactive">
                    <canvas class="scene-canvas computer-canvas" data-scene="calendar"></canvas>
                    <div class="cal-shell">
                        <div class="connector-cta">
                            <div class="connector-cta-label">Google Calendar not connected</div>
                            <button class="connector-cta-btn" data-oauth-connect="${escapeAttr(info.service || 'gcal')}">Connect Calendar</button>
                            <button class="connector-cta-btn" type="button" data-scene-domain="connectors">Open connections</button>
                        </div>
                    </div>
                </div>`;
            }
            const action = String(info.action || 'list').trim();
            const title  = String(info.title  || '').trim();
            const date   = String(info.date   || 'today').trim();
            const isCreate = action === 'create';
            const connectorEvents = Array.isArray(info.events) ? info.events : [];
            const events = connectorEvents;
            const focused = isCreate
                ? { time: date, label: title || 'New Event' }
                : (() => {
                    const e = events[0] || {};
                    const startRaw = String(e.start || e.startTime || e.time || '').replace('T', ' ');
                    return { time: startRaw.slice(11, 16) || startRaw.slice(0, 10) || '—', label: String(e.summary || e.title || e.label || 'Event') };
                })();
            const restHtml = (isCreate ? events : events.slice(1)).slice(0, 4).map((e, i) => {
                const startRaw = String(e.start || e.startTime || e.time || '').replace('T', ' ');
                const t = startRaw.slice(11, 16) || startRaw.slice(0, 10) || '';
                const lbl = String(e.summary || e.title || e.label || 'Event').slice(0, 48);
                return `<div class="cal-stream-item" style="opacity:${0.5 - i * 0.08}">
                    <span class="cal-stream-time">${escapeHtml(t)}</span>
                    <span class="cal-stream-title">${escapeHtml(lbl)}</span>
                </div>`;
            }).join('');
            return `<div class="scene scene-computer scene-calendar interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="calendar"></canvas>
                <div class="cal-shell">
                    <div class="cal-eyebrow">${escapeHtml(date)}</div>
                    <div class="cal-focus-time">${escapeHtml(focused.time)}</div>
                    <div class="cal-focus-title">${escapeHtml(focused.label)}</div>
                    ${isCreate ? '<div class="cal-creating">scheduling···</div>' : ''}
                    <div class="cal-stream">${!isCreate && !events.length ? '<div class="cnt-empty">No upcoming events available.</div>' : restHtml}</div>
                </div>
            </div>`;
        }
        if (core.kind === 'email') {
            const info    = core.info || {};
            const action  = String(info.action  || 'read').trim();
            const to      = String(info.to      || '').trim();
            const subject = String(info.subject || '').trim();
            const from    = String(info['from'] || '').trim();
            const query   = String(info.query   || '').trim();
            const isCompose = action === 'compose' || action === 'reply';
            const connectorMessages = Array.isArray(info.messages) ? info.messages : [];
            const msgs = connectorMessages;
            const unread = msgs.filter(m => m.unread).length;
            const focused = msgs[0] || {};
            const streamHtml = msgs.slice(1, 5).map((m, i) => `
                <div class="email-stream-item" style="opacity:${0.45 - i * 0.08}">
                    <span class="email-stream-from">${escapeHtml(String(m.from || '').slice(0, 28))}</span>
                    <span class="email-stream-dot">·</span>
                    <span class="email-stream-subject">${escapeHtml(String(m.subject || '').slice(0, 48))}</span>
                </div>`).join('');
            const provider = String(info.provider || info.service || 'gmail').toLowerCase();
            const providerLabel = provider === 'email' ? 'Email' : provider.charAt(0).toUpperCase() + provider.slice(1);
            const notConnected = Boolean(info.notConnected);
            const connectHtml = notConnected ? `
                <div class="connector-cta">
                    <div class="connector-cta-label">Connect your email</div>
                    <input class="connector-email-input" type="email" placeholder="you@example.com" data-email-connect-input />
                    <button class="connector-cta-btn" data-action="email-connect">Continue</button>
                    <button class="connector-cta-btn" type="button" data-scene-domain="connectors">Open connections</button>
                </div>` : '';
            const emptyInboxHtml = `
                <div class="email-unread-wrap">
                    <div class="email-unread-value">0</div>
                    <div class="email-unread-label">messages</div>
                </div>
                <div class="cnt-empty">${escapeHtml(info.error || `No live messages available from ${providerLabel}.`)}</div>`;
            const inboxHtml = notConnected ? connectHtml : `
                <div class="email-unread-wrap">
                    <div class="email-unread-value">${unread || msgs.length}</div>
                    <div class="email-unread-label">${unread ? 'unread' : 'messages'}</div>
                </div>
                <div class="email-focus">
                    <div class="email-focus-eyebrow">${escapeHtml(String(focused.date || '').slice(0, 16))}</div>
                    <div class="email-focus-from">${escapeHtml(String(focused.from || '').slice(0, 40))}</div>
                    <div class="email-focus-subject">${escapeHtml(String(focused.subject || '').slice(0, 60))}</div>
                    <div class="email-focus-snippet">${escapeHtml(String(focused.snippet || '').slice(0, 100))}</div>
                </div>
                <div class="email-stream">${streamHtml}</div>`;
            const composeHtml = `
                <div class="email-compose-eyebrow">${escapeHtml(action)}</div>
                <div class="email-compose-to">${escapeHtml(to || '···')}</div>
                <div class="email-compose-subject">${escapeHtml(subject || 'New message')}</div>
                <div class="email-compose-cursor"></div>`;
            return `<div class="scene scene-computer scene-email interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="email"></canvas>
                <div class="email-shell ${isCompose ? 'email-shell--compose' : ''}">
                    ${isCompose ? composeHtml : (msgs.length ? inboxHtml : emptyInboxHtml)}
                </div>
            </div>`;
        }
        if (core.kind === 'notifications') {
            const info = core.info || {};
            const items = Array.isArray(info.items) ? info.items : [];
            const rows = items
                .slice()
                .sort((a, b) => Number(b.ts || b.createdAt || 0) - Number(a.ts || a.createdAt || 0))
                .map((item) => {
                    const ts = Number(item.ts || item.createdAt || 0);
                    const time = ts ? formatDate(ts) : '';
                    const title = String(item.title || item.type || 'Notification').trim();
                    const message = String(item.message || '').trim();
                    const route = String(item.route || '').trim();
                    const severity = String(item.severity || 'info').trim() || 'info';
                    return `
                        <button class="cnt-hist-item ${item.read ? '' : 'current'}" type="button"${route ? ` data-command="${escapeAttr(route)}"` : ` data-scene-domain="notifications"`}>
                            <div class="cnt-hist-ver">${escapeHtml(severity)}</div>
                            <div class="cnt-hist-msg">${escapeHtml(title)}${message ? ` · ${escapeHtml(message)}` : ''}</div>
                            <div class="cnt-hist-time">${escapeHtml(time)}</div>
                        </button>`;
                }).join('');
            const sourceLabel = info.source === 'live' ? 'runtime inbox' : String(info.source || 'notifications').trim();
            return `<div class="scene scene-computer scene-content interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="notifications"></canvas>
                <div class="cnt-shell">
                    <div class="cnt-header">
                        <div class="cnt-action-badge">notifications</div>
                        <div class="cnt-type-badge">${escapeHtml(sourceLabel)}</div>
                        <button class="cnt-type-badge" type="button" data-notifications-mark-read="all">mark read</button>
                        <button class="cnt-type-badge" type="button" data-notifications-clear="all">clear</button>
                    </div>
                    <div class="cnt-history">${rows || '<div class="cnt-empty">No runtime notifications yet.</div>'}</div>
                </div>
            </div>`;
        }
        if (core.kind === 'content') {
            const info     = core.info || {};
            const action   = String(info.action || 'find').trim();
            const name     = String(info.name   || '').trim();
            const type     = String(info.type   || '').trim();
            const isHistory = ['history', 'branch', 'merge', 'revert'].includes(action);
            const isWorktrees = action === 'worktrees';
            const listItems = Array.isArray(info.items) ? info.items : [];
            const histItems = Array.isArray(info.history) ? info.history : [];
            const branchItems = Array.isArray(info.branches) ? info.branches : [];
            const workspace = this.state.session.workspace || {};
            const rawWorktrees = Array.isArray(info.worktrees)
                ? info.worktrees
                : Object.values((workspace.worktrees && typeof workspace.worktrees === 'object') ? workspace.worktrees : {});
            const worktreeItems = rawWorktrees;
            const activeItemId = String(workspace.activeContent?.itemId || '');
            const runtimeFocus = (info.runtimeFocus && typeof info.runtimeFocus === 'object') ? info.runtimeFocus : ((workspace.activeContent && typeof workspace.activeContent === 'object') ? workspace.activeContent : null);
            const presence = (info.presence && typeof info.presence === 'object') ? info.presence : (this.state.session.presence || {});
            const handoff = (info.handoff && typeof info.handoff === 'object') ? info.handoff : (this.state.session.handoff || {});
            const attachedIds = new Set(worktreeItems.map((item) => String(item?.itemId || '')));
            const repoTime = (value) => {
                const n = Number(value || 0);
                if (!n) return '';
                return formatDate(n < 1e12 ? n * 1000 : n);
            };
            const activeDevices = Number(presence.activeCount || presence.count || 0);
            const runtimeHtml = `
                <div class="cnt-header">
                    <div class="cnt-action-badge">runtime</div>
                    ${workspace?.repoId ? `<div class="cnt-type-badge">${escapeHtml(String(workspace.repoId || 'user-global'))}</div>` : ''}
                    ${runtimeFocus?.name ? `<div class="cnt-type-badge">${escapeHtml(String(runtimeFocus.name || ''))}</div>` : ''}
                    ${runtimeFocus?.branch ? `<div class="cnt-type-badge">${escapeHtml(String(runtimeFocus.branch || 'main'))}</div>` : ''}
                    <div class="cnt-type-badge">${escapeHtml(String(activeDevices))} active</div>
                    ${handoff?.activeDeviceId ? `<div class="cnt-type-badge">${escapeHtml(String(handoff.activeDeviceId))}</div>` : ''}
                </div>`;
            const listHtml = listItems.map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${escapeAttr(String(item.domain || item.type || 'content'))}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.name || 'Untitled'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.headMessage || item.summary || item.domain || item.type || 'content').slice(0, 72))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.branch || 'main'))}</div>
                    <div class="cnt-item-time">${escapeHtml(repoTime(item.updated_at))}</div>
                    <span class="cnt-type-badge">${attachedIds.has(String(item.itemId || '')) ? 'attached' : 'repo'}</span>
                    ${Number(item.revisionCount || 0) > 0 ? `<span class="cnt-type-badge">${escapeHtml(String(item.revisionCount || 0))} rev</span>` : ''}
                    ${Number(item.branchCount || 0) > 1 ? `<span class="cnt-type-badge">${escapeHtml(String(item.branchCount || 0))} branches</span>` : ''}
                    ${Number(item.attachedSessions || 0) > 0 ? `<span class="cnt-type-badge">${escapeHtml(String(item.attachedSessions || 0))} mounted</span>` : ''}
                    <button class="cnt-type-badge" type="button" data-shell-object-kind="repo" data-shell-object-domain="${escapeAttr(String(item.domain || item.type || 'content'))}" data-shell-object-name="${escapeAttr(String(item.name || 'Untitled'))}" data-shell-object-branch="${escapeAttr(String(item.branch || 'main'))}" data-shell-object-item-id="${escapeAttr(String(item.itemId || ''))}">open</button>
                    ${attachedIds.has(String(item.itemId || ''))
                        ? ''
                        : `<button class="cnt-type-badge" type="button" data-worktree-attach="1" data-worktree-domain="${escapeAttr(String(item.domain || item.type || 'content'))}" data-worktree-name="${escapeAttr(String(item.name || 'Untitled'))}" data-worktree-branch="${escapeAttr(String(item.branch || 'main'))}">attach</button>`}
                </div>`).join('');
            const histHtml = histItems.map((item) => `
                <button class="cnt-hist-item ${item.current ? 'current' : ''}" type="button"${!item.current && name ? ` data-repo-revert="${escapeAttr(String(item.hash || ''))}" data-repo-domain="${escapeAttr(type || 'document')}" data-repo-name="${escapeAttr(name)}"` : (name ? ` data-repo-history="1" data-repo-domain="${escapeAttr(type || 'document')}" data-repo-name="${escapeAttr(name)}"` : ` data-scene-domain="content"`)}>
                    <div class="cnt-hist-ver">${escapeHtml(String(item.hash || 'HEAD').slice(0, 8))}</div>
                    <div class="cnt-hist-msg">${escapeHtml(String(item.message || 'Revision'))}</div>
                    <div class="cnt-hist-time">${escapeHtml(repoTime(item.createdAt || item.created_at))}</div>
                </button>`).join('');
            const branchesHtml = branchItems.map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${escapeAttr(type || 'content')}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.branch || 'main'))}${item.isDefault ? ' *' : ''}</div>
                    <div class="cnt-item-type">branch</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.hash || '').slice(0, 8) || 'HEAD')}</div>
                    <div class="cnt-item-time">${escapeHtml(repoTime(item.updated_at))}</div>
                    <button class="cnt-type-badge" type="button" data-repo-branch="1" data-repo-domain="${escapeAttr(type || 'document')}" data-repo-name="${escapeAttr(name)}" data-repo-branch="${escapeAttr(String(item.branch || 'main'))}">switch</button>
                    ${String(item.branch || 'main') !== String(info.branch || 'main')
                        ? `<button class="cnt-type-badge" type="button" data-repo-merge="${escapeAttr(String(item.branch || 'main'))}" data-repo-domain="${escapeAttr(type || 'document')}" data-repo-name="${escapeAttr(name)}" data-repo-target-branch="${escapeAttr(String(info.branch || 'main'))}">merge</button>`
                        : ''}
                </div>`).join('');
            const worktreesHtml = worktreeItems.map((item) => `
                <div class="cnt-item${String(item.itemId || '') === activeItemId || item.active ? ' current' : ''}">
                    <div class="cnt-item-icon ${escapeAttr(String(item.domain || 'content'))}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.name || 'Untitled'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.domain || 'content'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.branch || 'main'))}</div>
                    <div class="cnt-item-time">${escapeHtml(repoTime(item.updatedAt))}</div>
                    <button class="cnt-type-badge" type="button" data-shell-object-kind="worktree" data-shell-object-item-id="${escapeAttr(String(item.itemId || ''))}" data-shell-object-domain="${escapeAttr(String(item.domain || ''))}" data-shell-object-name="${escapeAttr(String(item.name || ''))}" data-shell-object-branch="${escapeAttr(String(item.branch || 'main'))}">open</button>
                    <button class="cnt-type-badge" type="button" data-worktree-detach="${escapeAttr(String(item.itemId || ''))}">detach</button>
                </div>`).join('');
            const emptyHtml = isHistory
                ? `<div class="cnt-empty">${escapeHtml(name ? `No stored revisions for ${name}.` : 'No stored revisions yet.')}</div>`
                : `<div class="cnt-empty">${escapeHtml(info.query ? `No content matched "${info.query}".` : 'No repo objects found yet.')}</div>`;
            const authorityHtml = info.authoritative === false
                ? `<div class="cnt-empty">${escapeHtml(info.error || info.fallbackReason || 'This surface is not authoritative.')}</div>`
                : '';
            const branchPanelHtml = name && type
                ? `<div class="cnt-history">${branchesHtml || '<div class="cnt-empty">No branches yet.</div>'}</div>`
                : '';
            const worktreePanelHtml = `<div class="cnt-history">${worktreesHtml || '<div class="cnt-empty">No attached worktrees in this session.</div>'}</div>`;
            return `<div class="scene scene-computer scene-content interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="content"></canvas>
                <div class="cnt-shell">
                    <div class="cnt-header">
                        <div class="cnt-action-badge">${escapeHtml(action)}</div>
                        ${type ? `<div class="cnt-type-badge">${escapeHtml(type)}</div>` : ''}
                        ${info.branch ? `<div class="cnt-type-badge">${escapeHtml(String(info.branch))}</div>` : ''}
                        ${info.mergedFromBranch ? `<div class="cnt-type-badge">from ${escapeHtml(String(info.mergedFromBranch))}</div>` : ''}
                        <div class="cnt-type-badge">${escapeHtml(String(info.source || 'live'))}</div>
                        <div class="cnt-type-badge">${info.authoritative === false ? 'non-authoritative' : 'authoritative'}</div>
                        ${name ? `<div class="cnt-name">${escapeHtml(name)}</div>` : ''}
                        ${name && type ? `<button class="cnt-type-badge" type="button" data-repo-branch="1" data-repo-domain="${escapeAttr(type)}" data-repo-name="${escapeAttr(name)}" data-repo-branch="${escapeAttr(String(info.branch || 'main'))}">branch</button>` : ''}
                        ${name && type ? `<button class="cnt-type-badge" type="button" data-repo-history="1" data-repo-domain="${escapeAttr(type)}" data-repo-name="${escapeAttr(name)}">history</button>` : ''}
                        <button class="cnt-type-badge" type="button" data-repo-worktrees="1">worktrees</button>
                        ${info.authoritative === false ? `<button class="cnt-type-badge" type="button" data-scene-domain="connectors">connections</button>` : ''}
                    </div>
                    ${authorityHtml}
                    ${runtimeHtml}
                    ${!isWorktrees && name && type ? `<div class="cnt-header"><div class="cnt-action-badge">branches</div></div>${branchPanelHtml}` : ''}
                    ${!isWorktrees ? `<div class="cnt-header"><div class="cnt-action-badge">worktrees</div></div>${worktreePanelHtml}` : ''}
                    ${isWorktrees
                        ? `<div class="cnt-list">${worktreesHtml || '<div class="cnt-empty">No attached worktrees in this session.</div>'}</div>`
                        : isHistory
                        ? `<div class="cnt-history">${histHtml || emptyHtml}</div>`
                        : `<div class="cnt-list">${listHtml || emptyHtml}</div>`}
                </div>
            </div>`;
        }
        // ── Music / Spotify scene ──────────────────────────────────────────────
        if (core.kind === 'music') {
            const info     = core.info || {};
            if (info.notConnected) {
                const label = info.fallbackReason === 'no_active_device'
                    ? 'Open Spotify on a device, then try again'
                    : 'Connect Spotify to view playback';
                return `<div class="scene scene-music interactive">
                    <canvas class="scene-canvas music-canvas" data-scene="music"></canvas>
                    <div class="music-shell">
                        <div class="connector-cta">
                            <div class="connector-cta-label">${escapeHtml(label)}</div>
                            <button class="connector-cta-btn" data-oauth-connect="${escapeAttr(String(info.service || 'spotify'))}">Connect Spotify</button>
                            <button class="connector-cta-btn" type="button" data-scene-domain="connectors">Open connections</button>
                        </div>
                    </div>
                </div>`;
            }
            const track    = String(info.track    || '').trim();
            const artist   = String(info.artist   || '').trim();
            const albumArt = String(info.album_art || '').trim();
            const playing  = info.is_playing !== false;
            const prog     = Number(info.progress_ms || 0);
            const dur      = Number(info.duration_ms  || 0);
            const pct      = dur > 0 ? Math.min(100, Math.round((prog / dur) * 100)) : (playing ? 38 : 0);
            const artBg    = albumArt ? `<div class="music-art-bg" style="background-image:url('${escapeAttr(albumArt)}')"></div>` : '';
            return `<div class="scene scene-music interactive">
                <canvas class="scene-canvas music-canvas" data-scene="music"
                        data-track="${escapeAttr(track)}" data-artist="${escapeAttr(artist)}"
                        data-playing="${playing}"></canvas>
                ${artBg}
                <div class="music-shell">
                    <div class="music-track">${escapeHtml(track || 'Nothing playing')}</div>
                    <div class="music-artist">${escapeHtml(artist || '')}</div>
                    <div class="music-controls">
                        <span class="music-btn music-prev" data-command="previous track">&#9664;&#9664;</span>
                        <span class="music-btn music-playpause" data-command="${playing ? 'pause' : 'play'}">${playing ? '&#9646;&#9646;' : '&#9654;'}</span>
                        <span class="music-btn music-next" data-command="next track">&#9654;&#9654;</span>
                    </div>
                    <div class="music-progress-bar">
                        <div class="music-progress-fill" style="width:${pct}%"></div>
                    </div>
                </div>
            </div>`;
        }
        // ── Messaging / Slack scene ────────────────────────────────────────────
        if (core.kind === 'messaging') {
            const info     = core.info || {};
            if (info.notConnected) {
                return `<div class="scene scene-messaging interactive">
                    <canvas class="scene-canvas messaging-canvas" data-scene="messaging"></canvas>
                    <div class="msg-shell">
                        <div class="connector-cta">
                            <div class="connector-cta-label">Connect Slack to view channels</div>
                            <button class="connector-cta-btn" data-oauth-connect="${escapeAttr(String(info.service || 'slack'))}">Connect Slack</button>
                            <button class="connector-cta-btn" type="button" data-scene-domain="connectors">Open connections</button>
                        </div>
                    </div>
                </div>`;
            }
            const channels = Array.isArray(info.channels) ? info.channels : [];
            const messages = Array.isArray(info.messages) ? info.messages : [];
            const unread   = Number(info.unread || channels.reduce((s, c) => s + Number(c.unread_count || 0), 0));
            const src      = String(info.source || 'scaffold');
            // Channel stream (by unread desc)
            const sorted = [...channels].sort((a, b) => Number(b.unread_count || 0) - Number(a.unread_count || 0));
            const focused = sorted[0] || messages[0] || null;
            const focusName = focused ? String(focused.name || focused.user || focused.from || '').slice(0, 32) : '';
            const streamItems = sorted.slice(1, 8).map((ch, i) => {
                const u = Number(ch.unread_count || 0);
                const opacity = Math.max(0.1, 0.5 - i * 0.07);
                return `<div class="msg-stream-item" style="opacity:${opacity}">
                    <span class="msg-stream-hash">#</span>
                    <span class="msg-stream-name">${escapeHtml(String(ch.name || '').slice(0, 28))}</span>
                    ${u > 0 ? `<span class="msg-stream-badge">${u}</span>` : ''}
                </div>`;
            }).join('');
            const focusedUnread = focused ? Number(focused.unread_count || 0) : 0;
            return `<div class="scene scene-messaging interactive">
                <canvas class="scene-canvas messaging-canvas" data-scene="messaging"></canvas>
                <div class="msg-shell">
                    <div class="msg-eyebrow">${escapeHtml(src)} · messages</div>
                    <div class="msg-hero">${unread || 0}</div>
                    <div class="msg-hero-label">unread${channels.length > 0 ? ` across ${channels.length} channels` : ''}</div>
                    ${focusName ? `<div class="msg-focus"><span class="msg-focus-hash">#</span><span class="msg-focus-name">${escapeHtml(focusName)}</span>${focusedUnread > 0 ? `<span class="msg-focus-count">${focusedUnread}</span>` : ''}</div>` : ''}
                    ${streamItems ? `<div class="msg-stream">${streamItems}</div>` : '<div class="cnt-empty">No live messages available.</div>'}
                </div>
            </div>`;
        }
        // ── Connections management panel ─────────────────────────────────────
        if (core.kind === 'network') {
            const info = core.info || {};
            const nets = Array.isArray(info.networks) ? info.networks : [];
            const msgsByTopic = (info.messagesByTopic && typeof info.messagesByTopic === 'object') ? info.messagesByTopic : {};
            const did = String(info.did || '').trim();
            const typeLabel = { personal: 'My Devices', p2p: 'Direct', private_group: 'Private Group', public_local: 'Local', public_topic: 'Topic', public_global: 'Global' };
            const netCardsHtml = nets.map(net => {
                const nType = String(net.type || '');
                const label = String(net.label || net.networkId || nType);
                const topic = String(net.topic || '');
                const msgs = Array.isArray(msgsByTopic[topic]) ? msgsByTopic[topic] : [];
                const badge = msgs.length ? `<span class="net-msg-count">${msgs.length}</span>` : '';
                const msgsHtml = msgs.slice(-5).reverse().map(m => {
                    const from = String(m.from || '').slice(0, 20);
                    const payload = (m.payload && typeof m.payload === 'object') ? m.payload : {};
                    const text = String(payload.text || payload.message || payload.content || JSON.stringify(payload)).slice(0, 120);
                    const ago = m.receivedAt ? Math.round((Date.now() - m.receivedAt) / 60000) : 0;
                    const agoStr = ago < 1 ? 'just now' : ago < 60 ? `${ago}m ago` : `${Math.round(ago / 60)}h ago`;
                    return `<div class="net-msg-row">
                        <span class="net-msg-from">${escapeHtml(from)}…</span>
                        <span class="net-msg-text">${escapeHtml(text)}</span>
                        <span class="net-msg-time">${escapeHtml(agoStr)}</span>
                    </div>`;
                }).join('');
                return `<div class="net-card net-card--${escapeAttr(nType)}">
                    <div class="net-card-head">
                        <span class="net-card-type">${escapeHtml(typeLabel[nType] || nType)}</span>
                        <span class="net-card-label">${escapeHtml(label)}</span>
                        ${badge}
                    </div>
                    <div class="net-card-body">${msgsHtml || '<span class="net-empty">No messages yet</span>'}</div>
                </div>`;
            }).join('');
            const didLine = did ? `<div class="net-did">Your DID: <span class="net-did-val">${escapeHtml(did.slice(0, 40))}…</span></div>` : '';
            const relayConnected = !!info.relayConnected;
            const relayId = String(info.relayId || '').slice(0, 12);
            const relayDot = `<span class="net-relay-dot net-relay-dot--${relayConnected ? 'on' : 'off'}" title="Relay ${relayConnected ? 'connected' : 'disconnected'}${relayId ? ' · ' + relayId : ''}"></span>`;
            const relayLine = `<div class="net-relay-status">${relayDot}<span class="net-relay-label">Relay ${relayConnected ? `connected${relayId ? ' · ' + escapeHtml(relayId) : ''}` : 'offline'}</span></div>`;
            const peerCount = typeof info.peerCount === 'number' ? info.peerCount : 0;
            const statsLine = `<div class="net-stats"><span>${nets.length} network${nets.length !== 1 ? 's' : ''}</span><span class="net-stats-sep">·</span><span>${peerCount} peer${peerCount !== 1 ? 's' : ''}</span></div>`;
            return `<div class="network-scene">
                <canvas class="scene-canvas" data-scene="network"></canvas>
                <div class="network-scene-body">
                    ${statsLine}
                    ${relayLine}
                    ${didLine}
                    <div class="net-cards">${netCardsHtml || '<div class="net-empty">Not joined to any networks yet.</div>'}</div>
                </div>
            </div>`;
        }
        if (core.kind === 'connections') {
            const info     = core.info || {};
            const services = (info.services && typeof info.services === 'object') ? info.services : {};
            const grants = Array.isArray(info.grants) ? info.grants : [];
            const grantsSummary = (info.grantsSummary && typeof info.grantsSummary === 'object') ? info.grantsSummary : {};
            const providers = (info.providers && typeof info.providers === 'object') ? info.providers : {};
            const contracts = (info.contracts && typeof info.contracts === 'object') ? info.contracts : {};
            const serviceDiagnostics = (info.serviceDiagnostics && typeof info.serviceDiagnostics === 'object') ? info.serviceDiagnostics : null;
            const focusedServiceInfo = (serviceDiagnostics?.serviceInfo && typeof serviceDiagnostics.serviceInfo === 'object') ? serviceDiagnostics.serviceInfo : {};
            const focusedSnapshot = (serviceDiagnostics?.snapshot && typeof serviceDiagnostics.snapshot === 'object') ? serviceDiagnostics.snapshot : {};
            const focusedGrants = Array.isArray(serviceDiagnostics?.relevantGrants) ? serviceDiagnostics.relevantGrants : [];
            const focusedActions = Array.isArray(serviceDiagnostics?.actions) ? serviceDiagnostics.actions : [];
            const _mkLogoUri = svg => `data:image/svg+xml,${encodeURIComponent(svg)}`;
            const _svcMeta = {
                spotify: { logo: 'https://cdn.simpleicons.org/spotify/ffffff',        label: 'Spotify',         desc: 'Music streaming' },
                gmail:   { logo: 'https://cdn.simpleicons.org/gmail/ffffff',          label: 'Gmail',           desc: 'Email' },
                gcal:    { logo: 'https://cdn.simpleicons.org/googlecalendar/ffffff', label: 'Google Calendar', desc: 'Events & scheduling' },
                gdrive:  { logo: 'https://cdn.simpleicons.org/googledrive/ffffff',    label: 'Google Drive',    desc: 'Files & documents' },
                slack:   { logo: _mkLogoUri(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="white"><path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/></svg>`), label: 'Slack', desc: 'Team messaging' },
                plaid:   { logo: _mkLogoUri(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="white"><rect x="1" y="1" width="6" height="6" rx="1"/><rect x="9" y="1" width="6" height="6" rx="1"/><rect x="17" y="1" width="6" height="6" rx="1"/><rect x="1" y="9" width="6" height="6" rx="1"/><rect x="9" y="9" width="6" height="6" rx="1"/><rect x="17" y="9" width="6" height="6" rx="1"/><rect x="1" y="17" width="6" height="6" rx="1"/><rect x="9" y="17" width="6" height="6" rx="1"/><rect x="17" y="17" width="6" height="6" rx="1"/></svg>`), label: 'Plaid', desc: 'Bank accounts' },
            };
            const token = sessionStorage.getItem('genome_session') || '';
            const cardsHtml = Object.entries(_svcMeta).map(([svcId, meta]) => {
                const svc = services[svcId] || {};
                const connected = Boolean(svc.connected);
                const configured = svc.configured !== false;
                const mode = String(svc.mode || 'scaffold');
                const statusCode = String(svc.status || (connected ? 'connected' : (configured ? 'ready_to_connect' : 'credentials_required')));
                const statusLabel = connected ? `connected · ${mode}` : statusCode.replace(/_/g, ' ');
                const detail = String(svc.detail || '').trim();
                const fallbackReason = String(svc.fallbackReason || '').trim();
                const lastError = String(svc.lastError || '').trim();
                const authoritative = svc.authoritative !== false;
                const sampleCount = Number(svc.sampleCount || 0);
                const requiredScopes = Array.isArray(svc.requiredScopes) ? svc.requiredScopes.filter(Boolean) : [];
                const riskLevels = Array.isArray(svc.riskLevels) ? svc.riskLevels.filter(Boolean) : [];
                const domainLabel = String(svc.domain || '').trim();
                const providerId = String(svc.providerId || '').trim();
                const surfaceByService = { gmail: 'email', gcal: 'calendar', gdrive: 'drive', slack: 'messaging', spotify: 'music', plaid: 'connections' };
                const liveSurface = surfaceByService[svcId] || 'connections';
                const openButton = connected
                    ? `<button class="conn-card-btn" type="button" data-live-surface="${escapeAttr(liveSurface)}">open</button>`
                    : '';
                const inspectButton = `<button class="conn-card-btn" type="button" data-connector-inspect="${escapeAttr(svcId)}">inspect</button>`;
                const cardFooter = connected
                    ? `${inspectButton}${openButton}<button class="conn-card-btn" type="button" data-oauth-disconnect="${escapeAttr(svcId)}" data-auth-token="${escapeAttr(token)}">disconnect</button>`
                    : `${inspectButton}<button class="conn-card-btn" type="button" data-oauth-connect="${escapeAttr(svcId)}" data-auth-token="${escapeAttr(token)}"${!configured ? ' data-oauth-needs-creds="1"' : ''}>connect</button>`;
                return `<div class="conn-card ${connected ? 'conn-card--live' : ''}" data-card-svc="${escapeAttr(svcId)}">
                    <img class="conn-card-logo" src="${meta.logo}" alt="${escapeHtml(meta.label)}" width="36" height="36">
                    <div class="conn-card-name">${escapeHtml(meta.label)}</div>
                    <div class="conn-card-desc">${escapeHtml(meta.desc)}</div>
                    <div class="conn-card-status">${escapeHtml(authoritative ? statusLabel : `${statusLabel} · degraded`)}</div>
                    ${(domainLabel || providerId) ? `<div class="conn-card-desc">${escapeHtml([domainLabel, providerId].filter(Boolean).join(' · '))}</div>` : ''}
                    ${requiredScopes.length ? `<div class="conn-card-desc">${escapeHtml(String(requiredScopes.length))} scopes${riskLevels.length ? ` · ${riskLevels.join('/')}` : ''}</div>` : ''}
                    ${detail ? `<div class="conn-card-desc">${escapeHtml(detail)}</div>` : ''}
                    ${sampleCount > 0 ? `<div class="conn-card-desc">${escapeHtml(String(sampleCount))} live items detected</div>` : ''}
                    ${fallbackReason ? `<div class="conn-card-desc">${escapeHtml(fallbackReason.replace(/_/g, ' '))}</div>` : ''}
                    ${lastError ? `<div class="conn-card-desc">${escapeHtml(lastError)}</div>` : ''}
                    ${cardFooter}
                </div>`;
            }).join('');
            const grantsHtml = grants.slice(0, 10).map((item) => {
                const scope = String(item.scope || '').trim();
                const granted = Boolean(item.granted);
                const expiresAt = Number(item.expiresAt || 0);
                const expiry = expiresAt ? formatDate(expiresAt) : '';
                const domains = Array.isArray(item.domains) ? item.domains.filter(Boolean) : [];
                const providers = Array.isArray(item.providers) ? item.providers.filter(Boolean) : [];
                const capabilities = Array.isArray(item.capabilities) ? item.capabilities.filter(Boolean) : [];
                const risk = String(item.risk || '').trim();
                const support = String(item.support || '').trim();
                const capabilityLabel = capabilities.length
                    ? `${capabilities.length} capability${capabilities.length === 1 ? '' : 'ies'}`
                    : '';
                const metaLine = [domains.join(', '), providers.join(', '), risk ? `risk ${risk}` : '', support].filter(Boolean).join(' · ');
                return `<div class="cnt-item">
                    <div class="cnt-item-icon ${granted ? 'document' : 'generic'}"></div>
                    <div class="cnt-item-name">${escapeHtml(scope || 'scope')}</div>
                    <div class="cnt-item-type">${granted ? 'granted' : 'blocked'}</div>
                    <div class="cnt-item-ver">${escapeHtml(metaLine || expiry || 'persistent')}</div>
                    <div class="cnt-item-time"></div>
                    ${granted
                        ? `<button class="cnt-type-badge" type="button" data-connector-revoke="${escapeAttr(scope)}">revoke</button>`
                        : `<button class="cnt-type-badge" type="button" data-connector-grant="${escapeAttr(scope)}">grant</button>`}
                </div>`;
            }).join('');
            const targetSvc = String(info.targetService || '').trim();
            const autoConnect = targetSvc && _svcMeta[targetSvc] && !(services[targetSvc]?.connected) ? targetSvc : '';
            const focusedSampleRows = (Array.isArray(focusedSnapshot.sampleItems) ? focusedSnapshot.sampleItems : []).map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon document"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.label || 'item'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.type || 'sample'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.detail || '').slice(0, 96))}</div>
                    <div class="cnt-item-time">${escapeHtml(String(item.time || ''))}</div>
                </div>`).join('');
            const focusedGrantRows = focusedGrants.slice(0, 8).map((item) => {
                const scope = String(item.scope || '').trim();
                const granted = Boolean(item.granted);
                const risk = String(item.risk || '').trim();
                const support = String(item.support || '').trim();
                const metaLine = [risk ? `risk ${risk}` : '', support].filter(Boolean).join(' Â· ');
                return `<div class="cnt-item">
                    <div class="cnt-item-icon ${granted ? 'document' : 'generic'}"></div>
                    <div class="cnt-item-name">${escapeHtml(scope || 'scope')}</div>
                    <div class="cnt-item-type">${granted ? 'granted' : 'blocked'}</div>
                    <div class="cnt-item-ver">${escapeHtml(metaLine || 'connector scope')}</div>
                    <div class="cnt-item-time"></div>
                </div>`;
            }).join('');
            const focusedActionButtons = focusedActions.map((item) => {
                const id = String(item.id || '').trim();
                const label = String(item.label || id || 'action').trim();
                const surface = String(item.surface || '').trim();
                if (id === 'open' && surface) return `<button class="cnt-type-badge" type="button" data-shell-object-kind="service" data-shell-object-service="${escapeAttr(String(serviceDiagnostics?.service || ''))}">${escapeHtml(label)}</button>`;
                if (id === 'disconnect') return `<button class="cnt-type-badge" type="button" data-oauth-disconnect="${escapeAttr(String(serviceDiagnostics?.service || ''))}">${escapeHtml(label)}</button>`;
                if (id === 'connect') return `<button class="cnt-type-badge" type="button" data-oauth-connect="${escapeAttr(String(serviceDiagnostics?.service || ''))}"${focusedServiceInfo.configured === false ? ' data-oauth-needs-creds="1"' : ''}>${escapeHtml(label)}</button>`;
                if (surface) return `<button class="cnt-type-badge" type="button" data-shell-object-kind="scene" data-shell-object-scene="${escapeAttr(surface)}">${escapeHtml(label)}</button>`;
                return '';
            }).filter(Boolean).join('');
            const providerRows = Object.entries(providers).slice(0, 12).map(([key, item]) => {
                const provider = (item && typeof item === 'object') ? item : {};
                const mode = String(provider.mode || 'unknown').trim();
                const execution = String(provider.execution || '').trim();
                const configured = provider.configured === false ? 'unconfigured' : (provider.configured === true ? 'configured' : '');
                const meta = [execution, configured].filter(Boolean).join(' · ');
                return `<div class="cnt-item">
                    <div class="cnt-item-icon ${mode === 'live' ? 'document' : 'generic'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(key || 'provider'))}</div>
                    <div class="cnt-item-type">${escapeHtml(mode)}</div>
                    <div class="cnt-item-ver">${escapeHtml(meta || 'provider policy')}</div>
                    <div class="cnt-item-time"></div>
                </div>`;
            }).join('');
            const contractRows = Object.entries(contracts).slice(0, 16).map(([key, item]) => {
                const contract = (item && typeof item === 'object') ? item : {};
                const desktop = String(contract.desktop || '').trim();
                const mobile = String(contract.mobile || '').trim();
                const fallback = String(contract.fallback || '').trim();
                const meta = [desktop ? `desktop ${desktop}` : '', mobile ? `mobile ${mobile}` : '', fallback ? `fallback ${fallback}` : '']
                    .filter(Boolean)
                    .join(' · ');
                return `<div class="cnt-item">
                    <div class="cnt-item-icon document"></div>
                    <div class="cnt-item-name">${escapeHtml(String(key || 'contract'))}</div>
                    <div class="cnt-item-type">contract</div>
                    <div class="cnt-item-ver">${escapeHtml(meta || 'adapter contract')}</div>
                    <div class="cnt-item-time"></div>
                </div>`;
            }).join('');
            return `<div class="scene scene-connections interactive"${autoConnect ? ` data-auto-connect="${escapeAttr(autoConnect)}"` : ''}>
                <canvas class="scene-canvas domain-canvas" data-scene="connections"></canvas>
                <div class="connections-shell">
                    <div class="cnt-header">
                        <div class="cnt-action-badge">connector state</div>
                        <div class="cnt-type-badge">${escapeHtml(String(grantsSummary.granted || 0))}/${escapeHtml(String(grantsSummary.count || grants.length || 0))} grants</div>
                        ${targetSvc ? `<div class="cnt-type-badge">${escapeHtml(targetSvc)}</div>` : ''}
                    </div>
                    <div class="connections-grid">${cardsHtml}</div>
                    ${serviceDiagnostics ? `
                    <div class="cnt-header">
                        <div class="cnt-action-badge">service detail</div>
                        <div class="cnt-type-badge">${escapeHtml(String(focusedServiceInfo.label || serviceDiagnostics.service || 'connector'))}</div>
                        <div class="cnt-type-badge">${focusedServiceInfo.authoritative === false ? 'degraded' : 'authoritative'}</div>
                        <button class="cnt-type-badge" type="button" data-shell-object-kind="scene" data-shell-object-scene="connectors">all services</button>
                        ${focusedActionButtons}
                    </div>
                    <div class="cnt-history">
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${focusedServiceInfo.connected ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">status</div>
                            <div class="cnt-item-type">${escapeHtml(String(focusedServiceInfo.status || 'unknown').replace(/_/g, ' '))}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(focusedServiceInfo.detail || focusedSnapshot.error || ''))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${Number(focusedServiceInfo.sampleCount || 0) > 0 ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">live samples</div>
                            <div class="cnt-item-type">${escapeHtml(String(focusedSnapshot.source || focusedServiceInfo.mode || 'live'))}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(focusedServiceInfo.sampleCount || 0))} items</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">provider</div>
                            <div class="cnt-item-type">${escapeHtml(String(focusedServiceInfo.providerId || 'connector'))}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(focusedServiceInfo.domain || ''))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${focusedServiceInfo.lastError ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">fallback</div>
                            <div class="cnt-item-type">${escapeHtml(String(focusedServiceInfo.fallbackReason || 'none').replace(/_/g, ' '))}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(focusedServiceInfo.lastError || 'steady'))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                    </div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">sample items</div>
                    </div>
                    <div class="cnt-history">${focusedSampleRows || '<div class="cnt-empty">No live sample items available.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">relevant grants</div>
                    </div>
                    <div class="cnt-history">${focusedGrantRows || '<div class="cnt-empty">No relevant grants found for this service.</div>'}</div>` : ''}
                    <div class="cnt-header">
                        <div class="cnt-action-badge">provider modes</div>
                    </div>
                    <div class="cnt-history">${providerRows || '<div class="cnt-empty">No provider modes available.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">adapter contracts</div>
                    </div>
                    <div class="cnt-history">${contractRows || '<div class="cnt-empty">No connector contracts available.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">capability grants</div>
                    </div>
                    <div class="cnt-history">${grantsHtml || '<div class="cnt-empty">No connector grant catalog available.</div>'}</div>
                </div>
            </div>`;
        }
        if (core.kind === 'runtime') {
            const info = core.info || {};
            const health = (info.health && typeof info.health === 'object') ? info.health : {};
            const selfCheck = (info.selfCheck && typeof info.selfCheck === 'object') ? info.selfCheck : {};
            const profile = (info.profile && typeof info.profile === 'object') ? info.profile : {};
            const services = (info.services && typeof info.services === 'object') ? info.services : {};
            const diagnostics = (info.diagnostics && typeof info.diagnostics === 'object') ? info.diagnostics : {};
            const perf = (health.performance && typeof health.performance === 'object') ? health.performance : {};
            const jobs = (health.jobs && typeof health.jobs === 'object') ? health.jobs : {};
            const deadLetters = (health.deadLetters && typeof health.deadLetters === 'object') ? health.deadLetters : {};
            const runtimeWorkspace = (health.workspace && typeof health.workspace === 'object') ? health.workspace : {};
            const runtimeActive = (runtimeWorkspace.activeContent && typeof runtimeWorkspace.activeContent === 'object')
                ? runtimeWorkspace.activeContent
                : {};
            const slo = (health.slo && typeof health.slo === 'object') ? health.slo : {};
            const sloAlerts = Array.isArray(slo.alerts) ? slo.alerts : [];
            const latency = (profile.latencyMs && typeof profile.latencyMs === 'object') ? profile.latencyMs : {};
            const outcomes = (profile.outcomes && typeof profile.outcomes === 'object') ? profile.outcomes : {};
            const continuityNext = (diagnostics.continuityNext && typeof diagnostics.continuityNext === 'object') ? diagnostics.continuityNext : {};
            const autopilotSummary = (diagnostics.continuityAutopilot && typeof diagnostics.continuityAutopilot === 'object') ? diagnostics.continuityAutopilot : {};
            const autopilotPreview = (diagnostics.continuityAutopilotPreview && typeof diagnostics.continuityAutopilotPreview === 'object') ? diagnostics.continuityAutopilotPreview : {};
            const autopilotGuardrails = (diagnostics.continuityAutopilotGuardrails && typeof diagnostics.continuityAutopilotGuardrails === 'object') ? diagnostics.continuityAutopilotGuardrails : {};
            const autopilotMode = (diagnostics.continuityAutopilotMode && typeof diagnostics.continuityAutopilotMode === 'object') ? diagnostics.continuityAutopilotMode : {};
            const autopilotDrift = (diagnostics.continuityAutopilotDrift && typeof diagnostics.continuityAutopilotDrift === 'object') ? diagnostics.continuityAutopilotDrift : {};
            const autopilotAlignment = (diagnostics.continuityAutopilotAlignment && typeof diagnostics.continuityAutopilotAlignment === 'object') ? diagnostics.continuityAutopilotAlignment : {};
            const postureActions = (diagnostics.continuityAutopilotPostureActions && typeof diagnostics.continuityAutopilotPostureActions === 'object') ? diagnostics.continuityAutopilotPostureActions : {};
            const postureActionMetrics = (diagnostics.continuityAutopilotPostureActionMetrics && typeof diagnostics.continuityAutopilotPostureActionMetrics === 'object') ? diagnostics.continuityAutopilotPostureActionMetrics : {};
            const checks = Array.isArray(selfCheck.checks) ? selfCheck.checks : [];
            const previewInfo = (autopilotPreview.preview && typeof autopilotPreview.preview === 'object') ? autopilotPreview.preview : {};
            const driftSummary = (autopilotDrift.summary && typeof autopilotDrift.summary === 'object') ? autopilotDrift.summary : {};
            const alignmentSummary = (autopilotAlignment.summary && typeof autopilotAlignment.summary === 'object') ? autopilotAlignment.summary : {};
            const checksHtml = checks.map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${item.ok ? 'document' : 'generic'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.name || 'check'))}</div>
                    <div class="cnt-item-type">${item.ok ? 'ok' : 'degraded'}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.detail || ''))}</div>
                    <div class="cnt-item-time"></div>
                </div>`).join('');
            const serviceRows = Object.entries(services).map(([key, item]) => {
                const svc = (item && typeof item === 'object') ? item : {};
                return `<div class="cnt-item">
                    <div class="cnt-item-icon ${svc.running ? 'document' : 'generic'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(svc.label || key))}</div>
                    <div class="cnt-item-type">${svc.running ? 'running' : 'down'}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(svc.detail || ''))}</div>
                    <div class="cnt-item-time"></div>
                </div>`;
            }).join('');
            const postureRows = (Array.isArray(postureActions.items) ? postureActions.items : []).map((item, index) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${String(item.priority || 'p2').toLowerCase() === 'p0' ? 'generic' : 'document'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.title || 'action'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.priority || 'p2'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.reason || item.command || '').slice(0, 72))}</div>
                    <div class="cnt-item-time"></div>
                    <button class="cnt-type-badge" type="button" data-continuity-posture-apply="${index + 1}">apply</button>
                </div>`).join('');
            return `<div class="scene scene-connections interactive">
                <canvas class="scene-canvas domain-canvas" data-scene="connections"></canvas>
                <div class="connections-shell">
                    <div class="cnt-header">
                        <div class="cnt-action-badge">runtime health</div>
                        <div class="cnt-type-badge">${escapeHtml(String(perf.totalMs || latency.avg || 0))}ms</div>
                        <div class="cnt-type-badge">${Boolean(selfCheck.overallOk) ? 'healthy' : 'degraded'}</div>
                        <button class="cnt-type-badge" type="button" data-scene-domain="content">repo</button>
                        <button class="cnt-type-badge" type="button" data-repo-worktrees="1">worktrees</button>
                        <button class="cnt-type-badge" type="button" data-runtime-refresh="1">refresh</button>
                    </div>
                    <div class="cnt-history">
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">latency</div>
                            <div class="cnt-item-type">p50/p95</div>
                            <div class="cnt-item-ver">${escapeHtml(String(latency.p50 || 0))}/${escapeHtml(String(latency.p95 || 0))}ms</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">budget</div>
                            <div class="cnt-item-type">${perf.withinBudget === false ? 'over' : 'within'}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(perf.budgetMs || 0))}ms</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">jobs</div>
                            <div class="cnt-item-type">active/total</div>
                            <div class="cnt-item-ver">${escapeHtml(String(jobs.active || 0))}/${escapeHtml(String(jobs.total || 0))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">dead letters</div>
                            <div class="cnt-item-type">backlog</div>
                            <div class="cnt-item-ver">${escapeHtml(String(deadLetters.count || 0))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">within budget</div>
                            <div class="cnt-item-type">sample</div>
                            <div class="cnt-item-ver">${escapeHtml(String(outcomes.withinBudgetPct || 0))}%</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">worktrees</div>
                            <div class="cnt-item-type">mounted</div>
                            <div class="cnt-item-ver">${escapeHtml(String(runtimeWorkspace.worktreeCount || 0))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${runtimeActive.name ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">active content</div>
                            <div class="cnt-item-type">${escapeHtml(String(runtimeActive.branch || runtimeWorkspace.branch || 'main'))}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(runtimeActive.name || 'none'))}</div>
                            <div class="cnt-item-time"></div>
                            ${runtimeActive.name && runtimeActive.domain
                                ? `<button class="cnt-type-badge" type="button" data-shell-object-kind="repo" data-shell-object-domain="${escapeAttr(String(runtimeActive.domain || 'document'))}" data-shell-object-name="${escapeAttr(String(runtimeActive.name || ''))}" data-shell-object-branch="${escapeAttr(String(runtimeActive.branch || runtimeWorkspace.branch || 'main'))}" data-shell-object-item-id="${escapeAttr(String(runtimeActive.itemId || ''))}">open</button>`
                                : ''}
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${sloAlerts.length ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">slo alerts</div>
                            <div class="cnt-item-type">${Number(slo.breachStreak || 0) > 0 ? 'watch' : 'stable'}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(sloAlerts.length || 0))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                    </div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">self-check</div>
                    </div>
                    <div class="cnt-history">${checksHtml || '<div class="cnt-empty">No runtime checks available.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">services</div>
                    </div>
                    <div class="cnt-history">${serviceRows || '<div class="cnt-empty">No runtime services available.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">control plane</div>
                        <button class="cnt-type-badge" type="button" data-continuity-autopilot-mode="recommended">apply mode</button>
                        <button class="cnt-type-badge" type="button" data-continuity-posture-batch="3">apply batch</button>
                    </div>
                    <div class="cnt-history">
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${previewInfo.appliable === false ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">preview</div>
                            <div class="cnt-item-type">${escapeHtml(String(previewInfo.reason || autopilotSummary.previewReason || 'unknown'))}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(previewInfo.command || previewInfo.title || 'next step').slice(0, 72))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${Number(autopilotGuardrails.blockerCount || 0) > 0 ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">guardrails</div>
                            <div class="cnt-item-type">${Number(autopilotGuardrails.blockerCount || 0) > 0 ? 'blocked' : 'clear'}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(autopilotGuardrails.blockerCount || 0))} blockers</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${driftSummary.drifted ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">mode drift</div>
                            <div class="cnt-item-type">${driftSummary.drifted ? 'drifted' : 'aligned'}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(autopilotMode.recommendedMode || autopilotSummary.recommendedMode || 'normal'))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${Number(alignmentSummary.aligned || 0) > 0 ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">alignment</div>
                            <div class="cnt-item-type">recent</div>
                            <div class="cnt-item-ver">${escapeHtml(String(alignmentSummary.aligned || 0))}/${escapeHtml(String(alignmentSummary.count || 0))} aligned</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${Number(postureActionMetrics.summary?.applied || 0) > 0 ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">posture actions</div>
                            <div class="cnt-item-type">1h</div>
                            <div class="cnt-item-ver">${escapeHtml(String(postureActionMetrics.summary?.applied || 0))}/${escapeHtml(String(postureActionMetrics.summary?.count || 0))} applied</div>
                            <div class="cnt-item-time"></div>
                        </div>
                    </div>
                    <div class="cnt-history">${postureRows || '<div class="cnt-empty">No posture actions available.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">next actions</div>
                        <button class="cnt-type-badge" type="button" data-shell-object-kind="scene" data-shell-object-scene="continuity">continuity</button>
                        <button class="cnt-type-badge" type="button" data-shell-object-kind="scene" data-shell-object-scene="connectors">connections</button>
                        <button class="cnt-type-badge" type="button" data-shell-object-kind="scene" data-shell-object-scene="notifications">alerts</button>
                    </div>
                    <div class="cnt-history">
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${Number(continuityNext.count || 0) > 0 ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">continuity next</div>
                            <div class="cnt-item-type">${escapeHtml(String(continuityNext.topPriority || 'none'))}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(continuityNext.count || 0))} actions</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${autopilotSummary.enabled ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">autopilot</div>
                            <div class="cnt-item-type">${autopilotSummary.enabled ? 'enabled' : 'idle'}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(autopilotSummary.recommendedMode || autopilotSummary.lastResult || 'normal'))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'handoff') {
            const info = core.info || {};
            const continuity = (info.continuity && typeof info.continuity === 'object') ? info.continuity : {};
            const alerts = (info.alerts && typeof info.alerts === 'object') ? info.alerts : {};
            const handoff = (info.handoff && typeof info.handoff === 'object') ? info.handoff : {};
            const presence = (info.presence && typeof info.presence === 'object') ? info.presence : {};
            const incidents = (info.incidents && typeof info.incidents === 'object') ? info.incidents : {};
            const nextActions = (info.nextActions && typeof info.nextActions === 'object') ? info.nextActions : {};
            const diagnostics = (info.diagnostics && typeof info.diagnostics === 'object') ? info.diagnostics : {};
            const autopilot = (continuity.continuityAutopilot && typeof continuity.continuityAutopilot === 'object')
                ? continuity.continuityAutopilot
                : (this.state.session.continuityAutopilot || {});
            const health = (continuity.health && typeof continuity.health === 'object') ? continuity.health : {};
            const handoffStats = (handoff.stats && typeof handoff.stats === 'object') ? handoff.stats : {};
            const latency = (handoffStats.latencyMs && typeof handoffStats.latencyMs === 'object') ? handoffStats.latencyMs : {};
            const continuityWorkspace = (continuity.workspace && typeof continuity.workspace === 'object') ? continuity.workspace : {};
            const continuityActive = (continuityWorkspace.activeContent && typeof continuityWorkspace.activeContent === 'object')
                ? continuityWorkspace.activeContent
                : {};
            const continuityNotifications = (continuity.notifications && typeof continuity.notifications === 'object')
                ? continuity.notifications
                : {};
            const autopilotSummary = (diagnostics.continuityAutopilot && typeof diagnostics.continuityAutopilot === 'object') ? diagnostics.continuityAutopilot : {};
            const continuityHistory = (info.continuityHistory && typeof info.continuityHistory === 'object') ? info.continuityHistory : {};
            const continuityAnomalies = (info.continuityAnomalies && typeof info.continuityAnomalies === 'object') ? info.continuityAnomalies : {};
            const autopilotHistory = (info.autopilotHistory && typeof info.autopilotHistory === 'object') ? info.autopilotHistory : {};
            const autopilotPreview = (info.autopilotPreview && typeof info.autopilotPreview === 'object') ? info.autopilotPreview : {};
            const autopilotMetrics = (info.autopilotMetrics && typeof info.autopilotMetrics === 'object') ? info.autopilotMetrics : {};
            const autopilotGuardrails = (info.autopilotGuardrails && typeof info.autopilotGuardrails === 'object') ? info.autopilotGuardrails : {};
            const autopilotMode = (info.autopilotMode && typeof info.autopilotMode === 'object') ? info.autopilotMode : {};
            const autopilotDrift = (info.autopilotDrift && typeof info.autopilotDrift === 'object') ? info.autopilotDrift : {};
            const autopilotAlignment = (info.autopilotAlignment && typeof info.autopilotAlignment === 'object') ? info.autopilotAlignment : {};
            const autopilotPolicyMatrix = (info.autopilotPolicyMatrix && typeof info.autopilotPolicyMatrix === 'object') ? info.autopilotPolicyMatrix : {};
            const postureHistory = (info.postureHistory && typeof info.postureHistory === 'object') ? info.postureHistory : {};
            const postureAnomalies = (info.postureAnomalies && typeof info.postureAnomalies === 'object') ? info.postureAnomalies : {};
            const postureActions = (info.postureActions && typeof info.postureActions === 'object') ? info.postureActions : {};
            const postureActionMetrics = (info.postureActionMetrics && typeof info.postureActionMetrics === 'object') ? info.postureActionMetrics : {};
            const posturePolicyMatrix = (info.posturePolicyMatrix && typeof info.posturePolicyMatrix === 'object') ? info.posturePolicyMatrix : {};
            const pairedSurfaces = (info.pairedSurfaces && typeof info.pairedSurfaces === 'object') ? info.pairedSurfaces : {};
            const pendingHandoff = (handoff.pending && typeof handoff.pending === 'object') ? handoff.pending : {};
            const previewInfo = (autopilotPreview.preview && typeof autopilotPreview.preview === 'object') ? autopilotPreview.preview : {};
            const autopilotMetricsSummary = (autopilotMetrics.metrics && typeof autopilotMetrics.metrics === 'object') ? autopilotMetrics.metrics : {};
            const guardrailState = (autopilotGuardrails.guardrails && typeof autopilotGuardrails.guardrails === 'object') ? autopilotGuardrails.guardrails : {};
            const driftInfo = (autopilotDrift.summary && typeof autopilotDrift.summary === 'object') ? autopilotDrift.summary : {};
            const alignmentSummary = (autopilotAlignment.summary && typeof autopilotAlignment.summary === 'object') ? autopilotAlignment.summary : {};
            const pendingToken = String(pendingHandoff.token || '').trim();
            const pendingBackendUrl = String(pendingHandoff.backendUrl || '').trim();
            const pendingBridgeUrl = String(pendingHandoff.bridgeUrl || '').trim();
            const pendingExpires = Number(pendingHandoff.expiresAt || 0);
            const pendingPair = (pendingHandoff.pairedSurface && typeof pendingHandoff.pairedSurface === 'object') ? pendingHandoff.pairedSurface : {};
            const pendingShareUrl = this.buildHandoffShareUrl(pendingToken, pendingBackendUrl, pendingBridgeUrl);
            const alertItems = Array.isArray(handoffStats.alerts) ? handoffStats.alerts : (Array.isArray(alerts.items) ? alerts.items : []);
            const devices = Array.isArray(presence.items) ? presence.items : [];
            const pairedItems = Array.isArray(pairedSurfaces.items) ? pairedSurfaces.items : [];
            const deviceRows = devices.map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${item.active ? 'document' : 'generic'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.label || item.deviceId || 'device'))}</div>
                    <div class="cnt-item-type">${item.active ? 'active' : 'stale'}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.platform || 'unknown'))}</div>
                    <div class="cnt-item-time">${escapeHtml(item.lastSeenAt ? formatDate(item.lastSeenAt) : '')}</div>
                </div>`).join('');
            const pairedRows = pairedItems.map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${item.preferred ? 'document' : 'generic'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.label || item.surfaceId || 'surface'))}</div>
                    <div class="cnt-item-type">${item.preferred ? 'preferred' : escapeHtml(String(item.role || 'surface'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.platform || 'unknown'))}</div>
                    <div class="cnt-item-time">${escapeHtml(item.updatedAt ? formatDate(item.updatedAt) : '')}</div>
                    <button class="cnt-type-badge" type="button" data-handoff-start="1" data-handoff-surface="${escapeAttr(String(item.surfaceId || ''))}">take over</button>
                    ${item.preferred ? '' : `<button class="cnt-type-badge" type="button" data-surface-prefer="${escapeAttr(String(item.surfaceId || ''))}">prefer</button>`}
                </div>`).join('');
            const alertRows = alertItems.map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon generic"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.message || item.reason || 'alert'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.status || health.status || 'attention'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.deviceId || handoff.activeDeviceId || 'session'))}</div>
                    <div class="cnt-item-time">${escapeHtml(item.createdAt ? formatDate(item.createdAt) : '')}</div>
                </div>`).join('');
            const incidentItems = Array.isArray(incidents.items) ? incidents.items : [];
            const incidentRows = incidentItems.map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon generic"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.type || item.category || 'incident'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.severity || 'low'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.detail || '').slice(0, 72))}</div>
                    <div class="cnt-item-time">${escapeHtml(item.ts ? formatDate(item.ts) : '')}</div>
                </div>`).join('');
            const nextItems = Array.isArray(nextActions.items) ? nextActions.items : [];
            const nextRows = nextItems.map((item) => `
                <button class="cnt-item" type="button"${String(item.command || '').trim() ? ` data-command="${escapeAttr(String(item.command || '').trim())}"` : ''}>
                    <div class="cnt-item-icon ${String(item.priority || 'p2').toLowerCase() === 'p0' ? 'generic' : 'document'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.title || 'action'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.priority || 'p2'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.reason || item.command || '').slice(0, 72))}</div>
                    <div class="cnt-item-time"></div>
                </button>`).join('');
            const continuityHistoryRows = (Array.isArray(continuityHistory.items) ? continuityHistory.items : []).map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${String(item.status || 'healthy') === 'critical' ? 'generic' : 'document'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.source || 'snapshot'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.status || 'unknown'))}</div>
                    <div class="cnt-item-ver">score ${escapeHtml(String(item.score || 0))} · breaches ${escapeHtml(String(item.handoffBreaches || 0))}</div>
                    <div class="cnt-item-time">${escapeHtml(item.ts ? formatDate(item.ts) : '')}</div>
                </div>`).join('');
            const continuityAnomalyRows = (Array.isArray(continuityAnomalies.items) ? continuityAnomalies.items : []).map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon generic"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.type || 'anomaly'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.severity || 'watch'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.detail || '').slice(0, 72))}</div>
                    <div class="cnt-item-time">${escapeHtml(item.ts ? formatDate(item.ts) : '')}</div>
                </div>`).join('');
            const autopilotHistoryRows = (Array.isArray(autopilotHistory.items) ? autopilotHistory.items : []).map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${item.changed ? 'document' : 'generic'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.reason || item.source || 'autopilot'))}</div>
                    <div class="cnt-item-type">${item.changed ? 'changed' : 'observed'}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.command || item.message || '').slice(0, 72))}</div>
                    <div class="cnt-item-time">${escapeHtml(item.ts ? formatDate(item.ts) : '')}</div>
                </div>`).join('');
            const postureActionRows = (Array.isArray(postureActions.items) ? postureActions.items : []).map((item, index) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${String(item.priority || 'p2').toLowerCase() === 'p0' ? 'generic' : 'document'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.title || 'action'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.priority || 'p2'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.reason || item.command || '').slice(0, 72))}</div>
                    <div class="cnt-item-time"></div>
                    <button class="cnt-type-badge" type="button" data-continuity-posture-apply="${index + 1}">apply</button>
                </div>`).join('');
            const postureHistoryRows = (Array.isArray(postureHistory.items) ? postureHistory.items : []).map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${item.modeDrifted ? 'generic' : 'document'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.mode || 'normal'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.recommendedMode || 'normal'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.previewReason || 'unknown'))}</div>
                    <div class="cnt-item-time">${escapeHtml(item.ts ? formatDate(item.ts) : '')}</div>
                </div>`).join('');
            const postureAnomalyRows = (Array.isArray(postureAnomalies.items) ? postureAnomalies.items : []).map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon generic"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.type || 'anomaly'))}</div>
                    <div class="cnt-item-type">${escapeHtml(String(item.mode || 'normal'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.detail || item.reason || '').slice(0, 72))}</div>
                    <div class="cnt-item-time">${escapeHtml(item.ts ? formatDate(item.ts) : '')}</div>
                </div>`).join('');
            const modeMatrixRows = (Array.isArray(autopilotPolicyMatrix.items) ? autopilotPolicyMatrix.items : []).map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${item.allowed ? 'document' : 'generic'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.targetMode || 'normal'))}</div>
                    <div class="cnt-item-type">${item.allowed ? 'allowed' : escapeHtml(String(item.code || 'blocked'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.reason || '').slice(0, 72))}</div>
                    <div class="cnt-item-time"></div>
                    ${item.allowed ? `<button class="cnt-type-badge" type="button" data-continuity-autopilot-mode="${escapeAttr(String(item.targetMode || 'normal'))}">set</button>` : ''}
                </div>`).join('');
            const posturePolicyRows = (Array.isArray(posturePolicyMatrix.items) ? posturePolicyMatrix.items : []).map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${item.appliable ? 'document' : 'generic'}"></div>
                    <div class="cnt-item-name">${escapeHtml(String(item.title || 'action'))}</div>
                    <div class="cnt-item-type">${item.appliable ? 'ready' : escapeHtml(String(item.policyCode || item.reason || 'blocked'))}</div>
                    <div class="cnt-item-ver">${escapeHtml(String(item.command || '').slice(0, 72))}</div>
                    <div class="cnt-item-time"></div>
                </div>`).join('');
            return `<div class="scene scene-connections interactive">
                <canvas class="scene-canvas domain-canvas" data-scene="connections"></canvas>
                <div class="connections-shell">
                    <div class="cnt-header">
                        <div class="cnt-action-badge">continuity</div>
                        <div class="cnt-type-badge">${escapeHtml(String(health.status || 'healthy'))}</div>
                        <div class="cnt-type-badge">${escapeHtml(String(presence.activeCount || 0))}/${escapeHtml(String(presence.count || 0))} active</div>
                        ${handoff.activeDeviceId ? `<div class="cnt-type-badge">${escapeHtml(String(handoff.activeDeviceId || ''))}</div>` : ''}
                        <div class="cnt-type-badge">${autopilot.enabled ? 'autopilot on' : 'autopilot off'}</div>
                        <button class="cnt-type-badge" type="button" data-continuity-drill="1">drill</button>
                        <button class="cnt-type-badge" type="button" data-continuity-clear="1">clear alerts</button>
                    </div>
                    <div class="cnt-history">
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">handoff success</div>
                            <div class="cnt-item-type">rate</div>
                            <div class="cnt-item-ver">${escapeHtml(String(handoffStats.successRatePct || 0))}%</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">handoff latency</div>
                            <div class="cnt-item-type">avg / p95</div>
                            <div class="cnt-item-ver">${escapeHtml(String(latency.avg || 0))}/${escapeHtml(String(latency.p95 || 0))}ms</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">budget breaches</div>
                            <div class="cnt-item-type">count</div>
                            <div class="cnt-item-ver">${escapeHtml(String(handoffStats.breaches || 0))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">pending handoff</div>
                            <div class="cnt-item-type">state</div>
                            <div class="cnt-item-ver">${handoff.pending ? 'pending' : 'idle'}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${continuityActive.name ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">active content</div>
                            <div class="cnt-item-type">${escapeHtml(String(continuityActive.branch || continuityWorkspace.branch || 'main'))}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(continuityActive.name || 'none'))}</div>
                            <div class="cnt-item-time"></div>
                            ${continuityActive.name && continuityActive.domain
                                ? `<button class="cnt-type-badge" type="button" data-shell-object-kind="repo" data-shell-object-domain="${escapeAttr(String(continuityActive.domain || 'document'))}" data-shell-object-name="${escapeAttr(String(continuityActive.name || ''))}" data-shell-object-branch="${escapeAttr(String(continuityActive.branch || continuityWorkspace.branch || 'main'))}" data-shell-object-item-id="${escapeAttr(String(continuityActive.itemId || ''))}">open</button>`
                                : ''}
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">worktrees</div>
                            <div class="cnt-item-type">mounted</div>
                            <div class="cnt-item-ver">${escapeHtml(String(continuityWorkspace.worktreeCount || 0))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${Number(continuityNotifications.unread || 0) > 0 ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">runtime inbox</div>
                            <div class="cnt-item-type">unread / total</div>
                            <div class="cnt-item-ver">${escapeHtml(String(continuityNotifications.unread || 0))}/${escapeHtml(String(continuityNotifications.count || 0))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                    </div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">devices</div>
                        <button class="cnt-type-badge" type="button" data-handoff-start="1">start handoff</button>
                        <button class="cnt-type-badge" type="button" data-continuity-prune="stale">prune stale</button>
                    </div>
                    <div class="cnt-history">${deviceRows || '<div class="cnt-empty">No active devices yet.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">paired surfaces</div>
                        ${pairedItems.some((item) => item.preferred) ? `<div class="cnt-type-badge">preferred set</div>` : '<div class="cnt-type-badge">none preferred</div>'}
                    </div>
                    <div class="cnt-history">${pairedRows || '<div class="cnt-empty">No paired Genome surfaces yet.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">handoff</div>
                        ${pendingToken ? `<button class="cnt-type-badge" type="button" data-handoff-copy="${escapeAttr(pendingToken)}" data-handoff-backend="${escapeAttr(pendingBackendUrl)}" data-handoff-bridge="${escapeAttr(pendingBridgeUrl)}">copy link</button>` : ''}
                        ${pendingShareUrl ? `<button class="cnt-type-badge" type="button" data-handoff-qr="${escapeAttr(pendingToken)}" data-handoff-backend="${escapeAttr(pendingBackendUrl)}" data-handoff-bridge="${escapeAttr(pendingBridgeUrl)}" data-handoff-expires="${escapeAttr(String(pendingExpires || ''))}">show qr</button>` : ''}
                    </div>
                    <div class="cnt-history">
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${pendingToken ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">pending token</div>
                            <div class="cnt-item-type">${pendingToken ? 'ready' : 'idle'}</div>
                            <div class="cnt-item-ver">${escapeHtml(pendingToken ? pendingToken : 'no pending handoff')}</div>
                            <div class="cnt-item-time">${escapeHtml(pendingExpires ? formatDate(pendingExpires) : '')}</div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${handoff.activeDeviceId ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">active device</div>
                            <div class="cnt-item-type">${handoff.activeDeviceId ? 'claimed' : 'idle'}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(handoff.activeDeviceId || 'none'))}</div>
                            <div class="cnt-item-time">${escapeHtml(handoff.lastClaimAt ? formatDate(handoff.lastClaimAt) : '')}</div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${pendingPair.targetSurfaceId ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">target surface</div>
                            <div class="cnt-item-type">${pendingPair.routed || pendingPair.woke ? 'requested' : 'waiting'}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(pendingPair.targetLabel || pendingPair.targetSurfaceId || 'none'))}</div>
                            <div class="cnt-item-time">${escapeHtml(String(pendingPair.targetRole || ''))}</div>
                        </div>
                    </div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">next actions</div>
                        <button class="cnt-type-badge" type="button" data-continuity-next-apply="1">apply next</button>
                    </div>
                    <div class="cnt-history">${nextRows || '<div class="cnt-empty">No continuity action needed right now.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">incidents</div>
                        ${incidents.summary?.topSeverity ? `<div class="cnt-type-badge">${escapeHtml(String(incidents.summary.topSeverity))}</div>` : ''}
                    </div>
                    <div class="cnt-history">${incidentRows || '<div class="cnt-empty">No continuity incidents recorded.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">autopilot</div>
                        <button class="cnt-type-badge" type="button" data-continuity-autopilot="${autopilot.enabled ? 'off' : 'on'}">${autopilot.enabled ? 'disable' : 'enable'}</button>
                        <button class="cnt-type-badge" type="button" data-continuity-autopilot-tick="1">tick</button>
                        <button class="cnt-type-badge" type="button" data-continuity-autopilot-mode="recommended">apply mode</button>
                        <button class="cnt-type-badge" type="button" data-continuity-autopilot-align="${autopilot.autoAlignMode ? 'off' : 'on'}">${autopilot.autoAlignMode ? 'auto-align off' : 'auto-align on'}</button>
                        <button class="cnt-type-badge" type="button" data-continuity-autopilot-reset="stats">reset stats</button>
                    </div>
                    <div class="cnt-history">
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${autopilot.enabled ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">mode</div>
                            <div class="cnt-item-type">${escapeHtml(String(autopilot.mode || 'normal'))}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(autopilot.lastResult || 'idle'))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">cooldown</div>
                            <div class="cnt-item-type">ms</div>
                            <div class="cnt-item-ver">${escapeHtml(String(autopilot.cooldownMs || 0))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon document"></div>
                            <div class="cnt-item-name">applies</div>
                            <div class="cnt-item-type">count</div>
                            <div class="cnt-item-ver">${escapeHtml(String(autopilot.applied || 0))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${autopilotSummary.modeDrifted ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">recommended</div>
                            <div class="cnt-item-type">${escapeHtml(String(autopilotSummary.recommendedMode || 'normal'))}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(autopilotSummary.previewReason || autopilotSummary.lastResult || 'ready'))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${Number(autopilotSummary.guardrailBlockers || 0) > 0 ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">guardrails</div>
                            <div class="cnt-item-type">${Number(autopilotSummary.guardrailBlockers || 0) > 0 ? 'blocked' : 'clear'}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(autopilotSummary.guardrailBlockers || 0))} blockers</div>
                            <div class="cnt-item-time"></div>
                        </div>
                    </div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">control plane</div>
                        <button class="cnt-type-badge" type="button" data-continuity-posture-batch="3">apply batch</button>
                        <button class="cnt-type-badge" type="button" data-continuity-autopilot-reset="history">clear history</button>
                    </div>
                    <div class="cnt-history">
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${previewInfo.appliable === false ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">preview</div>
                            <div class="cnt-item-type">${escapeHtml(String(previewInfo.reason || autopilotSummary.previewReason || 'unknown'))}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(previewInfo.command || previewInfo.title || 'next control step').slice(0, 72))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${Number(guardrailState.blockerCount || 0) > 0 ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">guardrail summary</div>
                            <div class="cnt-item-type">${Number(guardrailState.blockerCount || 0) > 0 ? 'blocked' : 'clear'}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(guardrailState.blockerCount || 0))} blockers · ${escapeHtml(String((guardrailState.codes || []).slice(0, 2).join(', ') || 'steady'))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${driftInfo.drifted ? 'generic' : 'document'}"></div>
                            <div class="cnt-item-name">mode drift</div>
                            <div class="cnt-item-type">${driftInfo.drifted ? 'drifted' : 'aligned'}</div>
                            <div class="cnt-item-ver">${escapeHtml(String(autopilotMode.recommendedMode || autopilot.mode || 'normal'))}</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${Number(autopilotMetricsSummary.recentCount || 0) > 0 ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">recent events</div>
                            <div class="cnt-item-type">1h</div>
                            <div class="cnt-item-ver">${escapeHtml(String(autopilotMetricsSummary.recentCount || 0))} events</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${Number(alignmentSummary.aligned || 0) > 0 ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">mode alignment</div>
                            <div class="cnt-item-type">recent</div>
                            <div class="cnt-item-ver">${escapeHtml(String(alignmentSummary.aligned || 0))}/${escapeHtml(String(alignmentSummary.count || 0))} aligned</div>
                            <div class="cnt-item-time"></div>
                        </div>
                        <div class="cnt-item">
                            <div class="cnt-item-icon ${Number(postureActionMetrics.summary?.applied || 0) > 0 ? 'document' : 'generic'}"></div>
                            <div class="cnt-item-name">posture actions</div>
                            <div class="cnt-item-type">1h</div>
                            <div class="cnt-item-ver">${escapeHtml(String(postureActionMetrics.summary?.applied || 0))}/${escapeHtml(String(postureActionMetrics.summary?.count || 0))} applied</div>
                            <div class="cnt-item-time"></div>
                        </div>
                    </div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">mode policy</div>
                    </div>
                    <div class="cnt-history">${modeMatrixRows || '<div class="cnt-empty">No autopilot mode policy matrix available.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">posture actions</div>
                        ${posturePolicyMatrix.summary?.allowed != null ? `<div class="cnt-type-badge">${escapeHtml(String(posturePolicyMatrix.summary.allowed || 0))} ready</div>` : ''}
                    </div>
                    <div class="cnt-history">${postureActionRows || '<div class="cnt-empty">No posture actions available.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">posture policy</div>
                    </div>
                    <div class="cnt-history">${posturePolicyRows || '<div class="cnt-empty">No posture policy rows available.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">control history</div>
                    </div>
                    <div class="cnt-history">${autopilotHistoryRows || '<div class="cnt-empty">No autopilot actions recorded yet.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">posture history</div>
                        ${postureAnomalies.summary?.count ? `<div class="cnt-type-badge">${escapeHtml(String(postureAnomalies.summary.count || 0))} anomalies</div>` : ''}
                    </div>
                    <div class="cnt-history">${postureHistoryRows || '<div class="cnt-empty">No posture snapshots recorded yet.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">posture anomalies</div>
                    </div>
                    <div class="cnt-history">${postureAnomalyRows || '<div class="cnt-empty">No posture anomalies detected.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">continuity history</div>
                        ${continuityAnomalies.summary?.topSeverity ? `<div class="cnt-type-badge">${escapeHtml(String(continuityAnomalies.summary.topSeverity || 'steady'))}</div>` : ''}
                    </div>
                    <div class="cnt-history">${continuityHistoryRows || '<div class="cnt-empty">No continuity history recorded yet.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">continuity anomalies</div>
                    </div>
                    <div class="cnt-history">${continuityAnomalyRows || '<div class="cnt-empty">No continuity anomalies detected.</div>'}</div>
                    <div class="cnt-header">
                        <div class="cnt-action-badge">alerts</div>
                    </div>
                    <div class="cnt-history">${alertRows || '<div class="cnt-empty">No continuity alerts right now.</div>'}</div>
                </div>
            </div>`;
        }
        // ── Drive files scene (when gdrive.list data is present) ───────────────
        if ((core.kind === 'drive' || (core.kind === 'document' && Array.isArray(core.info?.files) && core.info.files.length))) {
            const info  = core.info || {};
            const files = info.files.slice(0, 8);
            const mimeLabel = (mime) => {
                if (!mime) return 'document';
                if (mime.includes('sheet'))        return 'spreadsheet';
                if (mime.includes('presentation')) return 'presentation';
                if (mime.includes('word') || mime.includes('document')) return 'document';
                if (mime.includes('pdf'))          return 'pdf';
                if (mime.includes('image'))        return 'image';
                if (mime.includes('video'))        return 'video';
                return 'file';
            };
            const hero = files[0];
            const heroName = String(hero?.name || 'File').trim();
            const heroType = mimeLabel(hero?.mimeType);
            const streamHtml = files.slice(1).map((f, i) => {
                const opacity = Math.max(0.1, 0.5 - i * 0.07);
                const date = String(f.modifiedTime || '').slice(0, 10);
                return `<div class="drive-stream-item" style="opacity:${opacity}">
                    <span class="drive-stream-name">${escapeHtml(String(f.name || 'File').slice(0, 40))}</span>
                    ${date ? `<span class="drive-stream-date">${escapeHtml(date)}</span>` : ''}
                </div>`;
            }).join('');
            return `<div class="scene scene-computer scene-document interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="document"></canvas>
                <div class="drive-shell">
                    <div class="drive-eyebrow">google drive · ${files.length} files</div>
                    <div class="drive-hero">${escapeHtml(heroName)}</div>
                    <div class="drive-hero-type">${escapeHtml(heroType)}</div>
                    ${streamHtml ? `<div class="drive-stream">${streamHtml}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Alarm scene ───────────────────────────────────────────────────────
        if (core.kind === 'alarm') {
            const info   = core.info || {};
            const alarms = Array.isArray(info.alarms) ? info.alarms : [];
            const action = String(info.action || 'set').trim();
            const time   = String(info.time || (alarms[0] && alarms[0].time) || '').trim();
            const label  = String(info.label || (alarms[0] && alarms[0].label) || '').trim();
            const streamHtml = alarms.slice(action === 'set' ? 1 : 0, 6).map((a, i) => {
                const opacity = Math.max(0.1, 0.5 - i * 0.08);
                const daysStr = Array.isArray(a.days) && a.days.length ? a.days.join(' · ') : 'once';
                return `<div class="alarm-stream-item" style="opacity:${opacity}">
                    <span class="alarm-stream-time">${escapeHtml(String(a.time || ''))}</span>
                    <span class="alarm-stream-label">${escapeHtml(String(a.label || ''))}</span>
                    <span class="alarm-stream-days">${escapeHtml(daysStr)}</span>
                </div>`;
            }).join('');
            const heroTime = action === 'set' ? time : (alarms[0] && alarms[0].time) || time || '—';
            return `<div class="scene scene-alarm interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                <div class="alarm-shell">
                    <div class="alarm-eyebrow">${escapeHtml(action === 'list' ? 'all alarms' : `alarm ${action}`)}</div>
                    <div class="alarm-hero">${escapeHtml(heroTime || '—')}</div>
                    ${label ? `<div class="alarm-label">${escapeHtml(label)}</div>` : ''}
                    ${streamHtml ? `<div class="alarm-stream">${streamHtml}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Health scene ──────────────────────────────────────────────────────
        if (core.kind === 'health') {
            const info    = core.info || {};
            const metrics = (info.metrics && typeof info.metrics === 'object') ? info.metrics : {};
            const steps   = Number(metrics.steps || 0);
            const goal    = Number(metrics.steps_goal || 10000);
            const pct     = goal > 0 ? Math.min(100, Math.round((steps / goal) * 100)) : 0;
            const streamItems = [
                { label: 'calories', val: `${metrics.calories || 0} kcal` },
                { label: 'active',   val: `${metrics.active_min || 0} min` },
                { label: 'sleep',    val: `${metrics.sleep_hours || 0}h` },
                { label: 'heart',    val: `${metrics.heart_rate || 0} bpm` },
                { label: 'streak',   val: `${metrics.streak_days || 0} days` },
            ].filter(x => x.val && !x.val.startsWith('0'));
            const streamHtml = streamItems.map((item, i) => {
                const opacity = Math.max(0.12, 0.55 - i * 0.09);
                return `<div class="health-stream-item" style="opacity:${opacity}">
                    <span class="health-stream-label">${escapeHtml(item.label)}</span>
                    <span class="health-stream-val">${escapeHtml(item.val)}</span>
                </div>`;
            }).join('');
            return `<div class="scene scene-health interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                <div class="health-shell">
                    <div class="health-eyebrow">today · activity</div>
                    <div class="health-hero">${steps.toLocaleString()}</div>
                    <div class="health-hero-label">steps · ${pct}% of goal</div>
                    <div class="health-progress-bar"><div class="health-progress-fill" style="width:${pct}%"></div></div>
                    ${streamHtml ? `<div class="health-stream">${streamHtml}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Podcast scene ─────────────────────────────────────────────────────
        if (core.kind === 'podcast') {
            const info    = core.info || {};
            const show    = String(info.show    || '').trim();
            const episode = String(info.episode || '').trim();
            const prog    = Number(info.progress_ms || 0);
            const dur     = Number(info.duration_ms  || 0);
            const playing = Boolean(info.is_playing);
            const pct     = dur > 0 ? Math.min(100, Math.round((prog / dur) * 100)) : (playing ? 30 : 0);
            const fmtTime = (ms) => { const s = Math.floor(ms / 1000); const m = Math.floor(s / 60); const h = Math.floor(m / 60); return h > 0 ? `${h}h ${m % 60}m` : `${m}m`; };
            return `<div class="scene scene-podcast interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                <div class="podcast-shell">
                    <div class="podcast-eyebrow">${escapeHtml(show || 'podcast')}</div>
                    <div class="podcast-episode">${escapeHtml(episode || 'Now playing')}</div>
                    <div class="podcast-controls">
                        <span class="podcast-btn" data-command="rewind 30 seconds">&#9664;&#9664;</span>
                        <span class="podcast-btn podcast-playpause" data-command="${playing ? 'pause podcast' : 'play podcast'}">${playing ? '&#9646;&#9646;' : '&#9654;'}</span>
                        <span class="podcast-btn" data-command="skip 30 seconds">&#9654;&#9654;</span>
                    </div>
                    <div class="podcast-progress-bar">
                        <div class="podcast-progress-fill" style="width:${pct}%"></div>
                    </div>
                    <div class="podcast-times">
                        <span>${fmtTime(prog)}</span>
                        <span>${fmtTime(dur)}</span>
                    </div>
                </div>
            </div>`;
        }
        // ── Smarthome scene ───────────────────────────────────────────────────
        if (core.kind === 'smarthome') {
            const info    = core.info || {};
            const devices = Array.isArray(info.devices) ? info.devices : [
                { name: 'Living Room', type: 'lights', state: 'on',     brightness: 70 },
                { name: 'Thermostat',  type: 'thermostat', state: 'on', temp: 72 },
                { name: 'Front Door',  type: 'lock',   state: 'locked' },
                { name: 'Bedroom',     type: 'lights', state: 'off',    brightness: 0 },
            ];
            const temp   = Number(info.temp || (devices.find(d => d.type === 'thermostat')?.temp) || 72);
            const unit   = String(info.unit || 'F');
            const active = devices.filter(d => d.state === 'on' || d.state === 'locked').length;
            const devHtml = devices.slice(0, 6).map((dv, i) => {
                const opacity = Math.max(0.15, 0.6 - i * 0.08);
                const on  = dv.state === 'on' || dv.state === 'locked';
                const val = dv.type === 'thermostat' ? `${dv.temp || temp}°${unit}`
                          : dv.brightness != null    ? `${dv.brightness}%`
                          : (on ? 'on' : 'off');
                return `<div class="sh-device-row" data-state="${on ? 'on' : 'off'}" style="opacity:${opacity}">
                    <span class="sh-device-name">${escapeHtml(String(dv.name || 'Device').slice(0, 24))}</span>
                    <span class="sh-device-val">${escapeHtml(val)}</span>
                    <span class="sh-device-dot ${on ? 'dot-on' : 'dot-off'}"></span>
                </div>`;
            }).join('');
            const heroLabel = String(info.action || '') === 'thermostat' ? `${temp}°${unit}` : `${active} active`;
            return `<div class="scene scene-smarthome interactive">
                <canvas class="scene-canvas smarthome-canvas" data-scene="smarthome"></canvas>
                <div class="sh-shell">
                    <div class="sh-eyebrow">home · ${devices.length} devices</div>
                    <div class="sh-hero">${escapeHtml(heroLabel)}</div>
                    <div class="sh-hero-label">${active} on · ${devices.length - active} off</div>
                    ${devHtml ? `<div class="sh-device-list">${devHtml}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Travel scene ──────────────────────────────────────────────────────
        if (core.kind === 'travel') {
            const info    = core.info || {};
            const flights = Array.isArray(info.flights) ? info.flights : [];
            const hotels  = Array.isArray(info.hotels)  ? info.hotels  : [];
            const first   = flights[0] || {};
            const hero    = first.dest || first.origin
                ? `${first.origin || '?'} → ${first.dest || '?'}`
                : (hotels[0]?.name || 'Upcoming Trip');
            const sub     = first.airline
                ? `${first.airline}${first.depart ? ' · ' + first.depart : ''}`
                : (first.status || 'travel');
            const streamItems = [
                ...flights.slice(1).map(f => ({ label: `${f.origin || ''}→${f.dest || ''}`, val: f.depart || f.status || 'flight' })),
                ...hotels.slice(0, 3).map(h => ({ label: h.name || 'Hotel', val: h.checkin || 'hotel' })),
            ].slice(0, 5);
            const streamHtml = streamItems.map((item, i) => {
                const opacity = Math.max(0.1, 0.5 - i * 0.08);
                return `<div class="travel-stream-item" style="opacity:${opacity}">
                    <span class="travel-stream-label">${escapeHtml(String(item.label).slice(0, 30))}</span>
                    <span class="travel-stream-val">${escapeHtml(String(item.val).slice(0, 20))}</span>
                </div>`;
            }).join('');
            return `<div class="scene scene-travel interactive">
                <canvas class="scene-canvas travel-canvas" data-scene="travel"></canvas>
                <div class="travel-shell">
                    <div class="travel-eyebrow">${escapeHtml(String(info.action || 'travel').replace(/_/g, ' '))} · itinerary</div>
                    <div class="travel-hero">${escapeHtml(hero)}</div>
                    <div class="travel-hero-sub">${escapeHtml(sub)}</div>
                    ${streamHtml ? `<div class="travel-stream">${streamHtml}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Payments scene ────────────────────────────────────────────────────
        if (core.kind === 'payments') {
            const info    = core.info || {};
            const action  = String(info.action || info.op || '').replace(/_/g, ' ').trim();
            const amount  = Number(info.amount  || 0);
            const balance = Number(info.balance || 0);
            const recipient    = String(info.recipient || '').trim();
            const transactions = Array.isArray(info.transactions) ? info.transactions : [];
            const heroAmt   = amount  ? `$${amount.toFixed(2)}`  : (balance ? `$${balance.toFixed(2)}` : '—');
            const heroLabel = recipient ? `→ ${recipient}` : (action || 'payment');
            const txHtml = transactions.slice(0, 5).map((tx, i) => {
                const opacity = Math.max(0.1, 0.5 - i * 0.08);
                const sign = Number(tx.amount || 0) >= 0 ? '+' : '';
                return `<div class="pay-tx-row" style="opacity:${opacity}">
                    <span class="pay-tx-name">${escapeHtml(String(tx.name || tx.recipient || 'Transfer').slice(0, 28))}</span>
                    <span class="pay-tx-amt">${escapeHtml(sign + '$' + Math.abs(Number(tx.amount || 0)).toFixed(2))}</span>
                </div>`;
            }).join('');
            return `<div class="scene scene-payments interactive">
                <canvas class="scene-canvas payments-canvas" data-scene="payments"></canvas>
                <div class="pay-shell">
                    <div class="pay-eyebrow">${escapeHtml(String(info.source || 'scaffold'))} · payments</div>
                    <div class="pay-hero">${escapeHtml(heroAmt)}</div>
                    <div class="pay-hero-label">${escapeHtml(heroLabel)}</div>
                    ${txHtml ? `<div class="pay-tx-list">${txHtml}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Focus scene ───────────────────────────────────────────────────────
        if (core.kind === 'focus') {
            const info    = core.info || {};
            const mode    = String(info.mode || String(info.op || '').replace('focus.', '')).trim() || 'focus';
            const dur     = Number(info.duration_min || 25);
            const action  = String(info.action || '').trim();
            const blocked = Array.isArray(info.apps_blocked) ? info.apps_blocked : [];
            const isPomodoro = mode === 'pomodoro' || info.op === 'focus.pomodoro';
            const heroVal   = isPomodoro ? `${dur}m` : (dur ? `${dur} min` : 'Focus');
            const heroLabel = isPomodoro ? 'pomodoro · deep work' : `${mode.replace(/_/g, ' ')} session`;
            const pct  = Math.min(100, Math.round((dur / (isPomodoro ? 25 : 60)) * 100));
            const circ = Math.round(2 * Math.PI * 20);
            const dash = Math.round(circ * pct / 100);
            const blockedHtml = blocked.slice(0, 6).map((app, i) => {
                const opacity = Math.max(0.15, 0.55 - i * 0.08);
                return `<span class="focus-blocked-app" style="opacity:${opacity}">${escapeHtml(String(app).slice(0, 16))}</span>`;
            }).join('');
            return `<div class="scene scene-focus interactive">
                <canvas class="scene-canvas focus-canvas" data-scene="focus"></canvas>
                <div class="focus-shell">
                    <div class="focus-eyebrow">${escapeHtml(action || 'starting')} · focus mode</div>
                    <div class="focus-hero">${escapeHtml(heroVal)}</div>
                    <div class="focus-hero-label">${escapeHtml(heroLabel)}</div>
                    <svg class="focus-ring" viewBox="0 0 48 48" width="48" height="48">
                        <circle cx="24" cy="24" r="20" fill="none" stroke="rgba(120,200,160,0.18)" stroke-width="4"/>
                        <circle cx="24" cy="24" r="20" fill="none" stroke="rgba(120,200,160,0.8)" stroke-width="4"
                            stroke-dasharray="${dash} ${circ}" stroke-linecap="round"
                            transform="rotate(-90 24 24)"/>
                    </svg>
                    ${blocked.length ? `<div class="focus-blocked-label">blocking ${blocked.length} apps</div><div class="focus-blocked-row">${blockedHtml}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Video scene ───────────────────────────────────────────────────────
        if (core.kind === 'video') {
            const info     = core.info || {};
            const title    = String(info.title   || '').trim();
            const show     = String(info.show    || '').trim();
            const service  = String(info.service || '').trim();
            const action   = String(info.action  || '').trim();
            const results  = Array.isArray(info.results) ? info.results : [];
            const prog     = Number(info.progress_ms || 0);
            const dur      = Number(info.duration_ms  || 0);
            const pct      = dur > 0 ? Math.min(100, Math.round((prog / dur) * 100)) : 0;
            const hero     = title || show || (results[0]?.title) || 'Now Watching';
            const eyebrow  = service ? `${service} · ${action || 'streaming'}` : (action || 'video');
            const streamHtml = results.slice(0, 5).map((r, i) => {
                const opacity = Math.max(0.1, 0.5 - i * 0.08);
                const label = String(r.title || r.show || '').slice(0, 36);
                const meta  = String(r.year || r.genre || r.type || '').slice(0, 16);
                return `<div class="video-stream-item" style="opacity:${opacity}">
                    <span class="video-stream-title">${escapeHtml(label)}</span>
                    ${meta ? `<span class="video-stream-meta">${escapeHtml(meta)}</span>` : ''}
                </div>`;
            }).join('');
            return `<div class="scene scene-video interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="video"></canvas>
                <div class="video-shell">
                    <div class="video-eyebrow">${escapeHtml(eyebrow)}</div>
                    <div class="video-hero">${escapeHtml(hero)}</div>
                    ${show && title ? `<div class="video-show">${escapeHtml(show)}</div>` : ''}
                    ${dur > 0 ? `<div class="video-progress-bar"><div class="video-progress-fill" style="width:${pct}%"></div></div>` : ''}
                    ${streamHtml ? `<div class="video-stream">${streamHtml}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Food delivery scene ───────────────────────────────────────────────
        if (core.kind === 'food_delivery') {
            const info       = core.info || {};
            const restaurant = String(info.restaurant || 'Your Order').trim();
            const eta        = String(info.eta    || '').trim();
            const status     = String(info.status || 'preparing').trim();
            const items      = Array.isArray(info.items) ? info.items : [];
            const total      = Number(info.total || 0);
            const itemsHtml  = items.slice(0, 5).map((item, i) => {
                const opacity = Math.max(0.1, 0.5 - i * 0.08);
                const qty   = item.qty || item.quantity || 1;
                const name  = String(item.name || item.title || 'Item').slice(0, 30);
                return `<div class="food-item-row" style="opacity:${opacity}">
                    <span class="food-item-qty">${escapeHtml(String(qty))}×</span>
                    <span class="food-item-name">${escapeHtml(name)}</span>
                </div>`;
            }).join('');
            const statusSteps = ['placed','preparing','ready','picked up','on the way','delivered'];
            const stepIdx = statusSteps.findIndex(s => status.toLowerCase().includes(s.split(' ')[0]));
            const stepPct = stepIdx >= 0 ? Math.round(((stepIdx + 1) / statusSteps.length) * 100) : 30;
            return `<div class="scene scene-food-delivery interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="food_delivery"></canvas>
                <div class="food-shell">
                    <div class="food-eyebrow">food delivery · ${escapeHtml(status)}</div>
                    <div class="food-hero">${escapeHtml(restaurant)}</div>
                    ${eta ? `<div class="food-eta">ETA <strong>${escapeHtml(eta)}</strong></div>` : ''}
                    <div class="food-progress-bar"><div class="food-progress-fill" style="width:${stepPct}%"></div></div>
                    ${itemsHtml ? `<div class="food-items">${itemsHtml}</div>` : ''}
                    ${total ? `<div class="food-total">$${escapeHtml(total.toFixed(2))}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Rideshare scene ───────────────────────────────────────────────────
        if (core.kind === 'rideshare') {
            const info    = core.info || {};
            const dest    = String(info.dest    || '').trim();
            const driver  = String(info.driver  || '').trim();
            const eta     = String(info.eta     || '').trim();
            const status  = String(info.status  || 'booking').trim();
            const price   = String(info.price   || '').trim();
            const vehicle = String(info.vehicle || '').trim();
            const statusSteps = ['booking','driver found','en route','arrived','trip started','dropped off'];
            const stepIdx = statusSteps.findIndex(s => status.toLowerCase().includes(s.split(' ')[0]));
            const stepPct = stepIdx >= 0 ? Math.round(((stepIdx + 1) / statusSteps.length) * 100) : 20;
            return `<div class="scene scene-rideshare interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="rideshare"></canvas>
                <div class="ride-shell">
                    <div class="ride-eyebrow">rideshare · ${escapeHtml(status)}</div>
                    <div class="ride-hero">${escapeHtml(dest || 'Your Ride')}</div>
                    ${driver ? `<div class="ride-driver">${escapeHtml(driver)}${vehicle ? ' · ' + escapeHtml(vehicle) : ''}</div>` : ''}
                    ${eta ? `<div class="ride-eta">ETA <strong>${escapeHtml(eta)}</strong></div>` : ''}
                    <div class="ride-progress-bar"><div class="ride-progress-fill" style="width:${stepPct}%"></div></div>
                    ${price ? `<div class="ride-price">${escapeHtml(price)}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Camera scene ──────────────────────────────────────────────────────
        if (core.kind === 'camera') {
            const info = core.info || {};
            const mode = String(info.mode || 'photo').trim();
            const modeLabel = mode === 'scan' ? 'Document Scanner' : mode === 'video' ? 'Video Camera' : mode === 'selfie' ? 'Selfie' : 'Camera';
            const modeIcon  = mode === 'scan' ? '⬛' : mode === 'video' ? '⬛' : '⬛';
            return `<div class="scene scene-camera interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="camera"></canvas>
                <div class="cam-shell">
                    <div class="cam-eyebrow">camera · ${escapeHtml(mode)}</div>
                    <div class="cam-hero">${escapeHtml(modeLabel)}</div>
                    <div class="cam-viewfinder">
                        <div class="cam-corner cam-corner-tl"></div>
                        <div class="cam-corner cam-corner-tr"></div>
                        <div class="cam-corner cam-corner-bl"></div>
                        <div class="cam-corner cam-corner-br"></div>
                    </div>
                    <div class="cam-mode-label">${escapeHtml(String(info.action || 'ready'))}</div>
                </div>
            </div>`;
        }
        // ── Photos scene ──────────────────────────────────────────────────────
        if (core.kind === 'photos') {
            const info   = core.info || {};
            const count  = Number(info.count  || 0);
            const album  = String(info.album  || '').trim();
            const query  = String(info.query  || '').trim();
            const photos = Array.isArray(info.photos) ? info.photos : [];
            const heroLabel = album || query || (count ? `${count.toLocaleString()} photos` : 'Library');
            const gridHtml = photos.slice(0, 6).map((p, i) => {
                const opacity = Math.max(0.2, 0.8 - i * 0.1);
                return `<div class="photos-grid-cell" style="opacity:${opacity};background:${p.color || 'rgba(255,255,255,0.06)'}"></div>`;
            }).join('') || [0,1,2,3,4,5].map(i => `<div class="photos-grid-cell" style="opacity:${Math.max(0.05, 0.4 - i*0.06)}"></div>`).join('');
            return `<div class="scene scene-photos interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="photos"></canvas>
                <div class="photos-shell">
                    <div class="photos-eyebrow">photos${album ? ' · ' + escapeHtml(album) : ''}</div>
                    <div class="photos-hero">${escapeHtml(heroLabel)}</div>
                    <div class="photos-grid">${gridHtml}</div>
                </div>
            </div>`;
        }
        // ── Clock / Timer scene ────────────────────────────────────────────────
        if (core.kind === 'clock') {
            const info = core.info || {};
            const op   = String(info.op || '');

            // ── world clocks ───────────────────────────────────────────────
            if (op === 'clock_world') {
                const clocks = Array.isArray(info.clocks) ? info.clocks : [];
                const tiles = clocks.map(c => `
                    <div class="cw-tile">
                        <div class="cw-city">${escapeHtml(c.city || c.zone)}</div>
                        <div class="cw-time">${escapeHtml(c.time)}</div>
                        <div class="cw-date">${escapeHtml(c.date || '')}</div>
                    </div>`).join('');
                return `<div class="scene scene-clock interactive">
                    <canvas class="scene-canvas computer-canvas" data-scene="clock"></canvas>
                    <div class="cw-shell">
                        <div class="clock-eyebrow">world clocks · ${clocks.length} zones</div>
                        <div class="cw-grid">${tiles || '<div class="cw-tile"><div class="cw-city">—</div></div>'}</div>
                    </div>
                </div>`;
            }

            // ── bedtime ────────────────────────────────────────────────────
            if (op === 'clock_bedtime') {
                const bedtimes = Array.isArray(info.bedtimes) ? info.bedtimes : [];
                const wake     = String(info.wake_time || '');
                const rows = bedtimes.map((b, i) => {
                    const best = i === 1;
                    return `<div class="cb-row${best ? ' cb-best' : ''}">
                        <div class="cb-time">${escapeHtml(b.time)}</div>
                        <div class="cb-meta">${b.cycles} cycles · ${b.hours}h</div>
                        ${best ? '<div class="cb-tag">recommended</div>' : ''}
                    </div>`;
                }).join('');
                return `<div class="scene scene-clock interactive">
                    <canvas class="scene-canvas computer-canvas" data-scene="clock"></canvas>
                    <div class="clock-shell">
                        <div class="clock-eyebrow">bedtime${wake ? ' · wake ' + escapeHtml(wake) : ''}</div>
                        <div class="cb-list">${rows || '<div class="cb-row"><div class="cb-time">Set a wake time</div></div>'}</div>
                    </div>
                </div>`;
            }

            // ── date calc ──────────────────────────────────────────────────
            if (['date_age','date_countdown','date_day_of','date_days_until'].includes(op)) {
                const ageYears  = info.age_years != null ? Number(info.age_years) : null;
                const dayName   = String(info.day_name || '');
                const delta     = info.delta_days != null ? Number(info.delta_days) : null;
                const label     = String(info.label || '');
                const dateStr   = String(info.date || info.target || '');
                const weekNum   = info.week_number ? ` · week ${info.week_number}` : '';
                const bday      = info.days_to_birthday != null ? `next birthday in ${info.days_to_birthday} days` : '';
                const heroText  = ageYears != null ? `${ageYears}` : dayName || (delta != null ? String(Math.abs(delta)) : '—');
                const heroLabel = ageYears != null ? 'years old'
                                : dayName          ? dateStr + weekNum
                                : delta != null    ? `days ${delta < 0 ? 'ago' : 'until'} ${label || dateStr}`
                                : '';
                return `<div class="scene scene-clock interactive">
                    <canvas class="scene-canvas computer-canvas" data-scene="clock"></canvas>
                    <div class="clock-shell">
                        <div class="clock-eyebrow">${escapeHtml(op.replace('_',' '))}</div>
                        <div class="clock-display" style="font-size:clamp(36px,6vw,64px)">${escapeHtml(heroText)}</div>
                        <div class="clock-state">${escapeHtml(heroLabel)}</div>
                        ${bday ? `<div class="clock-eyebrow" style="margin-top:8px;opacity:.7">${escapeHtml(bday)}</div>` : ''}
                    </div>
                </div>`;
            }

            // ── timer / stopwatch (default) ────────────────────────────────
            const mode    = String(info.mode    || 'timer').trim();
            const label   = String(info.label   || '').trim();
            const dur     = Number(info.duration_ms || 0);
            const elapsed = Number(info.elapsed_ms  || 0);
            const running = Boolean(info.running);
            const fmtMs   = (ms) => { const s = Math.floor(ms/1000); const m = Math.floor(s/60); const h = Math.floor(m/60); return h > 0 ? `${h}:${String(m%60).padStart(2,'0')}:${String(s%60).padStart(2,'0')}` : `${String(m).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`; };
            const display = dur ? fmtMs(dur) : (elapsed ? fmtMs(elapsed) : '00:00');
            const pct     = dur > 0 && elapsed > 0 ? Math.min(100, Math.round((elapsed / dur) * 100)) : 0;
            const circ    = Math.round(2 * Math.PI * 36);
            const dash    = Math.round(circ * pct / 100);
            return `<div class="scene scene-clock interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="clock"></canvas>
                <div class="clock-shell">
                    <div class="clock-eyebrow">${escapeHtml(mode)}${label ? ' · ' + escapeHtml(label) : ''}</div>
                    <div class="clock-display">${escapeHtml(display)}</div>
                    <div class="clock-state">${running ? 'running' : 'ready'}</div>
                    <svg class="clock-ring" viewBox="0 0 80 80" width="64" height="64">
                        <circle cx="40" cy="40" r="36" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="4"/>
                        <circle cx="40" cy="40" r="36" fill="none" stroke="rgba(255,200,80,0.8)" stroke-width="4"
                            stroke-dasharray="${dash} ${circ}" stroke-linecap="round"
                            transform="rotate(-90 40 40)"/>
                    </svg>
                </div>
            </div>`;
        }
        // ── Reference scene (dictionary, wikipedia, currency, units) ──────────
        if (core.kind === 'reference') {
            const info = core.info || {};
            const op   = String(info.op || '');

            // definition or etymology
            if (op === 'dict_define' || op === 'dict_etymology') {
                const word     = String(info.word || '');
                const phonetic = String(info.phonetic || '');
                const origin   = String(info.origin || '');
                const meanings = Array.isArray(info.meanings) ? info.meanings : [];
                const defsHtml = meanings.slice(0,3).map(m => {
                    const defs = (m.definitions || []).slice(0,2).map(d => `<div class="ref-def">${escapeHtml(d)}</div>`).join('');
                    const ex   = (m.examples  || []).slice(0,1).map(e => `<div class="ref-example">"${escapeHtml(e)}"</div>`).join('');
                    return `<div class="ref-meaning"><div class="ref-pos">${escapeHtml(m.pos || '')}</div>${defs}${ex}</div>`;
                }).join('');
                return `<div class="scene scene-reference interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                    <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="ref-shell">
                        <div class="ref-word">${escapeHtml(word)}</div>
                        ${phonetic ? `<div class="ref-phonetic">${escapeHtml(phonetic)}</div>` : ''}
                        ${op === 'dict_etymology' && origin ? `<div class="ref-origin"><span class="ref-pos">origin</span> ${escapeHtml(origin)}</div>` : ''}
                        <div class="ref-meanings">${defsHtml || '<div class="ref-def">No definition found.</div>'}</div>
                    </div>
                </div>`;
            }

            // thesaurus
            if (op === 'dict_thesaurus') {
                const word     = String(info.word || '');
                const synonyms = Array.isArray(info.synonyms) ? info.synonyms : [];
                const antonyms = Array.isArray(info.antonyms) ? info.antonyms : [];
                const synPills = synonyms.slice(0,14).map(w => `<span class="ref-pill ref-syn">${escapeHtml(w)}</span>`).join('');
                const antPills = antonyms.slice(0,6).map(w => `<span class="ref-pill ref-ant">${escapeHtml(w)}</span>`).join('');
                return `<div class="scene scene-reference interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                    <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="ref-shell">
                        <div class="ref-word">${escapeHtml(word)}</div>
                        <div class="ref-section-label">synonyms</div>
                        <div class="ref-pill-row">${synPills || '<span class="ref-pill">none found</span>'}</div>
                        ${antPills ? `<div class="ref-section-label" style="margin-top:10px">antonyms</div><div class="ref-pill-row">${antPills}</div>` : ''}
                    </div>
                </div>`;
            }

            // wikipedia
            if (op === 'dict_wikipedia') {
                const title     = String(info.title || '');
                const desc      = String(info.desc  || '');
                const extract   = String(info.extract || '');
                const thumbnail = String(info.thumbnail || '');
                return `<div class="scene scene-reference interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                    <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="ref-shell">
                        ${thumbnail ? `<div class="ref-thumb-wrap"><img class="ref-thumb" src="${encodeURI(thumbnail)}" alt="" loading="lazy"></div>` : ''}
                        <div class="ref-word">${escapeHtml(title)}</div>
                        ${desc ? `<div class="ref-phonetic">${escapeHtml(desc)}</div>` : ''}
                        <div class="ref-extract">${escapeHtml(extract.slice(0,500))}</div>
                    </div>
                </div>`;
            }

            // currency rates
            if (op === 'currency_rates') {
                const base  = String(info.base || 'USD');
                const major = (info.major && typeof info.major === 'object') ? info.major : {};
                const rows  = Object.entries(major).filter(([k]) => k !== base).slice(0,10).map(([sym, val], i) => {
                    const opacity = Math.max(0.15, 0.8 - i * 0.07);
                    return `<div class="cur-row" style="opacity:${opacity}">
                        <span class="cur-sym">${escapeHtml(sym)}</span>
                        <span class="cur-val">${Number(val).toFixed(4)}</span>
                    </div>`;
                }).join('');
                return `<div class="scene scene-reference interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                    <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="cur-shell">
                        <div class="ref-phonetic">base currency</div>
                        <div class="ref-word">${escapeHtml(base)}</div>
                        <div class="cur-list">${rows || '<div class="cur-row">No rates available</div>'}</div>
                    </div>
                </div>`;
            }

            // currency convert or unit convert
            if (op === 'currency_convert' || op === 'unit_convert') {
                const value  = op === 'currency_convert' ? Number(info.amount || 1) : Number(info.value || 1);
                const from   = String(info.from || '');
                const to     = String(info.to   || '');
                const result = Number(info.result || 0);
                const rate   = info.rate  ? Number(info.rate) : null;
                const cat    = String(info.category || '');
                return `<div class="scene scene-reference interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                    <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="conv-shell">
                        <div class="conv-from"><span class="conv-val">${escapeHtml(String(value))}</span><span class="conv-unit">${escapeHtml(from)}</span></div>
                        <div class="conv-arrow">↓</div>
                        <div class="conv-to"><span class="conv-val">${escapeHtml(result.toLocaleString(undefined,{maximumFractionDigits:6}))}</span><span class="conv-unit">${escapeHtml(to)}</span></div>
                        ${rate ? `<div class="conv-rate">rate: 1 ${escapeHtml(from)} = ${escapeHtml(String(rate))} ${escapeHtml(to)}</div>` : ''}
                        ${cat ? `<div class="conv-rate">${escapeHtml(cat)}</div>` : ''}
                    </div>
                </div>`;
            }

            // fallback
            return `<div class="scene scene-reference interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                <div class="scene-grid"></div>
                <div class="ref-shell"><div class="ref-word">${escapeHtml(core.headline || 'Reference')}</div></div>
            </div>`;
        }
        // ── Recipe scene ───────────────────────────────────────────────────────
        if (core.kind === 'recipe') {
            const info        = core.info || {};
            const name        = String(info.name     || '').trim();
            const cuisine     = String(info.cuisine  || '').trim();
            const time_min    = Number(info.time_min || 0);
            const servings    = Number(info.servings || 0);
            const ingredients = Array.isArray(info.ingredients) ? info.ingredients : [];
            const results     = Array.isArray(info.results) ? info.results : [];
            const hero        = name || (results[0]?.name) || 'Recipe';
            const ingHtml = ingredients.slice(0, 5).map((ing, i) => {
                const opacity = Math.max(0.12, 0.55 - i * 0.09);
                const item = typeof ing === 'string' ? ing : String(ing.name || ing.item || ing);
                return `<div class="recipe-ing-row" style="opacity:${opacity}">${escapeHtml(item.slice(0, 40))}</div>`;
            }).join('');
            const resHtml = !ingredients.length ? results.slice(0, 4).map((r, i) => {
                const opacity = Math.max(0.12, 0.55 - i * 0.09);
                return `<div class="recipe-ing-row" style="opacity:${opacity}">${escapeHtml(String(r.name || r.title || '').slice(0, 36))}</div>`;
            }).join('') : '';
            return `<div class="scene scene-recipe interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="recipe"></canvas>
                <div class="recipe-shell">
                    <div class="recipe-eyebrow">${[cuisine, time_min ? time_min + ' min' : '', servings ? servings + ' servings' : ''].filter(Boolean).join(' · ') || 'recipe'}</div>
                    <div class="recipe-hero">${escapeHtml(hero)}</div>
                    ${(ingHtml || resHtml) ? `<div class="recipe-ing-list">${ingHtml || resHtml}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Grocery scene ──────────────────────────────────────────────────────
        if (core.kind === 'grocery') {
            const info   = core.info || {};
            const items  = Array.isArray(info.items) ? info.items : [];
            const done   = Number(info.done || items.filter(it => it.checked || it.done).length);
            const total  = items.length;
            const pct    = total > 0 ? Math.round((done / total) * 100) : 0;
            const itemsHtml = items.slice(0, 6).map((it, i) => {
                const opacity = Math.max(0.12, 0.55 - i * 0.08);
                const checked = Boolean(it.checked || it.done);
                const name    = String(it.name || it.item || it).slice(0, 32);
                return `<div class="grocery-row ${checked ? 'grocery-row-done' : ''}" style="opacity:${opacity}">
                    <span class="grocery-check">${checked ? '✓' : '·'}</span>
                    <span class="grocery-name">${escapeHtml(name)}</span>
                    ${it.qty ? `<span class="grocery-qty">${escapeHtml(String(it.qty))}</span>` : ''}
                </div>`;
            }).join('');
            return `<div class="scene scene-grocery interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="grocery"></canvas>
                <div class="grocery-shell">
                    <div class="grocery-eyebrow">grocery list · ${done}/${total} done</div>
                    <div class="grocery-hero">${total} item${total !== 1 ? 's' : ''}</div>
                    <div class="grocery-progress-bar"><div class="grocery-progress-fill" style="width:${pct}%"></div></div>
                    ${itemsHtml ? `<div class="grocery-list">${itemsHtml}</div>` : ''}
                </div>
            </div>`;
        }
        // ── Translate scene ────────────────────────────────────────────────────
        if (core.kind === 'translate') {
            const info   = core.info || {};
            const src    = String(info.src    || '').trim();
            const result = String(info.result || '').trim();
            const from   = String(info.from   || '').trim();
            const to     = String(info.to     || '').trim();
            return `<div class="scene scene-translate interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="translate"></canvas>
                <div class="trans-shell">
                    <div class="trans-eyebrow">${from && to ? escapeHtml(from + ' → ' + to) : 'translate'}</div>
                    ${src    ? `<div class="trans-source">${escapeHtml(src.slice(0, 120))}</div>` : ''}
                    ${result ? `<div class="trans-result">${escapeHtml(result.slice(0, 120))}</div>` : ''}
                    ${!src && !result ? `<div class="trans-result">Translation ready</div>` : ''}
                </div>
            </div>`;
        }
        // ── Books scene ────────────────────────────────────────────────────────
        if (core.kind === 'book') {
            const info     = core.info || {};
            const title    = String(info.title  || '').trim();
            const author   = String(info.author || '').trim();
            const progress = Number(info.progress || 0);
            const results  = Array.isArray(info.results) ? info.results : [];
            const hero     = title || (results[0]?.title) || 'Books';
            const resHtml  = !title ? results.slice(0, 4).map((r, i) => {
                const opacity = Math.max(0.12, 0.55 - i * 0.09);
                return `<div class="book-stream-item" style="opacity:${opacity}">
                    <span class="book-stream-title">${escapeHtml(String(r.title || '').slice(0, 36))}</span>
                    ${r.author ? `<span class="book-stream-author">${escapeHtml(String(r.author).slice(0, 24))}</span>` : ''}
                </div>`;
            }).join('') : '';
            return `<div class="scene scene-book interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="book"></canvas>
                <div class="book-shell">
                    <div class="book-eyebrow">${author ? escapeHtml(author) : 'books'}</div>
                    <div class="book-hero">${escapeHtml(hero)}</div>
                    ${progress > 0 ? `<div class="book-progress-bar"><div class="book-progress-fill" style="width:${progress}%"></div></div><div class="book-pct">${progress}% complete</div>` : ''}
                    ${resHtml ? `<div class="book-stream">${resHtml}</div>` : ''}
                </div>
            </div>`;
        }
        // ── GitHub scene ──────────────────────────────────────────────────────
        if (core.kind === 'github') {
            const info  = core.info || {};
            const items = Array.isArray(info.items) ? info.items : [];
            const op    = String(info.op || '');
            const repo  = String(info.repo || '');
            if (op === 'github_issue_create') {
                return `<div class="scene scene-enterprise interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="enterprise"></canvas>
                    <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="ent-shell">
                        <div class="ent-eyebrow">github · issue created</div>
                        <div class="ent-hero">#${escapeHtml(String(info.number || '?'))}</div>
                        ${info.title ? `<div class="ent-sub">${escapeHtml(String(info.title).slice(0,60))}</div>` : ''}
                        ${repo ? `<div class="ent-meta">${escapeHtml(repo)}</div>` : ''}
                    </div>
                </div>`;
            }
            const label = String(info.kind || 'prs') === 'issues' ? 'issues' : 'pull requests';
            const rowsHtml = items.slice(0, 8).map((it, i) => {
                const opacity = Math.max(0.12, 0.72 - i * 0.08);
                const state = String(it.state || 'open');
                return `<div class="ent-row" style="opacity:${opacity}">
                    <span class="ent-row-num">#${escapeHtml(String(it.number || i+1))}</span>
                    <span class="ent-row-title">${escapeHtml(String(it.title || '').slice(0,52))}</span>
                    <span class="ent-row-state ent-state-${escapeAttr(state)}">${escapeHtml(state)}</span>
                </div>`;
            }).join('');
            return `<div class="scene scene-enterprise interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="enterprise"></canvas>
                <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                <div class="scene-grid"></div>
                <div class="ent-shell">
                    <div class="ent-eyebrow">github · ${escapeHtml(label)}</div>
                    <div class="ent-hero">${items.length} open</div>
                    ${repo ? `<div class="ent-meta">${escapeHtml(repo)}</div>` : ''}
                    <div class="ent-list">${rowsHtml || '<div class="ent-row"><span class="ent-row-title">No items found</span></div>'}</div>
                </div>
            </div>`;
        }
        // ── Jira scene ────────────────────────────────────────────────────────
        if (core.kind === 'jira') {
            const info   = core.info || {};
            const issues = Array.isArray(info.issues) ? info.issues : [];
            const op     = String(info.op || '');
            if (op === 'jira_create' || op === 'jira_update') {
                const label  = op === 'jira_create' ? 'issue created' : 'issue updated';
                const key    = String(info.key || '?');
                const detail = String(info.summary || info.status || '').slice(0, 60);
                return `<div class="scene scene-enterprise interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="enterprise"></canvas>
                    <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="ent-shell">
                        <div class="ent-eyebrow">jira · ${escapeHtml(label)}</div>
                        <div class="ent-hero">${escapeHtml(key)}</div>
                        ${detail ? `<div class="ent-sub">${escapeHtml(detail)}</div>` : ''}
                    </div>
                </div>`;
            }
            const priColor = (p) => {
                const pl = (p || '').toLowerCase();
                return pl === 'critical' || pl === 'highest' ? '#ff5555'
                     : pl === 'high'   ? '#ff8844'
                     : pl === 'medium' ? '#ffcc44'
                     : 'rgba(170,187,204,0.7)';
            };
            const rowsHtml = issues.slice(0, 8).map((iss, i) => {
                const opacity = Math.max(0.12, 0.72 - i * 0.08);
                return `<div class="ent-row" style="opacity:${opacity}">
                    <span class="ent-row-key" style="color:${priColor(iss.priority)}">${escapeHtml(iss.key || '?')}</span>
                    <span class="ent-row-title">${escapeHtml(String(iss.summary || '').slice(0,48))}</span>
                    <span class="ent-row-state">${escapeHtml(iss.status || '')}</span>
                </div>`;
            }).join('');
            const view = String(info.view || 'my issues');
            return `<div class="scene scene-enterprise interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="enterprise"></canvas>
                <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                <div class="scene-grid"></div>
                <div class="ent-shell">
                    <div class="ent-eyebrow">jira · ${escapeHtml(view)}</div>
                    <div class="ent-hero">${issues.length} issues</div>
                    <div class="ent-list">${rowsHtml || '<div class="ent-row"><span class="ent-row-title">No issues</span></div>'}</div>
                </div>
            </div>`;
        }
        // ── Notion scene ──────────────────────────────────────────────────────
        if (core.kind === 'notion') {
            const info  = core.info || {};
            const pages = Array.isArray(info.pages) ? info.pages : [];
            const op    = String(info.op || '');
            if (op === 'notion_create' || op === 'notion_update') {
                const label = op === 'notion_create' ? 'page created' : 'page updated';
                const title = String(info.title || '').slice(0, 60) || 'untitled';
                return `<div class="scene scene-enterprise interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="enterprise"></canvas>
                    <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="ent-shell">
                        <div class="ent-eyebrow">notion · ${escapeHtml(label)}</div>
                        <div class="ent-hero">${escapeHtml(title)}</div>
                        <div class="ent-check">✓ saved</div>
                    </div>
                </div>`;
            }
            const rowsHtml = pages.slice(0, 8).map((p, i) => {
                const opacity = Math.max(0.12, 0.72 - i * 0.08);
                const icon = p.type === 'database' ? '▦' : '▢';
                return `<div class="ent-row" style="opacity:${opacity}">
                    <span class="ent-row-key">${icon}</span>
                    <span class="ent-row-title">${escapeHtml(String(p.title || '').slice(0,52))}</span>
                    <span class="ent-row-state">${escapeHtml(p.last_edited || '')}</span>
                </div>`;
            }).join('');
            const query = String(info.query || '');
            return `<div class="scene scene-enterprise interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="enterprise"></canvas>
                <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                <div class="scene-grid"></div>
                <div class="ent-shell">
                    <div class="ent-eyebrow">notion · ${query ? escapeHtml('search: ' + query) : 'workspace'}</div>
                    <div class="ent-hero">${pages.length} pages</div>
                    <div class="ent-list">${rowsHtml || '<div class="ent-row"><span class="ent-row-title">No pages found</span></div>'}</div>
                </div>
            </div>`;
        }
        // ── Asana scene ───────────────────────────────────────────────────────
        if (core.kind === 'asana') {
            const info  = core.info || {};
            const tasks = Array.isArray(info.tasks) ? info.tasks : [];
            const op    = String(info.op || '');
            if (op === 'asana_create' || op === 'asana_update') {
                const label = op === 'asana_create' ? 'task created' : 'task updated';
                const name  = String(info.name || '').slice(0, 60) || 'task';
                return `<div class="scene scene-enterprise interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="enterprise"></canvas>
                    <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="ent-shell">
                        <div class="ent-eyebrow">asana · ${escapeHtml(label)}</div>
                        <div class="ent-hero">${escapeHtml(name)}</div>
                        <div class="ent-check">✓ saved</div>
                    </div>
                </div>`;
            }
            const today = new Date().toISOString().slice(0, 10);
            const open  = tasks.filter(t => !t.completed).length;
            const rowsHtml = tasks.slice(0, 8).map((t, i) => {
                const opacity  = Math.max(0.12, 0.72 - i * 0.08);
                const check    = t.completed ? '✓' : '○';
                const overdue  = t.due_on && t.due_on < today ? ' ent-overdue' : '';
                return `<div class="ent-row${overdue}" style="opacity:${opacity}">
                    <span class="ent-row-key">${check}</span>
                    <span class="ent-row-title">${escapeHtml(String(t.name || '').slice(0,52))}</span>
                    ${t.due_on ? `<span class="ent-row-state">${escapeHtml(t.due_on)}</span>` : ''}
                </div>`;
            }).join('');
            return `<div class="scene scene-enterprise interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="enterprise"></canvas>
                <div class="scene-orb orb-a"></div><div class="scene-orb orb-c"></div>
                <div class="scene-grid"></div>
                <div class="ent-shell">
                    <div class="ent-eyebrow">asana · my tasks</div>
                    <div class="ent-hero">${open} open</div>
                    <div class="ent-list">${rowsHtml || '<div class="ent-row"><span class="ent-row-title">No tasks</span></div>'}</div>
                </div>
            </div>`;
        }
        const intent = this.humanizeIntentLabel((envelope?.raw || this.state.session.lastIntent || '').trim());
        return `
            <div class="scene scene-generic">
                <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                <div class="scene-orb orb-a"></div>
                <div class="scene-orb orb-b"></div>
                <div class="scene-grid"></div>
                <div class="scene-chip-row">
                    <div class="scene-chip">intent canvas</div>
                    <div class="scene-chip">${escapeHtml(intent.slice(0, 40))}</div>
                </div>
            </div>
        `;
    },

    buildExperienceLayer(core, envelope, execution) {
        const latest = this.latestToolResult(execution) || {};
        const cards = this.buildExperienceCards(core, envelope, latest);
        const tone = String(core.kind || 'generic');
        const cardHtml = cards.map((card) => `
            <div class="exp-card ${escapeAttr(String(card.tone || 'neutral'))}">
                <div class="exp-label">${escapeHtml(String(card.label || 'signal'))}</div>
                <div class="exp-value">${escapeHtml(String(card.value || '-'))}</div>
            </div>
        `).join('');
        return `
            <div class="experience-layer ${escapeAttr(tone)}">
                <div class="experience-title">${escapeHtml(this.buildExperienceTitle(core, envelope, latest))}</div>
                <div class="experience-grid">${cardHtml}</div>
            </div>
        `;
    },

    buildExperienceTitle(core, envelope, latest) {
        if (core.kind === 'weather') return 'Atmosphere + Forecast';
        if (core.kind === 'shopping') return 'Visual Catalog + Fit Signal';
        if (core.kind === 'webdeck') return 'Web Surface + Source Route';
        if (core.kind === 'social') return 'Signal + Social Stream';
        if (core.kind === 'banking') return 'Balance + Transaction Flow';
        if (core.kind === 'contacts') return 'Contact Graph + Reachability';
        if (core.kind === 'telephony') return 'Call Handoff + Device Bridge';
        if (core.kind === 'files') return 'Workspace Tree + Live Preview';
        if (core.kind === 'tasks') return 'Flow + Completion State';
        if (core.kind === 'expenses') return 'Spend Dynamics + Distribution';
        if (core.kind === 'notes') return 'Knowledge Fragments + Context';
        if (core.kind === 'graph') return 'Relationship Topology + Guidance';
        if (core.kind === 'document')     return 'Document + Generation Context';
        if (core.kind === 'spreadsheet')  return 'Spreadsheet + Data Structure';
        if (core.kind === 'presentation') return 'Presentation + Slide Architecture';
        if (core.kind === 'code')         return 'Code + Execution Context';
        if (core.kind === 'terminal')     return 'Terminal + Shell Environment';
        if (core.kind === 'calendar')     return 'Calendar + Time Context';
        if (core.kind === 'email')        return 'Email + Communication Layer';
        if (core.kind === 'content')      return 'Content Graph + Version History';
        if (core.kind === 'smarthome') return 'Device Grid + Ambient State';
        if (core.kind === 'travel')    return 'Itinerary + Transit Layer';
        if (core.kind === 'payments')  return 'Transfer + Balance Flow';
        if (core.kind === 'focus')     return 'Attention Mode + Block State';
        if (core.kind === 'video')         return 'Stream + Playback State';
        if (core.kind === 'food_delivery') return 'Order + Delivery Status';
        if (core.kind === 'rideshare')     return 'Ride + Driver Handoff';
        if (core.kind === 'camera')    return 'Viewfinder + Capture Mode';
        if (core.kind === 'photos')    return 'Memory Grid + Recency';
        if (core.kind === 'clock')     return 'Time + Interval State';
        if (core.kind === 'recipe')    return 'Recipe + Ingredient Map';
        if (core.kind === 'grocery')   return 'List + Completion Flow';
        if (core.kind === 'translate') return 'Source + Translation Layer';
        if (core.kind === 'book')      return 'Title + Reading Progress';
        if (core.kind === 'github')    return 'Repo + PR Activity';
        if (core.kind === 'jira')      return 'Issue Board + Priority';
        if (core.kind === 'notion')    return 'Workspace + Pages';
        if (core.kind === 'asana')     return 'Tasks + Project State';
        if (core.kind === 'enterprise') return 'Enterprise + Integrations';
        const intent = String(envelope?.surfaceIntent?.raw || '').trim();
        return intent ? `Intent Lens: ${intent.slice(0, 48)}` : (latest.message || 'Intent + State');
    },

    buildExperienceCards(core, envelope, latest) {
        const memory = this.state.memory || { tasks: [], expenses: [], notes: [] };
        if (core.kind === 'weather') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const forecast = Array.isArray(data.forecast) ? data.forecast : [];
            const precipMax = forecast.length ? Math.max(...forecast.map((x) => Number(x.precipChance || 0))) : 0;
            const windMax = forecast.length ? Math.max(...forecast.map((x) => Number(x.windMph || 0))) : Number(data.windMph || 0);
            return [
                { label: 'Condition', value: String(data.condition || core.headline || 'unknown'), tone: 'cool' },
                { label: 'Temperature', value: `${Math.round(Number(data.temperatureF || 0))}F`, tone: 'warm' },
                { label: 'Precip Peak', value: `${Math.round(precipMax)}%`, tone: 'rain' },
                { label: 'Wind Max', value: `${Math.round(windMax)} mph`, tone: 'wind' },
            ];
        }
        if (core.kind === 'shopping') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const items = Array.isArray(data.items) ? data.items : [];
            const avg = items.length
                ? items.reduce((sum, item) => sum + Number(item.priceUsd || 0), 0) / items.length
                : 0;
            const topBrand = items.length ? String(items[0].brand || 'mixed') : 'mixed';
            return [
                { label: 'Results', value: String(items.length), tone: 'accent' },
                { label: 'Category', value: String(data.category || 'products'), tone: 'neutral' },
                { label: 'Avg Price', value: formatCurrency(avg), tone: 'warm' },
                { label: 'Top Brand', value: topBrand, tone: 'cool' },
            ];
        }
        if (core.kind === 'webdeck') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const source = String(data.source || 'scaffold');
            const query = String(data.query || '').trim();
            const hasUrl = Boolean(String(data.url || '').trim());
            const items = Array.isArray(data.items) ? data.items : [];
            const host = (() => {
                const raw = String(data.url || '');
                if (!raw) return query ? 'search' : '-';
                try {
                    return new URL(raw).hostname.replace(/^www\./, '');
                } catch {
                    return raw.slice(0, 24);
                }
            })();
            return [
                { label: 'Source', value: source, tone: 'accent' },
                { label: 'Mode', value: hasUrl ? 'page' : 'search', tone: 'neutral' },
                { label: 'Target', value: host, tone: 'cool' },
                { label: 'Results', value: String(items.length || (hasUrl ? 1 : 0)), tone: 'warm' },
            ];
        }
        if (core.kind === 'social') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const op = latest.op;
            if (op === 'social_notifications') {
                const items = Array.isArray(data.items) ? data.items : [];
                return [
                    { label: 'Unread', value: String(data.unread || 0), tone: 'accent' },
                    { label: 'Total', value: String(items.length), tone: 'cool' },
                    { label: 'Source', value: 'bluesky', tone: 'neutral' },
                    { label: 'Mode', value: 'notifications', tone: 'warm' },
                ];
            }
            if (op === 'social_trending') {
                const topics = Array.isArray(data.topics) ? data.topics : [];
                return [
                    { label: 'Topics', value: String(topics.length), tone: 'accent' },
                    { label: 'Top', value: String(topics[0]?.topic || '—').slice(0, 24), tone: 'cool' },
                    { label: 'Source', value: 'bluesky', tone: 'neutral' },
                    { label: 'Mode', value: 'trending', tone: 'warm' },
                ];
            }
            if (op === 'social_profile_read') {
                return [
                    { label: 'Handle', value: String(data.handle || '—').slice(0, 24), tone: 'accent' },
                    { label: 'Followers', value: Number(data.followersCount || 0).toLocaleString(), tone: 'cool' },
                    { label: 'Posts', value: String(data.postsCount || 0), tone: 'neutral' },
                    { label: 'Source', value: 'bluesky', tone: 'warm' },
                ];
            }
            if (op === 'social_dm_send') {
                return [
                    { label: 'To', value: String(data.recipient || '—').slice(0, 24), tone: 'accent' },
                    { label: 'Delivery', value: 'sent', tone: 'cool' },
                    { label: 'ConvoId', value: String(data.convoId || '—').slice(0, 16), tone: 'neutral' },
                    { label: 'Source', value: 'bluesky', tone: 'warm' },
                ];
            }
            if (op === 'social_react') {
                return [
                    { label: 'Action', value: 'liked', tone: 'accent' },
                    { label: 'Post', value: String(data.uri || '—').slice(0, 28), tone: 'cool' },
                    { label: 'Source', value: 'bluesky', tone: 'neutral' },
                    { label: 'Status', value: 'delivered', tone: 'warm' },
                ];
            }
            if (op === 'social_comment') {
                return [
                    { label: 'Action', value: 'replied', tone: 'accent' },
                    { label: 'Post', value: String(data.uri || '—').slice(0, 28), tone: 'cool' },
                    { label: 'Source', value: 'bluesky', tone: 'neutral' },
                    { label: 'Status', value: 'delivered', tone: 'warm' },
                ];
            }
            if (op === 'social_follow') {
                return [
                    { label: 'Action', value: String(data.action || 'followed'), tone: 'accent' },
                    { label: 'Actor', value: String(data.actor || '—').slice(0, 24), tone: 'cool' },
                    { label: 'Source', value: 'bluesky', tone: 'neutral' },
                    { label: 'Status', value: 'delivered', tone: 'warm' },
                ];
            }
            const items = Array.isArray(data.items) ? data.items : [];
            return [
                { label: 'Source', value: String(data.source || 'scaffold'), tone: 'accent' },
                { label: 'Mode', value: op === 'social_message_send' ? 'send' : 'feed', tone: 'neutral' },
                { label: 'Items', value: String(items.length), tone: 'cool' },
                { label: 'Delivery', value: String(data.delivery || (op === 'social_message_send' ? 'queued' : 'n/a')), tone: 'warm' },
            ];
        }
        if (core.kind === 'banking') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const items = Array.isArray(data.items) ? data.items : [];
            const available = Number(data.available || 0);
            const ledger = Number(data.ledger || 0);
            return [
                { label: 'Available', value: formatCurrency(available), tone: 'accent' },
                { label: 'Ledger', value: formatCurrency(ledger || available), tone: 'cool' },
                { label: 'Source', value: String(data.source || 'scaffold'), tone: 'neutral' },
                { label: 'Rows', value: String(items.length || (latest.op === 'banking_balance_read' ? 1 : 0)), tone: 'warm' },
            ];
        }
        if (core.kind === 'contacts') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const items = Array.isArray(data.items) ? data.items : [];
            return [
                { label: 'Source', value: String(data.source || 'scaffold'), tone: 'accent' },
                { label: 'Contacts', value: String(items.length), tone: 'cool' },
                { label: 'Query', value: String(data.query || 'all'), tone: 'neutral' },
                { label: 'Mode', value: 'lookup', tone: 'warm' },
            ];
        }
        if (core.kind === 'telephony') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            return [
                { label: 'Mode', value: String(data.mode || 'status'), tone: 'accent' },
                { label: 'Target', value: String(data.target || '-'), tone: 'cool' },
                { label: 'Contact', value: String(data.contactName || '-'), tone: 'neutral' },
                { label: 'Bridge', value: 'mobile handoff', tone: 'warm' },
            ];
        }
        if (core.kind === 'files') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const items = Array.isArray(data.items) ? data.items : [];
            return [
                { label: 'Path', value: String(data.path || '.'), tone: 'accent' },
                { label: 'Entries', value: String(items.length), tone: 'cool' },
                { label: 'Lines', value: String(Number(data.lineCount || 0)), tone: 'neutral' },
                { label: 'Mode', value: latest.op === 'read_file' ? 'file' : 'list', tone: 'warm' },
            ];
        }
        if (core.kind === 'tasks') {
            const tasks = Array.isArray(memory.tasks) ? memory.tasks : [];
            const open = tasks.filter((x) => !x.done).length;
            const done = tasks.filter((x) => x.done).length;
            return [
                { label: 'Open', value: String(open), tone: 'accent' },
                { label: 'Done', value: String(done), tone: 'cool' },
                { label: 'Total', value: String(tasks.length), tone: 'neutral' },
                { label: 'Focus', value: open > 0 ? 'execution' : 'planning', tone: 'warm' },
            ];
        }
        if (core.kind === 'expenses') {
            const expenses = Array.isArray(memory.expenses) ? memory.expenses : [];
            const total = expenses.reduce((sum, item) => sum + Number(item.amount || 0), 0);
            const categories = new Set(expenses.map((item) => String(item.category || 'misc'))).size;
            const avg = expenses.length ? total / expenses.length : 0;
            return [
                { label: 'Total', value: formatCurrency(total), tone: 'warm' },
                { label: 'Entries', value: String(expenses.length), tone: 'neutral' },
                { label: 'Categories', value: String(categories), tone: 'accent' },
                { label: 'Average', value: formatCurrency(avg), tone: 'cool' },
            ];
        }
        if (core.kind === 'notes') {
            const notes = Array.isArray(memory.notes) ? memory.notes : [];
            const latestText = notes.length ? String(notes[notes.length - 1].text || '') : '';
            return [
                { label: 'Notes', value: String(notes.length), tone: 'accent' },
                { label: 'Latest', value: latestText.slice(0, 16) || 'none', tone: 'neutral' },
                { label: 'Mode', value: 'synthesis', tone: 'cool' },
                { label: 'Context', value: 'active', tone: 'warm' },
            ];
        }
        if (core.kind === 'graph') {
            return [
                { label: 'Topology', value: 'connected', tone: 'accent' },
                { label: 'Drift', value: 'stable', tone: 'cool' },
                { label: 'Edges', value: 'live', tone: 'neutral' },
                { label: 'State', value: 'mapped', tone: 'warm' },
            ];
        }
        if (core.kind === 'reference') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const op   = latest.op;
            if (op === 'dict_define') {
                const meanings = Array.isArray(data.meanings) ? data.meanings : [];
                return [
                    { label: 'Word', value: String(data.word || '—'), tone: 'accent' },
                    { label: 'Phonetic', value: String(data.phonetic || '—'), tone: 'cool' },
                    { label: 'Senses', value: String(meanings.length), tone: 'neutral' },
                    { label: 'Source', value: 'wiktionary', tone: 'warm' },
                ];
            }
            if (op === 'dict_etymology') {
                return [
                    { label: 'Word', value: String(data.word || '—'), tone: 'accent' },
                    { label: 'Origin', value: String(data.origin || '—').slice(0, 28), tone: 'cool' },
                    { label: 'Phonetic', value: String(data.phonetic || '—'), tone: 'neutral' },
                    { label: 'Source', value: 'wiktionary', tone: 'warm' },
                ];
            }
            if (op === 'dict_thesaurus') {
                const syns = Array.isArray(data.synonyms) ? data.synonyms : [];
                return [
                    { label: 'Word', value: String(data.word || '—'), tone: 'accent' },
                    { label: 'Synonyms', value: String(syns.length), tone: 'cool' },
                    { label: 'Top', value: syns[0] || '—', tone: 'neutral' },
                    { label: 'Source', value: 'datamuse', tone: 'warm' },
                ];
            }
            if (op === 'dict_wikipedia') {
                return [
                    { label: 'Article', value: String(data.title || '—').slice(0, 24), tone: 'accent' },
                    { label: 'Desc', value: String(data.description || '—').slice(0, 28), tone: 'cool' },
                    { label: 'Source', value: 'wikipedia', tone: 'neutral' },
                    { label: 'Mode', value: 'summary', tone: 'warm' },
                ];
            }
            if (op === 'currency_rates') {
                const major = (data.major && typeof data.major === 'object') ? data.major : {};
                const eur = major.EUR ? Number(major.EUR).toFixed(3) : '—';
                const gbp = major.GBP ? Number(major.GBP).toFixed(3) : '—';
                return [
                    { label: 'Base', value: String(data.base || 'USD'), tone: 'accent' },
                    { label: 'EUR', value: eur, tone: 'cool' },
                    { label: 'GBP', value: gbp, tone: 'neutral' },
                    { label: 'Source', value: 'open.er-api', tone: 'warm' },
                ];
            }
            if (op === 'currency_convert') {
                return [
                    { label: 'Result', value: `${Number(data.result||0).toLocaleString(undefined,{maximumFractionDigits:2})} ${data.to||''}`, tone: 'accent' },
                    { label: 'From', value: `${data.amount||1} ${data.from||''}`, tone: 'cool' },
                    { label: 'Rate', value: String(Number(data.rate||0).toFixed(4)), tone: 'neutral' },
                    { label: 'Source', value: 'open.er-api', tone: 'warm' },
                ];
            }
            if (op === 'unit_convert') {
                return [
                    { label: 'Result', value: `${data.result||0} ${data.to||''}`, tone: 'accent' },
                    { label: 'From', value: `${data.value||1} ${data.from||''}`, tone: 'cool' },
                    { label: 'Category', value: String(data.category || 'units'), tone: 'neutral' },
                    { label: 'Mode', value: 'exact', tone: 'warm' },
                ];
            }
            return [
                { label: 'Query', value: String(data.word || data.query || data.title || '—').slice(0,24), tone: 'accent' },
                { label: 'Source', value: 'reference', tone: 'cool' },
                { label: 'Op', value: op, tone: 'neutral' },
                { label: 'Status', value: 'ready', tone: 'warm' },
            ];
        }
        if (core.kind === 'health') {
            const data    = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const metrics = (data.metrics && typeof data.metrics === 'object') ? data.metrics : {};
            const op      = latest.op;
            const hero    = String(data.hero || '');
            const sumLine = String(data.summary_line || '');
            if (op === 'health_heart_rate') {
                return [
                    { label: 'Heart Rate', value: `${metrics.heart_rate || 0} bpm`, tone: 'accent' },
                    { label: 'Resting', value: `${metrics.heart_rate_resting || 0} bpm`, tone: 'cool' },
                    { label: 'HRV', value: `${metrics.hrv || 0} ms`, tone: 'neutral' },
                    { label: 'Source', value: 'scaffold', tone: 'warm' },
                ];
            }
            if (op === 'health_sleep') {
                return [
                    { label: 'Sleep', value: `${metrics.sleep_hours || 0}h`, tone: 'accent' },
                    { label: 'Calories', value: `${metrics.calories || 0}`, tone: 'cool' },
                    { label: 'Active', value: `${metrics.active_min || 0} min`, tone: 'neutral' },
                    { label: 'Source', value: 'scaffold', tone: 'warm' },
                ];
            }
            if (op === 'health_hrv') {
                return [
                    { label: 'HRV', value: `${metrics.hrv || 0} ms`, tone: 'accent' },
                    { label: 'HR', value: `${metrics.heart_rate || 0} bpm`, tone: 'cool' },
                    { label: 'Resting', value: `${metrics.heart_rate_resting || 0} bpm`, tone: 'neutral' },
                    { label: 'Source', value: 'scaffold', tone: 'warm' },
                ];
            }
            return [
                { label: 'Metric', value: hero || 'activity', tone: 'accent' },
                { label: 'Goal', value: sumLine || '—', tone: 'cool' },
                { label: 'Streak', value: `${metrics.streak_days || 0} days`, tone: 'neutral' },
                { label: 'Source', value: 'scaffold', tone: 'warm' },
            ];
        }
        if (core.kind === 'clock') {
            const data = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const op   = latest.op;
            if (op === 'clock_world') {
                const clocks = Array.isArray(data.clocks) ? data.clocks : [];
                return [
                    { label: 'Zones', value: String(clocks.length), tone: 'accent' },
                    { label: 'Local', value: clocks[0]?.time || '—', tone: 'cool' },
                    { label: 'UTC+0', value: clocks.find(c => c.zone?.includes('London'))?.time || '—', tone: 'neutral' },
                    { label: 'Mode', value: 'world', tone: 'warm' },
                ];
            }
            if (op === 'clock_bedtime') {
                const beds = Array.isArray(data.bedtimes) ? data.bedtimes : [];
                const best = beds[1] || beds[0] || {};
                return [
                    { label: 'Bedtime', value: best.time || '—', tone: 'accent' },
                    { label: 'Cycles', value: String(best.cycles || 5), tone: 'cool' },
                    { label: 'Duration', value: `${best.hours || 7.5}h`, tone: 'neutral' },
                    { label: 'Wake', value: data.wake_time || '—', tone: 'warm' },
                ];
            }
            if (['date_age','date_countdown','date_day_of','date_days_until'].includes(op)) {
                const delta = data.delta_days != null ? Number(data.delta_days) : null;
                return [
                    { label: 'Result', value: String(data.day_name || (data.age_years != null ? `${data.age_years}y` : (delta != null ? `${Math.abs(delta)}d` : '—'))), tone: 'accent' },
                    { label: 'Date', value: String(data.date || data.target || '—'), tone: 'cool' },
                    { label: 'Op', value: op.replace('date_',''), tone: 'neutral' },
                    { label: 'Mode', value: 'calc', tone: 'warm' },
                ];
            }
            return [
                { label: 'Mode', value: String(data.mode || 'timer'), tone: 'accent' },
                { label: 'State', value: data.running ? 'running' : 'ready', tone: 'cool' },
                { label: 'Label', value: String(data.label || '—'), tone: 'neutral' },
                { label: 'Source', value: 'local', tone: 'warm' },
            ];
        }
        if (core.kind === 'github') {
            const data  = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const items = Array.isArray(data.items) ? data.items : [];
            const op    = latest.op;
            return [
                { label: op === 'github_issue_create' ? 'Number' : 'Open',
                  value: op === 'github_issue_create' ? `#${data.number || '?'}` : String(items.filter(i => i.state === 'open').length),
                  tone: 'accent' },
                { label: 'Repo',   value: String(data.repo || '—').slice(0, 24),    tone: 'cool'    },
                { label: 'Source', value: String(data.source || 'scaffold'),         tone: 'neutral' },
                { label: 'Mode',   value: String(data.kind || op.replace('github_','')), tone: 'warm' },
            ];
        }
        if (core.kind === 'jira') {
            const data   = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const issues = Array.isArray(data.issues) ? data.issues : [];
            const op     = latest.op;
            if (op === 'jira_create') {
                return [
                    { label: 'Key',     value: String(data.key || '?'),                  tone: 'accent'  },
                    { label: 'Summary', value: String(data.summary || '—').slice(0, 24), tone: 'cool'    },
                    { label: 'Source',  value: 'jira',                                   tone: 'neutral' },
                    { label: 'Status',  value: 'created',                                tone: 'warm'    },
                ];
            }
            const critical = issues.filter(i => (i.priority || '').toLowerCase() === 'critical').length;
            return [
                { label: 'Issues',   value: String(issues.length),                                  tone: 'accent'                    },
                { label: 'Critical', value: String(critical),                                        tone: critical > 0 ? 'warm' : 'cool' },
                { label: 'Source',   value: String(data.source || 'scaffold'),                       tone: 'neutral'                   },
                { label: 'View',     value: String(data.view || 'my issues').replace(/_/g, ' '),    tone: 'cool'                      },
            ];
        }
        if (core.kind === 'notion') {
            const data  = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const pages = Array.isArray(data.pages) ? data.pages : [];
            const op    = latest.op;
            const dbs   = pages.filter(p => p.type === 'database').length;
            return [
                { label: op === 'notion_create' ? 'Status' : 'Pages',
                  value: op === 'notion_create' ? 'created' : String(pages.length),       tone: 'accent'  },
                { label: op === 'notion_create' ? 'Title' : 'Databases',
                  value: op === 'notion_create' ? String(data.title || '—').slice(0, 20) : String(dbs), tone: 'cool' },
                { label: 'Source', value: String(data.source || 'scaffold'),              tone: 'neutral' },
                { label: 'Mode',   value: data.query ? 'search' : 'browse',               tone: 'warm'    },
            ];
        }
        if (core.kind === 'asana') {
            const data  = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const tasks = Array.isArray(data.tasks) ? data.tasks : [];
            const op    = latest.op;
            const open  = tasks.filter(t => !t.completed).length;
            const today = new Date().toISOString().slice(0, 10);
            const overdue = tasks.filter(t => t.due_on && t.due_on < today).length;
            return [
                { label: op === 'asana_create' ? 'Status' : 'Open',
                  value: op === 'asana_create' ? 'created' : String(open),               tone: 'accent'                       },
                { label: op === 'asana_create' ? 'Task' : 'Total',
                  value: op === 'asana_create' ? String(data.name || '—').slice(0, 20) : String(tasks.length), tone: 'cool' },
                { label: 'Overdue', value: String(overdue), tone: overdue > 0 ? 'warm' : 'neutral' },
                { label: 'Source',  value: String(data.source || 'scaffold'),             tone: 'cool'                         },
            ];
        }
        return [
            { label: 'Intent', value: String(envelope?.taskIntent?.operation || 'read'), tone: 'accent' },
            { label: 'Session', value: String(this.state.session.sessionId || '-').slice(0, 8), tone: 'neutral' },
            { label: 'Sync', value: String(this.state.session.syncTransport || 'idle'), tone: 'cool' },
            { label: 'Status', value: latest.ok === false ? 'needs input' : 'ready', tone: 'warm' },
        ];
    },

    teardownSceneGraphics() {
        if (this._sceneAnimFrame) {
            cancelAnimationFrame(this._sceneAnimFrame);
            this._sceneAnimFrame = null;
        }
        this._sceneRenderer = null;
    },

    activateSceneGraphics() {
        const canvas = this.container.querySelector('.scene-canvas');
        if (!canvas) return;
        const scene = String(canvas.dataset.scene || '').trim();
        if (scene === 'weather') {
            this._sceneRenderer = this.makeWeatherRenderer(canvas);
        } else if (scene === 'weather-7day') {
            this._sceneRenderer = this.makeWeather7DayRenderer(canvas);
        } else if (scene === 'shopping') {
            this._sceneRenderer = this.makeShoppingRenderer(canvas);
        } else if (scene === 'tasks') {
            this._sceneRenderer = this.makeTasksRenderer(canvas);
        } else if (scene === 'expenses') {
            this._sceneRenderer = this.makeExpensesRenderer(canvas);
        } else if (scene === 'notes') {
            this._sceneRenderer = this.makeNotesRenderer(canvas);
        } else if (scene === 'connections') {
            this._sceneRenderer = this.makeConnectionsRenderer(canvas);
        } else if (scene === 'graph') {
            this._sceneRenderer = this.makeGraphRenderer(canvas);
        } else if (scene === 'webdeck') {
            this._sceneRenderer = this.makeWebdeckRenderer(canvas);
        } else if (scene === 'mcp') {
            this._sceneRenderer = this.makeMcpRenderer(canvas);
        } else if (scene === 'sports') {
            this._sceneRenderer = this.makeSportsRenderer(canvas);
        } else if (scene === 'phone') {
            this._sceneRenderer = this.makePhoneRenderer(canvas);
        } else if (scene === 'plan') {
            this._sceneRenderer = this.makePlanRenderer(canvas);
        } else if (['document','spreadsheet','presentation','code','terminal','calendar','email','content'].includes(scene)) {
            this._sceneRenderer = this.makeComputerRenderer(canvas);
        } else if (scene === 'music') {
            this._sceneRenderer = this.makeMusicRenderer(canvas);
        } else if (scene === 'smarthome') {
            this._sceneRenderer = this.makeSmarthomeRenderer(canvas);
        } else if (scene === 'health') {
            this._sceneRenderer = this.makeHealthRenderer(canvas);
        } else if (scene === 'messaging') {
            this._sceneRenderer = this.makeMessagingRenderer(canvas);
        } else if (scene === 'travel') {
            this._sceneRenderer = this.makeTravelRenderer(canvas);
        } else if (scene === 'payments') {
            this._sceneRenderer = this.makePaymentsRenderer(canvas);
        } else if (scene === 'network') {
            this._sceneRenderer = this.makeNetworkRenderer(canvas);
        } else if (scene === 'banking') {
            this._sceneRenderer = this.makeBankingRenderer(canvas);
        } else if (scene === 'contacts') {
            this._sceneRenderer = this.makeContactsRenderer(canvas);
        } else if (scene === 'location') {
            this._sceneRenderer = this.makeLocationRenderer(canvas);
        } else if (scene === 'social') {
            this._sceneRenderer = this.makeSocialRenderer(canvas);
        } else if (scene === 'telephony') {
            this._sceneRenderer = this.makeTelephonyRenderer(canvas);
        } else if (scene === 'reminders') {
            this._sceneRenderer = this.makeRemindersRenderer(canvas);
        } else if (scene === 'finance') {
            this._sceneRenderer = this.makeFinanceRenderer(canvas);
        } else if (scene === 'gaming') {
            this._sceneRenderer = this.makeGamingRenderer(canvas);
        } else if (scene === 'arvr') {
            this._sceneRenderer = this.makeArVrRenderer(canvas);
        } else if (scene === 'dating') {
            this._sceneRenderer = this.makeDatingRenderer(canvas);
        } else if (scene === 'focus') {
            this._sceneRenderer = this.makeFocusRenderer(canvas);
        } else if (['notifications','handoff','wallet','vpn','dictionary','password',
                    'app','reading','date','screen','print','backup',
                    'accessibility','shortcuts','currency','phone','camera',
                    'photos','food_delivery','rideshare','video','alarm','clock',
                    'podcast','recipe','grocery','translate','book',
                    'enterprise','github','jira','notion','asana'].includes(scene)) {
            this._sceneRenderer = this.makeComputerRenderer(canvas);
        } else {
            this._sceneRenderer = this.makeGenericRenderer(canvas);
        }
        let lastFrameAt = 0;
        const loop = (ts) => {
            if (typeof this._sceneRenderer === 'function') {
                const mobile = this.isMobileViewport();
                const hidden = document.visibilityState !== 'visible';
                const targetFps = hidden ? 8 : (mobile ? 30 : 60);
                const minDelta = 1000 / targetFps;
                const now = Number(ts || performance.now());
                if (!lastFrameAt || (now - lastFrameAt) >= minDelta) {
                    this._sceneRenderer();
                    lastFrameAt = now;
                }
                this._sceneAnimFrame = requestAnimationFrame(loop);
            }
        };
        this._sceneAnimFrame = requestAnimationFrame(loop);
    },

    isMobileViewport() {
        return Boolean(
            (window.matchMedia && window.matchMedia('(pointer: coarse)').matches)
            || window.innerWidth <= 900
        );
    },

    fitCanvas(canvas) {
        const dpr = Math.max(1, window.devicePixelRatio || 1);
        const rect = canvas.getBoundingClientRect();
        const w = Math.max(1, Math.round(rect.width));
        const h = Math.max(1, Math.round(rect.height));
        const rw = Math.round(w * dpr);
        const rh = Math.round(h * dpr);
        if (canvas.width !== rw || canvas.height !== rh) {
            canvas.width = rw;
            canvas.height = rh;
        }
        return { dpr, w, h };
    },

    makeSportsRenderer(canvas) {
        let t = 0;
        const parseHex = (hex) => {
            const h = String(hex || '').trim();
            if (!/^[0-9a-fA-F]{6}$/.test(h)) return null;
            return [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16)];
        };
        const away = parseHex(canvas.dataset.away) || [30, 30, 60];
        const home = parseHex(canvas.dataset.home) || [60, 15, 15];

        // Two spotlights — slow independent oscillation
        const spots = [
            { side: 0, phase: 0,    speed: 0.0028, swing: 0.18 },
            { side: 1, phase: 1.9,  speed: 0.0021, swing: 0.18 },
        ];

        // Load venue photo asynchronously — draw as base layer once ready
        let venueImg = null;
        const venueUrl = String(canvas.dataset.venueImg || '').trim();
        if (venueUrl) {
            const img = new Image();
            img.onload = () => { venueImg = img; };
            img.onerror = () => {};  // silent fallback to canvas-only atmosphere
            img.src = venueUrl;
        }

        // Helper: draw venue photo cover-cropped onto canvas
        const drawVenuePhoto = (ctx, w, h) => {
            if (!venueImg || !venueImg.naturalWidth) return false;
            const iw = venueImg.naturalWidth, ih = venueImg.naturalHeight;
            const ir = iw / ih, cr = w / h;
            let sx = 0, sy = 0, sw = iw, sh = ih;
            if (ir > cr) { sw = Math.round(ih * cr); sx = Math.round((iw - sw) / 2); }
            else         { sh = Math.round(iw / cr); sy = Math.round((ih - sh) * 0.3); }
            ctx.drawImage(venueImg, sx, sy, sw, sh, 0, 0, w, h);
            return true;
        };

        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.01;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

            // ── Layer 1: Stadium photo or deep fill ──
            ctx.fillStyle = 'rgba(4,4,12,1)';
            ctx.fillRect(0, 0, w, h);
            if (venueImg) {
                ctx.save();
                ctx.globalAlpha = 0.32;
                drawVenuePhoto(ctx, w, h);
                ctx.restore();
                // Darken heavily so content stays readable
                ctx.fillStyle = 'rgba(4,4,12,0.60)';
                ctx.fillRect(0, 0, w, h);
            }

            // ── Layer 2: Team color radial blooms ──
            const drawBloom = (rgb, cx, cy, r, alpha) => {
                const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
                g.addColorStop(0, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha})`);
                g.addColorStop(1, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0)`);
                ctx.fillStyle = g;
                ctx.fillRect(0, 0, w, h);
            };
            // Photo present → subtler bloom (photo carries the color); no photo → stronger
            const bloomStr = venueImg ? 0.28 : 0.42;
            drawBloom(away, w * 0.20, h * 0.55, w * 0.65, bloomStr);
            drawBloom(home, w * 0.80, h * 0.55, w * 0.65, bloomStr);

            // ── Layer 3: Vertical center fade (photo blur seam) ──
            const seam = ctx.createLinearGradient(w * 0.44, 0, w * 0.56, 0);
            seam.addColorStop(0, 'rgba(4,4,12,0)');
            seam.addColorStop(0.5, venueImg ? 'rgba(4,4,12,0.18)' : 'rgba(255,255,255,0.04)');
            seam.addColorStop(1, 'rgba(4,4,12,0)');
            ctx.fillStyle = seam;
            ctx.fillRect(0, 0, w, h);

            // ── Layer 4: Animated stadium spotlights ──
            ctx.save();
            for (const sp of spots) {
                sp.phase += sp.speed;
                const baseX = sp.side === 0 ? w * 0.25 : w * 0.75;
                const sweepX = baseX + Math.sin(sp.phase) * w * sp.swing;
                const rgb = sp.side === 0 ? away : home;
                const apexX = sweepX, apexY = -h * 0.06;
                const coneW = w * 0.20, coneH = h * 1.12;
                const footX = apexX + Math.sin(sp.phase * 0.3) * w * 0.03;
                // Cone body
                const spotGrad = ctx.createRadialGradient(apexX, apexY, 0, footX, apexY + coneH, coneH);
                spotGrad.addColorStop(0, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${venueImg ? 0.22 : 0.18})`);
                spotGrad.addColorStop(0.45, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.07)`);
                spotGrad.addColorStop(1, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0)`);
                ctx.beginPath();
                ctx.moveTo(apexX, apexY);
                ctx.lineTo(footX - coneW / 2, apexY + coneH);
                ctx.lineTo(footX + coneW / 2, apexY + coneH);
                ctx.closePath();
                ctx.fillStyle = spotGrad;
                ctx.fill();
                // Apex hotspot
                const dot = ctx.createRadialGradient(apexX, apexY + 3, 0, apexX, apexY + 3, 16);
                dot.addColorStop(0, 'rgba(255,255,255,0.28)');
                dot.addColorStop(1, 'rgba(255,255,255,0)');
                ctx.fillStyle = dot;
                ctx.beginPath();
                ctx.arc(apexX, apexY + 3, 16, 0, Math.PI * 2);
                ctx.fill();
            }
            ctx.restore();

            // ── Layer 5: Subtle vignette to keep edges dark ──
            const vig = ctx.createRadialGradient(w/2, h*0.45, h*0.1, w/2, h*0.45, w*0.82);
            vig.addColorStop(0, 'rgba(0,0,0,0)');
            vig.addColorStop(1, 'rgba(0,0,0,0.55)');
            ctx.fillStyle = vig;
            ctx.fillRect(0, 0, w, h);
        };
    },
    makePhoneRenderer(canvas) {
        const ctx = canvas.getContext('2d');
        let frame = 0;
        const draw = () => {
            const w = canvas.offsetWidth; const h = canvas.offsetHeight;
            if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
            ctx.clearRect(0, 0, w, h);
            // Radial pulse — warm ember tones
            frame++;
            const sceneEl = canvas.closest('[data-call-state]');
            const state = String(sceneEl ? sceneEl.dataset.callState : 'ringing');
            const baseAlpha = state === 'active' ? 0.18 : state === 'held' ? 0.09 : 0.13;
            const pulse = Math.sin(frame * (state === 'ringing' ? 0.07 : 0.03)) * 0.5 + 0.5;
            const r1 = w * (0.32 + pulse * 0.06);
            const grad = ctx.createRadialGradient(w * 0.5, h * 0.38, 0, w * 0.5, h * 0.38, r1);
            grad.addColorStop(0, `rgba(255,120,60,${(baseAlpha + pulse * 0.06).toFixed(3)})`);
            grad.addColorStop(0.5, `rgba(200,80,40,${(baseAlpha * 0.5).toFixed(3)})`);
            grad.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = grad;
            ctx.fillRect(0, 0, w, h);
            // Thin ring
            ctx.beginPath();
            ctx.arc(w * 0.5, h * 0.38, r1 * (0.85 + pulse * 0.04), 0, Math.PI * 2);
            ctx.strokeStyle = `rgba(255,160,100,${(0.10 + pulse * 0.08).toFixed(3)})`;
            ctx.lineWidth = 1;
            ctx.stroke();
        };
        let raf;
        const loop = () => { draw(); raf = requestAnimationFrame(loop); };
        loop();
        return { stop: () => cancelAnimationFrame(raf) };
    },

    makeComputerRenderer(canvas) {
        const scene = String(canvas.dataset.scene || 'document');
        const THEMES = {
            document:     [96,  128, 196],
            spreadsheet:  [48,  160, 120],
            presentation: [140,  96, 220],
            code:         [64,  180, 200],
            terminal:     [48,  200,  80],
            calendar:     [80,  160, 240],
            email:        [100, 140, 200],
            content:      [120,  80, 240],
        };
        const [r, g, b] = THEMES[scene] || THEMES.document;
        const nodes = scene === 'content' ? Array.from({ length: 18 }, (_, i) => ({
            x: ((i * 1973 + 83) % 9973) / 9973, y: ((i * 2741 + 17) % 9871) / 9871,
            vx: (((i * 137) % 100) - 50) * 0.00018, vy: (((i * 251) % 100) - 50) * 0.00018,
        })) : [];
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.008;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);
            // Base dark gradient
            const bg = ctx.createLinearGradient(0, 0, w * 0.3, h);
            bg.addColorStop(0, `rgba(${r * 0.08 | 0}, ${g * 0.08 | 0}, ${b * 0.12 | 0}, 1)`);
            bg.addColorStop(1, `rgba(6, 8, 14, 1)`);
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Scene-specific texture
            if (scene === 'document') {
                ctx.strokeStyle = `rgba(${r},${g},${b},0.04)`;
                ctx.lineWidth = 1;
                for (let y = 22; y < h; y += 22) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
                ctx.strokeStyle = `rgba(${r},${g},${b},0.07)`;
                ctx.beginPath(); ctx.moveTo(48, 0); ctx.lineTo(48, h); ctx.stroke();
            } else if (scene === 'spreadsheet') {
                ctx.strokeStyle = `rgba(${r},${g},${b},0.06)`;
                ctx.lineWidth = 0.5;
                for (let x = 0; x < w; x += 80) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
                for (let y = 0; y < h; y += 24) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
            } else if (scene === 'presentation') {
                const cx = w * 0.6, cy = h * 0.45;
                const pulse = 0.04 + Math.sin(t * 0.5) * 0.015;
                const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, w * 0.4);
                glow.addColorStop(0, `rgba(${r},${g},${b},${pulse * 2})`);
                glow.addColorStop(1, `rgba(${r},${g},${b},0)`);
                ctx.fillStyle = glow; ctx.fillRect(0, 0, w, h);
                ctx.strokeStyle = `rgba(${r},${g},${b},0.1)`;
                ctx.lineWidth = 1;
                ctx.strokeRect(cx - w * 0.28, cy - h * 0.2, w * 0.56, h * 0.4);
            } else if (scene === 'code') {
                const bands = [0.15, 0.32, 0.50, 0.67, 0.84];
                bands.forEach((yf, i) => {
                    const a = 0.02 + Math.sin(t * 0.35 + i) * 0.008;
                    ctx.fillStyle = `rgba(${r},${g},${b},${a})`;
                    ctx.fillRect(0, h * yf, w * (0.38 + ((i * 37) % 40) / 100), 12);
                });
                ctx.strokeStyle = 'rgba(0,0,0,0.07)';
                ctx.lineWidth = 1;
                for (let y = 0; y < h; y += 3) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
            } else if (scene === 'terminal') {
                ctx.strokeStyle = 'rgba(0,0,0,0.16)';
                ctx.lineWidth = 1;
                for (let y = 0; y < h; y += 3) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
                const glow = ctx.createLinearGradient(0, h * 0.65, 0, h);
                glow.addColorStop(0, `rgba(${r},${g},${b},0)`);
                glow.addColorStop(1, `rgba(${r},${g},${b},0.07)`);
                ctx.fillStyle = glow; ctx.fillRect(0, h * 0.65, w, h * 0.35);
            } else if (scene === 'calendar') {
                const hour = new Date().getHours();
                const dp = Math.max(0, Math.min(1, (hour - 6) / 16));
                const glow = ctx.createRadialGradient(w * 0.5, h, 0, w * 0.5, h, w * 0.75);
                glow.addColorStop(0, `rgba(${80 + dp * 60 | 0},${120 + dp * 30 | 0},${240 - dp * 80 | 0},0.1)`);
                glow.addColorStop(1, `rgba(${r},${g},${b},0)`);
                ctx.fillStyle = glow; ctx.fillRect(0, 0, w, h);
                ctx.strokeStyle = `rgba(${r},${g},${b},0.05)`;
                ctx.lineWidth = 0.5;
                for (let i = 0; i <= 24; i++) {
                    const x = (i / 24) * w;
                    ctx.beginPath(); ctx.moveTo(x, h * 0.88); ctx.lineTo(x, h); ctx.stroke();
                }
            } else if (scene === 'email') {
                ctx.strokeStyle = `rgba(${r},${g},${b},0.05)`;
                ctx.lineWidth = 0.5;
                for (let y = 48; y < h; y += 48) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
            } else if (scene === 'content') {
                nodes.forEach((n) => {
                    n.x += n.vx; n.y += n.vy;
                    if (n.x < 0) n.x = 1; if (n.x > 1) n.x = 0;
                    if (n.y < 0) n.y = 1; if (n.y > 1) n.y = 0;
                });
                ctx.strokeStyle = `rgba(${r},${g},${b},0.08)`;
                ctx.lineWidth = 0.5;
                for (let i = 0; i < nodes.length; i++) {
                    for (let j = i + 1; j < nodes.length; j++) {
                        const dx = (nodes[i].x - nodes[j].x) * w, dy = (nodes[i].y - nodes[j].y) * h;
                        if (Math.sqrt(dx * dx + dy * dy) < w * 0.22) {
                            ctx.beginPath(); ctx.moveTo(nodes[i].x * w, nodes[i].y * h); ctx.lineTo(nodes[j].x * w, nodes[j].y * h); ctx.stroke();
                        }
                    }
                }
                nodes.forEach((n, i) => {
                    const a = 0.28 + Math.sin(t * 0.8 + i * 0.5) * 0.14;
                    ctx.fillStyle = `rgba(${r},${g},${b},${a})`;
                    ctx.beginPath(); ctx.arc(n.x * w, n.y * h, 2.5, 0, Math.PI * 2); ctx.fill();
                });
            }
            // Subtle corner accent
            const corner = ctx.createRadialGradient(0, 0, 0, 0, 0, w * 0.38);
            corner.addColorStop(0, `rgba(${r},${g},${b},0.06)`);
            corner.addColorStop(1, `rgba(${r},${g},${b},0)`);
            ctx.fillStyle = corner; ctx.fillRect(0, 0, w, h);
        };
    },

    makeGenericRenderer(canvas) {
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.008;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);
            const grad = ctx.createLinearGradient(0, 0, 0, h);
            grad.addColorStop(0, 'rgba(24, 118, 100, 0.12)');
            grad.addColorStop(1, 'rgba(44, 91, 170, 0.04)');
            ctx.fillStyle = grad;
            ctx.fillRect(0, 0, w, h);
            ctx.strokeStyle = 'rgba(13, 26, 42, 0.1)';
            ctx.lineWidth = 1;
            for (let x = 0; x <= w; x += 32) {
                const yOffset = Math.sin(t + x * 0.01) * 3;
                ctx.beginPath();
                ctx.moveTo(x, 0 + yOffset);
                ctx.lineTo(x, h + yOffset);
                ctx.stroke();
            }
        };
    },

    makeMcpRenderer(canvas) {
        // Ambient canvas for MCP surfaces — deep teal/indigo field with
        // floating connection nodes and pulse rings suggesting network/API integration.
        let t = 0;
        const nodes = Array.from({ length: 18 }, (_, i) => ({
            x: (i * 0.618033) % 1,
            y: (i * 0.381966) % 1,
            r: 2 + (i % 4),
            phase: i * 0.42,
            speed: 0.003 + (i % 5) * 0.0008,
        }));
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.012;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            // Deep background
            const bg = ctx.createLinearGradient(0, 0, w, h);
            bg.addColorStop(0, '#060d1a');
            bg.addColorStop(0.5, '#0a1628');
            bg.addColorStop(1, '#04111f');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Pulse rings from center-left (suggesting an origin point)
            const ox = w * 0.18, oy = h * 0.5;
            for (let ring = 0; ring < 4; ring++) {
                const prog = ((t * 0.4 + ring * 0.25) % 1);
                const radius = prog * Math.min(w, h) * 0.55;
                const alpha = (1 - prog) * 0.18;
                ctx.beginPath();
                ctx.arc(ox, oy, radius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(56, 189, 248, ${alpha})`;
                ctx.lineWidth = 1.5;
                ctx.stroke();
            }
            // Connection lines between nodes
            ctx.lineWidth = 0.6;
            for (let i = 0; i < nodes.length; i++) {
                for (let j = i + 1; j < nodes.length; j++) {
                    const nx = nodes[i].x * w, ny = nodes[i].y * h;
                    const mx = nodes[j].x * w, my = nodes[j].y * h;
                    const dist = Math.hypot(nx - mx, ny - my);
                    if (dist < w * 0.22) {
                        const alpha = (1 - dist / (w * 0.22)) * 0.12;
                        ctx.strokeStyle = `rgba(99, 179, 237, ${alpha})`;
                        ctx.beginPath();
                        ctx.moveTo(nx, ny);
                        ctx.lineTo(mx, my);
                        ctx.stroke();
                    }
                }
            }
            // Nodes with subtle pulse
            nodes.forEach((n, i) => {
                const nx = n.x * w, ny = n.y * h;
                const pulse = 0.7 + 0.3 * Math.sin(t * n.speed * 100 + n.phase);
                const grd = ctx.createRadialGradient(nx, ny, 0, nx, ny, n.r * 3 * pulse);
                grd.addColorStop(0, 'rgba(56, 189, 248, 0.55)');
                grd.addColorStop(1, 'rgba(56, 189, 248, 0)');
                ctx.fillStyle = grd;
                ctx.beginPath();
                ctx.arc(nx, ny, n.r * 3 * pulse, 0, Math.PI * 2);
                ctx.fill();
                ctx.fillStyle = i % 3 === 0 ? 'rgba(186, 230, 253, 0.9)' : 'rgba(56, 189, 248, 0.7)';
                ctx.beginPath();
                ctx.arc(nx, ny, n.r * pulse, 0, Math.PI * 2);
                ctx.fill();
            });
            // Subtle scan line
            const scanY = (t * 0.12 % 1) * h;
            const scanGrad = ctx.createLinearGradient(0, scanY - 2, 0, scanY + 2);
            scanGrad.addColorStop(0, 'rgba(56, 189, 248, 0)');
            scanGrad.addColorStop(0.5, 'rgba(56, 189, 248, 0.06)');
            scanGrad.addColorStop(1, 'rgba(56, 189, 248, 0)');
            ctx.fillStyle = scanGrad;
            ctx.fillRect(0, scanY - 2, w, 4);
        };
    },

    makeWeatherRenderer(canvas) {
        const condition = String(canvas.dataset.condition || '').toLowerCase();
        const temp      = Number(canvas.dataset.temp    || 62);
        const wind      = Number(canvas.dataset.wind    || 0);
        const hour      = Number(canvas.dataset.hour    ?? -1);
        const sunrise   = Number(canvas.dataset.sunrise ?? 6);
        const sunset    = Number(canvas.dataset.sunset  ?? 19);
        const terrain   = String(canvas.dataset.terrain || 'hills');

        const isRain  = condition.includes('rain') || condition.includes('drizzle') || condition.includes('shower');
        const isStorm = condition.includes('storm') || condition.includes('thunder');
        const isSnow  = condition.includes('snow') || condition.includes('sleet') || condition.includes('ice');
        const isSun   = condition.includes('sun') || condition.includes('clear');
        const isFog   = condition.includes('fog') || condition.includes('haze') || condition.includes('mist');

        // Time-of-day phase at the location.
        // If backend didn't return hour (-1), fall back to browser local time.
        const localHour = hour >= 0 ? hour : new Date().getHours();
        const isNight   = localHour < sunrise - 1 || localHour >= sunset + 1;
        const isDawn    = !isNight && localHour >= sunrise - 1 && localHour < sunrise + 1;
        const isDusk    = !isNight && localHour >= sunset - 1 && localHour < sunset + 1.5;
        const isGolden  = !isNight && !isDusk && localHour >= sunset - 2.5 && localHour < sunset - 1;

        // Seeded star field (consistent, not random per frame)
        const stars = Array.from({ length: 80 }, (_, i) => ({
            x: ((i * 1973 + 83) % 9973) / 9973,
            y: ((i * 2741 + 17) % 9871) / 9871 * 0.72, // keep stars in upper 72%
            r: 0.5 + ((i * 137) % 10) / 10 * 1.2,
            twinkle: (i * 0.17) % (Math.PI * 2),
        }));

        // 0 = freezing (32F), 1 = hot (100F+)
        const warmth = Math.max(0, Math.min(1, (temp - 32) / 68));
        // Rain tilt: straight down at calm, ~35° at 20+ mph
        const rainAngle = Math.min(0.62, wind * 0.031);

        // Angled rain streaks
        const droplets = Array.from({ length: 130 }, (_, i) => ({
            x: (i * 37.3) % 1,
            y: ((i * 83.7) % 991) / 991,
            v: 0.38 + ((i * 17) % 100) / 100,
            len: 0.016 + ((i * 11) % 100) / 5500,
        }));

        // Parallax snow: near / mid / far layers
        const snowLayers = [
            Array.from({ length: 28 }, (_, i) => ({ x: (i * 0.618) % 1, y: (i * 0.38) % 1, sz: 3.5 + (i % 3) * 0.8, sp: 0.0011, dr: i * 0.28 })),
            Array.from({ length: 48 }, (_, i) => ({ x: (i * 0.382) % 1, y: (i * 0.23) % 1, sz: 2.0,                   sp: 0.00065, dr: i * 0.18 })),
            Array.from({ length: 72 }, (_, i) => ({ x: (i * 0.236) % 1, y: (i * 0.17) % 1, sz: 1.1,                   sp: 0.00032, dr: i * 0.10 })),
        ];

        // Drifting cloud blobs
        const clouds = Array.from({ length: 5 }, (_, i) => ({
            x: 0.08 + (i * 0.22) % 1.1,
            y: 0.05 + (i * 0.09) % 0.26,
            rw: 0.18 + (i * 0.07) % 0.16,
            rh: 0.055 + (i * 0.022) % 0.048,
            sp: 0.000055 + i * 0.000025,
            a: (isStorm)        ? 0.50 + (i * 0.06) % 0.16
             : (isRain)         ? 0.40 + (i * 0.06) % 0.18
             : (isFog)          ? 0.68 + (i * 0.08) % 0.14
             : (!isSun)         ? 0.28 + (i * 0.06) % 0.16
             :                    0.08 + (i * 0.04) % 0.08,
        }));

        const rayCount = 14;
        let flash = 0;
        let flashCooldown = 5 + Math.random() * 7;

        const drawCloud = (ctx, cx, cy, rw, rh, alpha) => {
            ctx.fillStyle = `rgba(220, 230, 242, ${alpha})`;
            ctx.beginPath(); ctx.ellipse(cx,            cy,            rw,        rh,        0, 0, Math.PI * 2); ctx.fill();
            ctx.beginPath(); ctx.ellipse(cx - rw * 0.38, cy - rh * 0.34, rw * 0.54, rh * 0.72, 0, 0, Math.PI * 2); ctx.fill();
            ctx.beginPath(); ctx.ellipse(cx + rw * 0.34, cy - rh * 0.28, rw * 0.50, rh * 0.68, 0, 0, Math.PI * 2); ctx.fill();
            ctx.beginPath(); ctx.ellipse(cx + rw * 0.07, cy - rh * 0.52, rw * 0.40, rh * 0.66, 0, 0, Math.PI * 2); ctx.fill();
        };

        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.016;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);

            // ── Sky gradient (time-of-day × condition) ────────────────────────
            const sky = ctx.createLinearGradient(0, 0, 0, h);
            if (isNight) {
                sky.addColorStop(0,   'rgba(2, 5, 16, 0.96)');
                sky.addColorStop(0.6, 'rgba(8, 12, 28, 0.65)');
                sky.addColorStop(1,   'rgba(14, 18, 40, 0.28)');
            } else if (isDawn) {
                sky.addColorStop(0,   'rgba(8, 10, 30, 0.82)');
                sky.addColorStop(0.5, 'rgba(90, 38, 18, 0.42)');
                sky.addColorStop(1,   'rgba(224, 104, 36, 0.16)');
            } else if (isDusk) {
                sky.addColorStop(0,   'rgba(14, 10, 30, 0.76)');
                sky.addColorStop(0.42,'rgba(115, 40, 16, 0.48)');
                sky.addColorStop(1,   'rgba(245, 72, 18, 0.18)');
            } else if (isGolden) {
                sky.addColorStop(0,   'rgba(18, 22, 50, 0.62)');
                sky.addColorStop(0.58,'rgba(188, 92, 20, 0.30)');
                sky.addColorStop(1,   'rgba(255, 155, 38, 0.12)');
            } else if (isStorm) {
                sky.addColorStop(0, 'rgba(12, 22, 38, 0.75)');
                sky.addColorStop(1, 'rgba(28, 44, 64, 0.18)');
            } else if (isRain) {
                sky.addColorStop(0, 'rgba(22, 40, 62, 0.62)');
                sky.addColorStop(1, 'rgba(36, 56, 80, 0.14)');
            } else if (isSnow) {
                sky.addColorStop(0, 'rgba(125, 160, 200, 0.50)');
                sky.addColorStop(1, 'rgba(92, 128, 170, 0.10)');
            } else if (isSun) {
                const r0 = Math.round(18 + warmth * 30), g0 = Math.round(30 + warmth * 18), b0 = Math.round(82 - warmth * 44);
                sky.addColorStop(0,    `rgba(${r0}, ${g0}, ${b0}, 0.56)`);
                sky.addColorStop(0.55, `rgba(${Math.round(155 + warmth * 65)}, ${Math.round(85 + warmth * 35)}, ${Math.round(26 + warmth * 10)}, 0.22)`);
                sky.addColorStop(1,    'rgba(255, 162, 48, 0.06)');
            } else if (isFog) {
                sky.addColorStop(0, 'rgba(152, 166, 178, 0.62)');
                sky.addColorStop(1, 'rgba(192, 204, 210, 0.16)');
            } else {
                sky.addColorStop(0, 'rgba(48, 62, 78, 0.54)');
                sky.addColorStop(1, 'rgba(74, 92, 108, 0.12)');
            }
            ctx.fillStyle = sky;
            ctx.fillRect(0, 0, w, h);

            // ── Night: stars + moon ───────────────────────────────────────────
            if (isNight) {
                const starA = (isRain || isSnow || isStorm) ? 0.22 : 0.88;
                stars.forEach((s) => {
                    const twink = 0.58 + Math.sin(t * 1.35 + s.twinkle) * 0.32;
                    ctx.fillStyle = `rgba(230, 240, 255, ${(starA * twink).toFixed(2)})`;
                    ctx.beginPath(); ctx.arc(s.x * w, s.y * h, s.r, 0, Math.PI * 2); ctx.fill();
                });
                // Moon: disk + crescent shadow + soft halo
                const moonX = w * 0.78, moonY = h * 0.17;
                const moonR = Math.max(10, Math.min(22, (w + h) * 0.014));
                const mHalo = ctx.createRadialGradient(moonX, moonY, moonR, moonX, moonY, moonR * 3.8);
                mHalo.addColorStop(0, 'rgba(210, 228, 255, 0.26)');
                mHalo.addColorStop(1, 'rgba(180, 210, 255, 0)');
                ctx.fillStyle = mHalo;
                ctx.beginPath(); ctx.arc(moonX, moonY, moonR * 3.8, 0, Math.PI * 2); ctx.fill();
                ctx.fillStyle = 'rgba(238, 246, 255, 0.93)';
                ctx.beginPath(); ctx.arc(moonX, moonY, moonR, 0, Math.PI * 2); ctx.fill();
                ctx.fillStyle = 'rgba(6, 10, 24, 0.76)';
                ctx.beginPath(); ctx.arc(moonX - moonR * 0.33, moonY - moonR * 0.04, moonR * 0.86, 0, Math.PI * 2); ctx.fill();
            }

            // ── Dawn / dusk / golden: horizon glow ───────────────────────────
            if (isDawn || isDusk || isGolden) {
                const hr = isDusk ? { r: 242, g: 62, b: 18 } : isDawn ? { r: 250, g: 112, b: 48 } : { r: 255, g: 148, b: 36 };
                const ha = isDusk ? 0.54 : isDawn ? 0.46 : 0.36;
                const glow = ctx.createRadialGradient(w * 0.5, h * 0.72, 0, w * 0.5, h * 0.72, w * 0.68);
                glow.addColorStop(0,   `rgba(${hr.r}, ${hr.g}, ${hr.b}, ${ha})`);
                glow.addColorStop(0.42,`rgba(${hr.r}, ${hr.g}, ${hr.b}, ${ha * 0.38})`);
                glow.addColorStop(1,   `rgba(${hr.r}, ${hr.g}, ${hr.b}, 0)`);
                ctx.fillStyle = glow;
                ctx.fillRect(0, 0, w, h);
            }

            // ── Sun: atmospheric halo + rotating crepuscular rays ─────────────
            if (!isNight && (isSun || (!isRain && !isStorm && !isSnow && !isFog))) {
                const sunX = w * 0.75, sunY = h * 0.19;
                const sunR = Math.max(16, Math.min(34, (w + h) * 0.022));

                const halo = ctx.createRadialGradient(sunX, sunY, sunR, sunX, sunY, sunR * 4.4);
                halo.addColorStop(0,    `rgba(255, 218, 128, ${isSun ? 0.50 + warmth * 0.12 : 0.10})`);
                halo.addColorStop(0.42, `rgba(255, 170, 68,  ${isSun ? 0.18 + warmth * 0.08 : 0.04})`);
                halo.addColorStop(1,    'rgba(255, 138, 24, 0)');
                ctx.fillStyle = halo;
                ctx.beginPath(); ctx.arc(sunX, sunY, sunR * 4.4, 0, Math.PI * 2); ctx.fill();

                if (isSun) {
                    ctx.save();
                    ctx.translate(sunX, sunY);
                    ctx.rotate(t * 0.030);
                    for (let r = 0; r < rayCount; r++) {
                        const a = (r / rayCount) * Math.PI * 2;
                        const pulse = 0.50 + Math.sin(t * 0.70 + r * 0.84) * 0.16;
                        const rLen = sunR * (5.0 + Math.sin(t * 0.48 + r * 1.18) * 1.5);
                        const rWid = sunR * 0.30;
                        const rg = ctx.createLinearGradient(0, 0, Math.cos(a) * rLen, Math.sin(a) * rLen);
                        rg.addColorStop(0,    `rgba(255, 240, 172, ${pulse * 0.44})`);
                        rg.addColorStop(0.44, `rgba(255, 206, 96,  ${pulse * 0.14})`);
                        rg.addColorStop(1,    'rgba(255, 172, 44, 0)');
                        ctx.fillStyle = rg;
                        const perp = a + Math.PI / 2;
                        ctx.beginPath();
                        ctx.moveTo(0, 0);
                        ctx.lineTo( Math.cos(perp) * rWid * 0.5,  Math.sin(perp) * rWid * 0.5);
                        ctx.lineTo( Math.cos(a) * rLen,            Math.sin(a) * rLen);
                        ctx.lineTo(-Math.cos(perp) * rWid * 0.5, -Math.sin(perp) * rWid * 0.5);
                        ctx.closePath();
                        ctx.fill();
                    }
                    ctx.restore();

                    const disk = ctx.createRadialGradient(sunX, sunY, 0, sunX, sunY, sunR);
                    disk.addColorStop(0,    'rgba(255, 254, 232, 0.97)');
                    disk.addColorStop(0.62, 'rgba(255, 228, 136, 0.90)');
                    disk.addColorStop(1,    'rgba(255, 188, 60,  0.75)');
                    ctx.fillStyle = disk;
                    ctx.beginPath(); ctx.arc(sunX, sunY, sunR, 0, Math.PI * 2); ctx.fill();
                }
            }

            // ── Drifting cloud blobs ──────────────────────────────────────────
            clouds.forEach((c) => {
                c.x += c.sp;
                if (c.x > 1.28) c.x = -0.28;
                drawCloud(ctx, c.x * w, c.y * h, c.rw * w, c.rh * h, c.a);
            });

            // ── Fog veil ──────────────────────────────────────────────────────
            if (isFog) {
                const fogY = h * 0.44;
                const fg = ctx.createLinearGradient(0, fogY, 0, h);
                fg.addColorStop(0,   'rgba(182, 194, 202, 0)');
                fg.addColorStop(0.5, `rgba(182, 194, 202, ${0.28 + Math.sin(t * 0.38) * 0.06})`);
                fg.addColorStop(1,   'rgba(188, 198, 204, 0.44)');
                ctx.fillStyle = fg;
                ctx.fillRect(0, fogY, w, h - fogY);
            }

            // ── Wind-angled rain streaks ──────────────────────────────────────
            if (isRain || isStorm) {
                const intensity = isStorm ? 1.75 : 1.0;
                ctx.strokeStyle = `rgba(192, 222, 255, ${0.34 * intensity})`;
                ctx.lineWidth = isStorm ? 1.5 : 0.85;
                droplets.forEach((d) => {
                    d.y += (0.0025 + d.v * 0.0023) * intensity;
                    d.x += Math.tan(rainAngle) * 0.0012 * d.v * intensity;
                    if (d.y > 1.06) { d.y = -0.09; }
                    if (d.x > 1.12) d.x -= 1.22; else if (d.x < -0.12) d.x += 1.22;
                    const px = d.x * w, py = d.y * h;
                    const dx = Math.sin(rainAngle) * d.len * h * 1.7;
                    const dy = Math.cos(rainAngle) * d.len * h * 1.7;
                    ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(px + dx, py + dy); ctx.stroke();
                });
                // Splash dots at the low horizon
                const splashY = h * 0.74;
                ctx.fillStyle = 'rgba(214, 236, 255, 0.20)';
                for (let i = 0; i < 22; i++) {
                    const sx = ((i * 127 + Math.floor(t * 10) * 61) % (w * 10)) / 10;
                    const jy = Math.sin(t * 14 + i * 2.7) * 3;
                    ctx.beginPath(); ctx.arc(sx % w, splashY + jy, 1.5, 0, Math.PI * 2); ctx.fill();
                }
            }

            // ── Lightning flash (storm only) ──────────────────────────────────
            if (isStorm) {
                flashCooldown -= 0.016;
                if (flash > 0) {
                    ctx.fillStyle = `rgba(222, 238, 255, ${flash * 0.30})`;
                    ctx.fillRect(0, 0, w, h);
                    flash = Math.max(0, flash - 0.13);
                }
                if (flashCooldown <= 0) { flash = 1.0; flashCooldown = 4.5 + Math.random() * 8; }
            }

            // ── Parallax snow ─────────────────────────────────────────────────
            if (isSnow) {
                snowLayers.forEach((layer, li) => {
                    const alpha = 0.78 - li * 0.20;
                    layer.forEach((f) => {
                        f.y += f.sp;
                        f.x += Math.sin(t * 0.54 + f.dr) * 0.00027;
                        if (f.y > 1.05) { f.y = -0.05; f.x = Math.random(); }
                        const px = ((f.x * w) % w + w) % w;
                        ctx.fillStyle = `rgba(236, 246, 255, ${alpha})`;
                        ctx.beginPath(); ctx.arc(px, f.y * h, f.sz, 0, Math.PI * 2); ctx.fill();
                    });
                });
            }

            // ── Horizon shimmer ───────────────────────────────────────────────
            ctx.lineWidth = 1;
            ctx.strokeStyle = isSun
                ? `rgba(255, 208, 106, ${0.08 + warmth * 0.04})`
                : (isRain || isStorm) ? 'rgba(18, 32, 52, 0.09)'
                : 'rgba(20, 32, 48, 0.07)';
            const horizonY = h * 0.63;
            for (let i = 0; i < 3; i++) {
                ctx.beginPath();
                for (let x = 0; x <= w; x += 6) {
                    const y = horizonY + i * 13 + Math.sin(x * 0.017 + t * (0.56 + i * 0.07)) * (3 + i * 1.4);
                    if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
                }
                ctx.stroke();
            }

            // ── Terrain silhouette (drawn last — sits in front of weather) ───
            (function() {
                const groundY = h * 0.74;
                ctx.fillStyle = isNight           ? 'rgba(3, 6, 16, 0.95)'
                              : (isDawn || isDusk) ? 'rgba(8, 10, 22, 0.86)'
                              : 'rgba(10, 14, 26, 0.72)';
                ctx.beginPath();
                ctx.moveTo(0, h);
                if (terrain === 'mountains') {
                    ctx.lineTo(0, groundY + h * 0.04);
                    ctx.lineTo(w * 0.08, groundY - h * 0.07);
                    ctx.lineTo(w * 0.18, groundY + h * 0.01);
                    ctx.lineTo(w * 0.30, groundY - h * 0.21);
                    ctx.lineTo(w * 0.42, groundY + h * 0.01);
                    ctx.lineTo(w * 0.53, groundY - h * 0.15);
                    ctx.lineTo(w * 0.64, groundY + h * 0.02);
                    ctx.lineTo(w * 0.74, groundY - h * 0.10);
                    ctx.lineTo(w * 0.87, groundY + h * 0.01);
                    ctx.lineTo(w, groundY - h * 0.04);
                } else if (terrain === 'desert') {
                    const b = groundY + h * 0.02;
                    ctx.lineTo(0, b);
                    ctx.lineTo(w * 0.14, b + h * 0.01);
                    ctx.lineTo(w * 0.19, b - h * 0.09); ctx.lineTo(w * 0.33, b - h * 0.09); ctx.lineTo(w * 0.37, b + h * 0.01);
                    ctx.lineTo(w * 0.50, b + h * 0.02);
                    ctx.lineTo(w * 0.56, b - h * 0.13); ctx.lineTo(w * 0.69, b - h * 0.13); ctx.lineTo(w * 0.73, b + h * 0.01);
                    ctx.lineTo(w, b);
                } else if (terrain === 'coast') {
                    const b = groundY + h * 0.05;
                    ctx.lineTo(0, b);
                    ctx.bezierCurveTo(w * 0.28, b - h * 0.012, w * 0.58, b + h * 0.010, w, b - h * 0.005);
                } else if (terrain === 'city') {
                    const b = groundY;
                    const blds = [
                        [0.03,0.06,0.13],[0.10,0.05,0.22],[0.16,0.07,0.15],[0.24,0.04,0.28],
                        [0.29,0.06,0.17],[0.36,0.05,0.11],[0.42,0.08,0.24],[0.51,0.05,0.14],
                        [0.57,0.07,0.18],[0.65,0.04,0.26],[0.70,0.06,0.12],[0.77,0.05,0.20],
                        [0.83,0.07,0.15],[0.91,0.09,0.10],
                    ];
                    ctx.lineTo(0, b);
                    blds.forEach(([bx, bw, bh]) => {
                        ctx.lineTo(bx * w, b); ctx.lineTo(bx * w, b - bh * h);
                        ctx.lineTo((bx + bw) * w, b - bh * h); ctx.lineTo((bx + bw) * w, b);
                    });
                    ctx.lineTo(w, b);
                } else if (terrain === 'plains') {
                    const b = groundY + h * 0.03;
                    ctx.lineTo(0, b);
                    // Water tower silhouette
                    ctx.lineTo(w * 0.10, b); ctx.lineTo(w * 0.10, b - h * 0.08);
                    ctx.lineTo(w * 0.113, b - h * 0.08); ctx.lineTo(w * 0.113, b - h * 0.13);
                    ctx.lineTo(w * 0.142, b - h * 0.13); ctx.lineTo(w * 0.142, b - h * 0.08);
                    ctx.lineTo(w * 0.155, b - h * 0.08); ctx.lineTo(w * 0.155, b);
                    ctx.lineTo(w * 0.80, b + h * 0.005);
                    // Grain elevator
                    ctx.lineTo(w * 0.82, b); ctx.lineTo(w * 0.82, b - h * 0.07);
                    ctx.lineTo(w * 0.85, b - h * 0.07); ctx.lineTo(w * 0.85, b - h * 0.11);
                    ctx.lineTo(w * 0.875, b - h * 0.11); ctx.lineTo(w * 0.875, b);
                    ctx.lineTo(w, b + h * 0.01);
                } else {
                    // hills / default
                    const b = groundY + h * 0.03;
                    ctx.lineTo(0, b + h * 0.02);
                    ctx.bezierCurveTo(w * 0.18, b - h * 0.03, w * 0.30, b + h * 0.01, w * 0.44, b - h * 0.07);
                    ctx.bezierCurveTo(w * 0.58, b - h * 0.15, w * 0.72, b - h * 0.02, w * 0.86, b - h * 0.09);
                    ctx.bezierCurveTo(w * 0.93, b - h * 0.13, w * 0.97, b - h * 0.04, w, b);
                }
                ctx.lineTo(w, h); ctx.closePath(); ctx.fill();

                // Mountain snow caps
                if (terrain === 'mountains') {
                    ctx.fillStyle = 'rgba(232, 242, 255, 0.72)';
                    [[w * 0.30, groundY - h * 0.21, h * 0.058], [w * 0.53, groundY - h * 0.15, h * 0.040], [w * 0.08, groundY - h * 0.07, h * 0.024]]
                        .forEach(([px, py, ph]) => {
                            ctx.beginPath();
                            ctx.moveTo(px - ph * 0.88, py + ph * 0.58);
                            ctx.lineTo(px, py - ph * 0.04);
                            ctx.lineTo(px + ph * 0.88, py + ph * 0.58);
                            ctx.closePath(); ctx.fill();
                        });
                }
                // City night windows
                if (terrain === 'city' && isNight) {
                    ctx.fillStyle = 'rgba(255, 236, 148, 0.72)';
                    for (let i = 0; i < 55; i++) {
                        const wx2 = (((i * 1973 + 83) % 9973) / 9973) * w;
                        const wy  = groundY - (((i * 2741 + 17) % 9871) / 9871) * groundY * 0.28;
                        if (wy > h * 0.10 && wy < groundY - h * 0.01) ctx.fillRect(wx2, wy, 3, 4);
                    }
                }
                // Coastal water shimmer
                if (terrain === 'coast') {
                    ctx.strokeStyle = 'rgba(140, 202, 245, 0.20)';
                    ctx.lineWidth = 1;
                    for (let i = 0; i < 5; i++) {
                        const wy = groundY + h * 0.06 + i * h * 0.04;
                        ctx.beginPath();
                        for (let x = 0; x <= w; x += 8) {
                            const y = wy + Math.sin(x * 0.025 + t * 0.78 + i * 1.2) * 3;
                            if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
                        }
                        ctx.stroke();
                    }
                }
            })();

            // ── Temp readout ──────────────────────────────────────────────────
            ctx.fillStyle = isSun ? `rgba(255, 244, 192, ${0.76 + warmth * 0.14})` : 'rgba(216, 232, 252, 0.76)';
            ctx.font = "700 12px 'IBM Plex Mono', monospace";
            ctx.fillText(`${Math.round(temp)}F`, 14, 24);
        };
    },

    makeWeather7DayRenderer(canvas) {
        const condition = String(canvas.dataset.condition || '').toLowerCase();
        const terrain   = String(canvas.dataset.terrain   || 'hills');
        const isSun   = condition.includes('clear') || condition.includes('sun');
        const isRain  = condition.includes('rain') || condition.includes('drizzle');
        const isSnow  = condition.includes('snow');
        const isStorm = condition.includes('storm') || condition.includes('thunder');

        // Slow-drifting cloud blobs for background ambiance
        const clouds = Array.from({ length: 6 }, (_, i) => ({
            x: (i * 0.19 + 0.04) % 1.1,
            y: 0.05 + (i * 0.11) % 0.32,
            rw: 0.14 + (i * 0.06) % 0.12,
            rh: 0.045 + (i * 0.02) % 0.038,
            sp: 0.000038 + i * 0.000018,
            a: isSun ? 0.07 + (i * 0.03) % 0.07 : 0.22 + (i * 0.05) % 0.14,
        }));
        const drawCloud = (ctx, cx, cy, rw, rh, alpha) => {
            ctx.fillStyle = `rgba(220, 230, 242, ${alpha})`;
            ctx.beginPath(); ctx.ellipse(cx,            cy,            rw,        rh,        0, 0, Math.PI * 2); ctx.fill();
            ctx.beginPath(); ctx.ellipse(cx - rw * 0.38, cy - rh * 0.34, rw * 0.52, rh * 0.70, 0, 0, Math.PI * 2); ctx.fill();
            ctx.beginPath(); ctx.ellipse(cx + rw * 0.32, cy - rh * 0.26, rw * 0.48, rh * 0.66, 0, 0, Math.PI * 2); ctx.fill();
        };
        // Slow diagonal rain streaks for rainy background
        const drops = Array.from({ length: 60 }, (_, i) => ({
            x: (i * 37.3) % 1, y: ((i * 83.7) % 991) / 991, v: 0.5 + ((i * 17) % 100) / 100,
        }));
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.016;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);

            // Sky
            const sky = ctx.createLinearGradient(0, 0, 0, h);
            if (isStorm || isRain) {
                sky.addColorStop(0, 'rgba(16, 28, 48, 0.68)');
                sky.addColorStop(1, 'rgba(28, 44, 66, 0.18)');
            } else if (isSnow) {
                sky.addColorStop(0, 'rgba(110, 148, 192, 0.48)');
                sky.addColorStop(1, 'rgba(80, 120, 164, 0.10)');
            } else if (isSun) {
                sky.addColorStop(0, 'rgba(18, 28, 72, 0.52)');
                sky.addColorStop(0.6, 'rgba(38, 80, 130, 0.22)');
                sky.addColorStop(1, 'rgba(80, 130, 180, 0.06)');
            } else {
                sky.addColorStop(0, 'rgba(44, 58, 74, 0.50)');
                sky.addColorStop(1, 'rgba(68, 84, 102, 0.10)');
            }
            ctx.fillStyle = sky; ctx.fillRect(0, 0, w, h);

            // Clouds
            clouds.forEach((c) => {
                c.x += c.sp; if (c.x > 1.26) c.x = -0.26;
                drawCloud(ctx, c.x * w, c.y * h, c.rw * w, c.rh * h, c.a);
            });

            // Background rain (subtle, non-distracting)
            if (isRain || isStorm) {
                ctx.strokeStyle = `rgba(180, 216, 255, 0.18)`;
                ctx.lineWidth = 0.7;
                drops.forEach((d) => {
                    d.y += 0.003 * d.v; d.x += 0.0006 * d.v;
                    if (d.y > 1.04) { d.y = -0.08; } if (d.x > 1.08) d.x -= 1.1;
                    ctx.beginPath(); ctx.moveTo(d.x * w, d.y * h);
                    ctx.lineTo(d.x * w + 2, d.y * h + 8); ctx.stroke();
                });
            }

            // Terrain silhouette (same logic as main renderer, reused here)
            const groundY = h * 0.76;
            ctx.fillStyle = 'rgba(8, 12, 22, 0.80)';
            ctx.beginPath(); ctx.moveTo(0, h);
            if (terrain === 'mountains') {
                ctx.lineTo(0, groundY + h * 0.04); ctx.lineTo(w * 0.08, groundY - h * 0.07);
                ctx.lineTo(w * 0.18, groundY + h * 0.01); ctx.lineTo(w * 0.30, groundY - h * 0.20);
                ctx.lineTo(w * 0.42, groundY + h * 0.01); ctx.lineTo(w * 0.53, groundY - h * 0.14);
                ctx.lineTo(w * 0.64, groundY + h * 0.02); ctx.lineTo(w * 0.74, groundY - h * 0.09);
                ctx.lineTo(w * 0.87, groundY + h * 0.01); ctx.lineTo(w, groundY - h * 0.04);
            } else if (terrain === 'city') {
                const b = groundY;
                [[0.04,0.07,0.12],[0.12,0.06,0.20],[0.19,0.08,0.14],[0.28,0.05,0.26],
                 [0.34,0.07,0.16],[0.42,0.09,0.22],[0.52,0.06,0.13],[0.59,0.08,0.17],
                 [0.68,0.05,0.24],[0.74,0.07,0.11],[0.82,0.06,0.18],[0.89,0.09,0.09]].forEach(([bx, bw, bh]) => {
                    ctx.lineTo(bx * w, b); ctx.lineTo(bx * w, b - bh * h);
                    ctx.lineTo((bx + bw) * w, b - bh * h); ctx.lineTo((bx + bw) * w, b);
                }); ctx.lineTo(w, b);
            } else if (terrain === 'desert') {
                const b = groundY + h * 0.02;
                ctx.lineTo(0, b); ctx.lineTo(w * 0.14, b + h * 0.01);
                ctx.lineTo(w * 0.19, b - h * 0.09); ctx.lineTo(w * 0.33, b - h * 0.09); ctx.lineTo(w * 0.37, b + h * 0.01);
                ctx.lineTo(w * 0.55, b - h * 0.13); ctx.lineTo(w * 0.68, b - h * 0.13); ctx.lineTo(w * 0.72, b + h * 0.01);
                ctx.lineTo(w, b);
            } else {
                const b = groundY + h * 0.03;
                ctx.lineTo(0, b + h * 0.02);
                ctx.bezierCurveTo(w * 0.22, b - h * 0.04, w * 0.38, b + h * 0.01, w * 0.52, b - h * 0.06);
                ctx.bezierCurveTo(w * 0.66, b - h * 0.13, w * 0.80, b - h * 0.02, w, b);
            }
            ctx.lineTo(w, h); ctx.closePath(); ctx.fill();
        };
    },

    makeShoppingRenderer(canvas) {
        const brandPrimary = String(canvas.dataset.brandPrimary || '#1d1d1b');
        const brandAccent  = String(canvas.dataset.brandAccent  || '#00a651');
        const activity     = String(canvas.dataset.activity     || 'sport').trim();
        const fitSignal    = String(canvas.dataset.fitSignal    || '').trim();

        const hex = (h) => {
            const r = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(h);
            return r ? { r: parseInt(r[1], 16), g: parseInt(r[2], 16), b: parseInt(r[3], 16) } : { r: 29, g: 29, b: 27 };
        };
        const pc = hex(brandPrimary);
        const ac = hex(brandAccent);
        const rgb  = (c, a = 1) => `rgba(${c.r},${c.g},${c.b},${a})`;
        const lerp = (a, b, t) => a + (b - a) * t;

        let t = 0;
        let draw;

        // ── RUNNING — stadium night / speed ──────────────────────────────────
        if (activity === 'running') {
            const streaks = Array.from({ length: 48 }, (_, i) => ({
                x:   (i * 0.618) % 1,
                y:   i / 48,
                len: 0.18 + ((i * 0.11) % 0.28),
                spd: 0.006 + ((i * 0.0021) % 0.009),
                w:   0.6 + ((i * 0.29) % 2.2),
                a:   0.35 + ((i * 0.13) % 0.5),
                hot: i % 6 === 0,
            }));
            const lanes = 9;
            draw = () => {
                const ctx = canvas.getContext('2d'); if (!ctx) return;
                const { dpr, w, h } = this.fitCanvas(canvas); t += 0.018;
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

                // Deep dark base
                ctx.fillStyle = `rgb(${pc.r},${pc.g},${pc.b})`; ctx.fillRect(0, 0, w, h);

                // Stadium floodlight — upper-right, hard and bright
                const fl = ctx.createRadialGradient(w * 0.9, 0, 0, w * 0.9, 0, Math.min(w, h) * 1.3);
                fl.addColorStop(0,   rgb(ac, 0.55));
                fl.addColorStop(0.3, rgb(ac, 0.20));
                fl.addColorStop(0.7, rgb(ac, 0.06));
                fl.addColorStop(1,   rgb(ac, 0));
                ctx.fillStyle = fl; ctx.fillRect(0, 0, w, h);

                // Opposing cooler fill — lower-left
                const fl2 = ctx.createRadialGradient(w * 0.05, h, 0, w * 0.05, h, Math.min(w, h) * 0.9);
                fl2.addColorStop(0,   `rgba(${lerp(pc.r,255,0.06)|0},${lerp(pc.g,255,0.06)|0},${lerp(pc.b,255,0.12)|0},0.28)`);
                fl2.addColorStop(1,   rgb(pc, 0));
                ctx.fillStyle = fl2; ctx.fillRect(0, 0, w, h);

                // Track lane perspective lines (converge at right-center vanishing point)
                ctx.strokeStyle = 'rgba(255,255,255,0.055)'; ctx.lineWidth = 1;
                for (let i = 0; i <= lanes; i++) {
                    const startY = h * 0.08 + (h * 0.84) * (i / lanes);
                    ctx.beginPath(); ctx.moveTo(0, startY); ctx.lineTo(w, h * 0.5); ctx.stroke();
                }

                // Speed streaks — sweep right across full canvas
                streaks.forEach((s) => {
                    s.x += s.spd; if (s.x > 1.15) s.x = -s.len - 0.02;
                    const sx = s.x * w, ex = sx + s.len * w;
                    const sy = s.y * h + Math.sin(t * 1.4 + s.y * 8) * (h * 0.008);
                    const c  = s.hot ? ac : { r: 255, g: 255, b: 255 };
                    const g  = ctx.createLinearGradient(sx, 0, ex, 0);
                    g.addColorStop(0,   rgb(c, 0));
                    g.addColorStop(0.25, rgb(c, s.a));
                    g.addColorStop(0.8, rgb(c, s.a * 0.6));
                    g.addColorStop(1,   rgb(c, 0));
                    ctx.strokeStyle = g; ctx.lineWidth = s.w;
                    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ex, sy); ctx.stroke();
                });

                if (fitSignal) {
                    ctx.fillStyle = rgb(ac, 0.55); ctx.font = "600 11px 'IBM Plex Mono', monospace";
                    ctx.fillText(fitSignal.toLowerCase(), 18, h - 18);
                }
            };

        // ── BASKETBALL — arena court under lights ─────────────────────────────
        } else if (activity === 'basketball') {
            draw = () => {
                const ctx = canvas.getContext('2d'); if (!ctx) return;
                const { dpr, w, h } = this.fitCanvas(canvas); t += 0.013;
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

                ctx.fillStyle = `rgb(${Math.min(pc.r+4,32)},${Math.min(pc.g+2,18)},${Math.min(pc.b+2,14)})`; ctx.fillRect(0, 0, w, h);

                // Arena overhead cluster — 3 spotlights
                [0.25, 0.5, 0.75].forEach((xf, i) => {
                    const pulse = 0.88 + Math.sin(t * 0.28 + i * 2.1) * 0.12;
                    const sp = ctx.createRadialGradient(w * xf, h * 0.05, 0, w * xf, h * 0.05, Math.min(w, h) * 0.72 * pulse);
                    sp.addColorStop(0,   rgb(ac, 0.38));
                    sp.addColorStop(0.4, rgb(ac, 0.10));
                    sp.addColorStop(1,   rgb(ac, 0));
                    ctx.fillStyle = sp; ctx.fillRect(0, 0, w, h);
                });

                // Floor — wood parquet gradient
                const floorY = h * 0.58;
                const floor = ctx.createLinearGradient(0, floorY, 0, h);
                floor.addColorStop(0, `rgba(${lerp(pc.r,80,0.3)|0},${lerp(pc.g,52,0.3)|0},${lerp(pc.b,20,0.3)|0},0.55)`);
                floor.addColorStop(1, `rgba(${lerp(pc.r,40,0.2)|0},${lerp(pc.g,26,0.2)|0},${lerp(pc.b,10,0.2)|0},0.70)`);
                ctx.fillStyle = floor; ctx.fillRect(0, floorY, w, h - floorY);

                // Parquet lines (horizontal + vertical planks in perspective)
                ctx.strokeStyle = rgb(ac, 0.09); ctx.lineWidth = 1;
                for (let i = 0; i < 14; i++) {
                    const prog = i / 14;
                    const y = floorY + (h - floorY) * prog;
                    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
                }
                for (let i = 0; i < 28; i++) {
                    ctx.beginPath(); ctx.moveTo((i / 28) * w, floorY); ctx.lineTo((i / 28) * w, h); ctx.stroke();
                }

                // Three-point arc glowing in accent
                const arcCx = w * 0.46, arcCy = floorY + 4;
                const arcR  = Math.min(w, h) * 0.44;
                ctx.strokeStyle = rgb(ac, 0.30); ctx.lineWidth = 2.5;
                ctx.beginPath(); ctx.arc(arcCx, arcCy, arcR, 0, Math.PI); ctx.stroke();

                // Key (paint)
                const kw = w * 0.20, kh = (h - floorY) * 0.36;
                ctx.strokeStyle = rgb(ac, 0.22); ctx.lineWidth = 2;
                ctx.strokeRect(arcCx - kw / 2, arcCy, kw, kh);
                // Free-throw circle
                ctx.beginPath(); ctx.arc(arcCx, arcCy + kh, kw * 0.5, 0, Math.PI); ctx.stroke();

                // Baseline
                ctx.strokeStyle = rgb(ac, 0.18); ctx.lineWidth = 2;
                ctx.beginPath(); ctx.moveTo(w * 0.05, floorY); ctx.lineTo(w * 0.95, floorY); ctx.stroke();

                if (fitSignal) {
                    ctx.fillStyle = rgb(ac, 0.55); ctx.font = "600 11px 'IBM Plex Mono', monospace";
                    ctx.fillText(fitSignal.toLowerCase(), 18, h - 18);
                }
            };

        // ── SOCCER — night pitch under floodlights ────────────────────────────
        } else if (activity === 'soccer') {
            draw = () => {
                const ctx = canvas.getContext('2d'); if (!ctx) return;
                const { dpr, w, h } = this.fitCanvas(canvas); t += 0.012;
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

                ctx.fillStyle = `rgb(${pc.r},${pc.g},${pc.b})`; ctx.fillRect(0, 0, w, h);

                // Pitch surface gradient (dark grass)
                const pitchY = h * 0.45;
                const pitch = ctx.createLinearGradient(0, pitchY, 0, h);
                pitch.addColorStop(0, `rgba(${lerp(pc.r,12,0.4)|0},${lerp(pc.g,42,0.4)|0},${lerp(pc.b,18,0.4)|0},0.65)`);
                pitch.addColorStop(1, `rgba(${lerp(pc.r,6,0.3)|0},${lerp(pc.g,28,0.3)|0},${lerp(pc.b,10,0.3)|0},0.80)`);
                ctx.fillStyle = pitch; ctx.fillRect(0, pitchY, w, h - pitchY);

                // Corner floodlights — all 4 corners of sky
                [[0.02, -0.08], [0.98, -0.08]].forEach(([xf, yf]) => {
                    const fl = ctx.createRadialGradient(w * xf, h * yf, 0, w * xf, h * yf, Math.min(w, h) * 1.4);
                    fl.addColorStop(0,   'rgba(255,252,220,0.30)');
                    fl.addColorStop(0.3, rgb(ac, 0.08));
                    fl.addColorStop(1,   rgb(ac, 0));
                    ctx.fillStyle = fl; ctx.fillRect(0, 0, w, h);
                });

                // Pitch stripe alternating bands
                ctx.strokeStyle = 'rgba(255,255,255,0.03)'; ctx.lineWidth = w * 0.072;
                for (let i = 0; i < 8; i += 2) {
                    ctx.beginPath(); ctx.moveTo((i / 8) * w, pitchY); ctx.lineTo((i / 8) * w, h); ctx.stroke();
                }

                // Pitch markings
                const pY = pitchY;
                ctx.strokeStyle = 'rgba(255,255,255,0.22)'; ctx.lineWidth = 1.8;
                // Halfway line
                ctx.beginPath(); ctx.moveTo(0, pY); ctx.lineTo(w, pY); ctx.stroke();
                // Centre circle (perspective ellipse)
                ctx.beginPath(); ctx.ellipse(w * 0.5, pY + (h - pY) * 0.18, w * 0.16, (h - pY) * 0.12, 0, 0, Math.PI * 2); ctx.stroke();
                // Penalty box
                ctx.strokeRect(w * 0.3, pY, w * 0.4, (h - pY) * 0.42);
                // Goal box
                ctx.strokeRect(w * 0.4, pY, w * 0.2, (h - pY) * 0.18);
                // Corner arcs
                ['left', 'right'].forEach((side) => {
                    const cx = side === 'left' ? w * 0.01 : w * 0.99;
                    ctx.beginPath(); ctx.arc(cx, pY, w * 0.04, 0, Math.PI / 2 * (side === 'left' ? 1 : -1) + Math.PI / 2); ctx.stroke();
                });

                if (fitSignal) {
                    ctx.fillStyle = rgb(ac, 0.55); ctx.font = "600 11px 'IBM Plex Mono', monospace";
                    ctx.fillText(fitSignal.toLowerCase(), 18, h - 18);
                }
            };

        // ── LIFESTYLE — warm editorial, slow colour fields ────────────────────
        } else if (activity === 'lifestyle') {
            const orbs = Array.from({ length: 10 }, (_, i) => ({
                x: (i * 0.618) % 1, y: (i * 0.382) % 1,
                r: 0.22 + ((i * 0.071) % 0.20),
                spd: 0.00006 + ((i * 0.000033) % 0.00009),
                phi: i * 2.399,
                warm: i % 3 !== 0,
            }));
            draw = () => {
                const ctx = canvas.getContext('2d'); if (!ctx) return;
                const { dpr, w, h } = this.fitCanvas(canvas); t += 0.007;
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

                // Warm dark base (slightly lighter than sport)
                const base = ctx.createLinearGradient(0, 0, w, h);
                base.addColorStop(0, `rgb(${Math.min(pc.r+22,72)},${Math.min(pc.g+14,48)},${Math.min(pc.b+8,38)})`);
                base.addColorStop(1, `rgb(${pc.r},${pc.g},${pc.b})`);
                ctx.fillStyle = base; ctx.fillRect(0, 0, w, h);

                // Large slow bokeh fields
                orbs.forEach((o) => {
                    const cx = (o.x + Math.sin(t * o.spd * 180 + o.phi) * 0.13) * w;
                    const cy = (o.y + Math.cos(t * o.spd * 160 + o.phi) * 0.11) * h;
                    const r  = o.r * Math.max(w, h) * (0.9 + Math.sin(t * 0.3 + o.phi) * 0.1);
                    const warm = o.warm
                        ? { r: Math.min(255, pc.r + 55), g: Math.min(255, pc.g + 38), b: Math.min(255, pc.b + 22) }
                        : ac;
                    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
                    g.addColorStop(0,   rgb(warm, 0.20));
                    g.addColorStop(0.55,rgb(warm, 0.07));
                    g.addColorStop(1,   rgb(warm, 0));
                    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
                });

                // Wandering accent sweep
                const swX = (Math.sin(t * 0.11) * 0.4 + 0.5) * w;
                const swY = (Math.cos(t * 0.09) * 0.3 + 0.5) * h;
                const sw = ctx.createRadialGradient(swX, swY, 0, swX, swY, Math.min(w, h) * 0.65);
                sw.addColorStop(0,   rgb(ac, 0.22));
                sw.addColorStop(0.5, rgb(ac, 0.07));
                sw.addColorStop(1,   rgb(ac, 0));
                ctx.fillStyle = sw; ctx.fillRect(0, 0, w, h);

                // Faint horizontal grain lines (editorial)
                ctx.strokeStyle = 'rgba(255,255,255,0.025)'; ctx.lineWidth = 1;
                for (let y = 0; y < h; y += 6) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }

                if (fitSignal) {
                    ctx.fillStyle = rgb(ac, 0.55); ctx.font = "600 11px 'IBM Plex Mono', monospace";
                    ctx.fillText(fitSignal.toLowerCase(), 18, h - 18);
                }
            };

        // ── TRAIL — topographic contour / mountain atmosphere ─────────────────
        } else if (activity === 'trail') {
            const contours = Array.from({ length: 24 }, (_, i) => ({
                yBase: i / 24,
                amp:   0.055 + ((i * 0.031) % 0.065),
                freq:  0.0032 + ((i * 0.0011) % 0.0024),
                phi:   i * 1.618,
                a:     0.10 + ((i * 0.019) % 0.14),
            }));
            draw = () => {
                const ctx = canvas.getContext('2d'); if (!ctx) return;
                const { dpr, w, h } = this.fitCanvas(canvas); t += 0.005;
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

                ctx.fillStyle = `rgb(${pc.r},${pc.g},${pc.b})`; ctx.fillRect(0, 0, w, h);

                // Earthy sky glow — top
                const sky = ctx.createLinearGradient(0, 0, 0, h * 0.55);
                sky.addColorStop(0, rgb(ac, 0.14));
                sky.addColorStop(1, rgb(ac, 0));
                ctx.fillStyle = sky; ctx.fillRect(0, 0, w, h);

                // Ground warmth — bottom
                const ground = ctx.createLinearGradient(0, h * 0.6, 0, h);
                ground.addColorStop(0, rgb(ac, 0));
                ground.addColorStop(1, rgb(ac, 0.18));
                ctx.fillStyle = ground; ctx.fillRect(0, 0, w, h);

                // Topographic contour lines
                contours.forEach((c_) => {
                    const baseY = c_.yBase * h;
                    ctx.strokeStyle = rgb(ac, c_.a); ctx.lineWidth = 0.9;
                    ctx.beginPath();
                    for (let x = 0; x <= w; x += 4) {
                        const y = baseY
                            + Math.sin(x * c_.freq + t * 0.35 + c_.phi) * (c_.amp * h)
                            + Math.cos(x * c_.freq * 0.55 + c_.phi * 1.4) * (c_.amp * h * 0.44);
                        x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
                    }
                    ctx.stroke();
                });

                if (fitSignal) {
                    ctx.fillStyle = rgb(ac, 0.55); ctx.font = "600 11px 'IBM Plex Mono', monospace";
                    ctx.fillText(fitSignal.toLowerCase(), 18, h - 18);
                }
            };

        // ── SPORT / DEFAULT — cinematic diagonal beams + particles ────────────
        } else {
            const particles = Array.from({ length: 60 }, (_, i) => ({
                x: (i * 0.618) % 1, y: (i * 0.382) % 1,
                sz: 0.7 + ((i * 0.37) % 1) * 1.8,
                spd: 0.00016 + ((i * 0.000068) % 0.00021),
                phi: i * 2.399,
            }));
            // Diagonal beam definitions
            const beams = Array.from({ length: 5 }, (_, i) => ({
                angle: -0.48 + i * 0.12,
                x: 0.15 + i * 0.18,
                w: 0.04 + ((i * 0.023) % 0.05),
                spd: 0.003 + ((i * 0.0017) % 0.004),
                phi: i * 1.4,
            }));
            draw = () => {
                const ctx = canvas.getContext('2d'); if (!ctx) return;
                const { dpr, w, h } = this.fitCanvas(canvas); t += 0.013;
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

                ctx.fillStyle = `rgb(${pc.r},${pc.g},${pc.b})`; ctx.fillRect(0, 0, w, h);

                // Main accent spotlight — lower-right
                const sX = w * 0.80, sY = h * 0.85;
                const sR = Math.min(w, h) * (0.95 + Math.sin(t * 0.22) * 0.05);
                const spot = ctx.createRadialGradient(sX, sY, 0, sX, sY, sR);
                spot.addColorStop(0,   rgb(ac, 0.42));
                spot.addColorStop(0.35,rgb(ac, 0.16));
                spot.addColorStop(1,   rgb(ac, 0));
                ctx.fillStyle = spot; ctx.fillRect(0, 0, w, h);

                // Upper-left fill
                const ul = ctx.createRadialGradient(w * 0.06, h * 0.10, 0, w * 0.06, h * 0.10, Math.min(w, h) * 0.68);
                ul.addColorStop(0, `rgba(${Math.min(255,pc.r+28)},${Math.min(255,pc.g+28)},${Math.min(255,pc.b+28)},0.24)`);
                ul.addColorStop(1, rgb(pc, 0));
                ctx.fillStyle = ul; ctx.fillRect(0, 0, w, h);

                // Diagonal light beams
                ctx.save();
                beams.forEach((b) => {
                    const cx = (b.x + Math.sin(t * b.spd * 60 + b.phi) * 0.07) * w;
                    const alpha = 0.12 + 0.10 * Math.sin(t * 0.6 + b.phi);
                    ctx.save();
                    ctx.translate(cx, 0);
                    ctx.rotate(b.angle);
                    const bw = b.w * w;
                    const bg = ctx.createLinearGradient(-bw, 0, bw, 0);
                    bg.addColorStop(0, rgb(ac, 0));
                    bg.addColorStop(0.5, rgb(ac, alpha));
                    bg.addColorStop(1, rgb(ac, 0));
                    ctx.fillStyle = bg;
                    ctx.fillRect(-bw, -h * 0.1, bw * 2, h * 1.4);
                    ctx.restore();
                });
                ctx.restore();

                // Rising particles
                particles.forEach((p) => {
                    p.y -= p.spd; if (p.y < -0.02) { p.y = 1.02; p.x = Math.random(); }
                    const px = p.x * w + Math.sin(t * 0.5 + p.phi) * 14;
                    const alpha = 0.20 + 0.26 * Math.sin(t * 1.1 + p.phi);
                    ctx.beginPath(); ctx.arc(px, p.y * h, p.sz, 0, Math.PI * 2);
                    ctx.fillStyle = rgb(ac, alpha); ctx.fill();
                });

                // Scan line
                const scy = ((t * 26) % (h + 36)) - 18;
                const scg = ctx.createLinearGradient(0, scy - 12, 0, scy + 12);
                scg.addColorStop(0, rgb(ac, 0)); scg.addColorStop(0.5, rgb(ac, 0.13)); scg.addColorStop(1, rgb(ac, 0));
                ctx.fillStyle = scg; ctx.fillRect(0, scy - 12, w, 24);

                if (fitSignal) {
                    ctx.fillStyle = rgb(ac, 0.60); ctx.font = "600 11px 'IBM Plex Mono', monospace";
                    ctx.fillText(fitSignal.toLowerCase(), 18, h - 18);
                }
            };
        }

        return () => draw();
    },

    makeConnectionsRenderer(canvas) {
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.004;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            // Deep dark base
            ctx.fillStyle = 'rgba(6, 8, 18, 1)';
            ctx.fillRect(0, 0, w, h);
            // Slow breathing ambient glow — blue-indigo
            const pulse = 0.06 + Math.sin(t) * 0.025;
            const amb = ctx.createRadialGradient(w * 0.38, h * 0.5, 0, w * 0.38, h * 0.5, w * 0.72);
            amb.addColorStop(0, `rgba(60, 80, 200, ${pulse})`);
            amb.addColorStop(0.5, `rgba(40, 55, 140, ${pulse * 0.4})`);
            amb.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = amb;
            ctx.fillRect(0, 0, w, h);
            // Vignette
            const vig = ctx.createRadialGradient(w * 0.5, h * 0.5, h * 0.1, w * 0.5, h * 0.5, w * 0.85);
            vig.addColorStop(0, 'rgba(0,0,0,0)');
            vig.addColorStop(1, 'rgba(0,0,0,0.72)');
            ctx.fillStyle = vig;
            ctx.fillRect(0, 0, w, h);
        };
    },

    makeTasksRenderer(canvas) {
        const open = Math.max(0, Number(canvas.dataset.open || 0));
        const done = Math.max(0, Number(canvas.dataset.done || 0));
        const total = Math.max(1, open + done);
        const completion = done / total;
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.010;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);
            // Base: deep dark, slightly warm at completion
            const warm = completion;
            const bg = ctx.createLinearGradient(0, 0, w, h);
            bg.addColorStop(0, `rgba(${8 + warm * 18 | 0}, ${14 + warm * 8 | 0}, ${28 - warm * 10 | 0}, 1)`);
            bg.addColorStop(1, `rgba(4, 8, 18, 1)`);
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Progress bloom — fills from left, glows teal→gold based on completion
            const sweep = Math.max(0.04, completion) * w;
            const pulse = 0.5 + Math.sin(t * 1.4) * 0.12;
            const r = Math.round(20 + completion * 220);
            const g = Math.round(184 - completion * 80);
            const b = Math.round(166 - completion * 120);
            const bloom = ctx.createLinearGradient(0, 0, sweep * 1.4, 0);
            bloom.addColorStop(0,   `rgba(${r},${g},${b},${0.28 + pulse * 0.12})`);
            bloom.addColorStop(0.6, `rgba(${r},${g},${b},${0.10})`);
            bloom.addColorStop(1,   `rgba(${r},${g},${b},0)`);
            ctx.fillStyle = bloom;
            ctx.fillRect(0, 0, Math.min(w, sweep * 1.8), h);
            // Edge glow at sweep front
            if (completion > 0.02 && completion < 0.99) {
                const edge = ctx.createRadialGradient(sweep, h * 0.5, 0, sweep, h * 0.5, h * 0.55);
                edge.addColorStop(0,   `rgba(${r},${g},${b},${0.22 + pulse * 0.1})`);
                edge.addColorStop(1,   `rgba(${r},${g},${b},0)`);
                ctx.fillStyle = edge;
                ctx.fillRect(0, 0, w, h);
            }
            // Subtle horizontal scan lines
            ctx.strokeStyle = 'rgba(255,255,255,0.025)';
            ctx.lineWidth = 1;
            for (let y = 0; y < h; y += 32) {
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            }
            // Vignette
            const vig = ctx.createRadialGradient(w * 0.5, h * 0.5, h * 0.2, w * 0.5, h * 0.5, w * 0.75);
            vig.addColorStop(0, 'rgba(0,0,0,0)');
            vig.addColorStop(1, 'rgba(0,0,0,0.5)');
            ctx.fillStyle = vig;
            ctx.fillRect(0, 0, w, h);
        };
    },

    makeExpensesRenderer(canvas) {
        const totalVal = Math.max(0, Number(canvas.dataset.total || 0));
        const entries  = Math.max(1, Number(canvas.dataset.items || 1));
        const barCount = Math.max(12, Math.min(28, entries * 2));
        const intensity = Math.min(1, totalVal / 2000);
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.016;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);
            // Deep green-black base
            ctx.fillStyle = 'rgba(4, 14, 10, 1)';
            ctx.fillRect(0, 0, w, h);
            // Radial glow center-bottom
            const glow = ctx.createRadialGradient(w * 0.5, h, 0, w * 0.5, h * 0.6, w * 0.7);
            glow.addColorStop(0, `rgba(16, 158, 129, ${0.18 + intensity * 0.22})`);
            glow.addColorStop(1, 'rgba(16, 158, 129, 0)');
            ctx.fillStyle = glow;
            ctx.fillRect(0, 0, w, h);
            // Animated bars rising from bottom
            const bw = (w - 16) / (barCount * 1.5);
            for (let i = 0; i < barCount; i++) {
                const x = 8 + i * bw * 1.5;
                const signal = (Math.sin(t * 0.8 + i * 0.42) + 1) / 2;
                const bh = h * (0.08 + signal * (0.55 + intensity * 0.3));
                const alpha = 0.15 + signal * (0.3 + intensity * 0.2);
                const g2 = ctx.createLinearGradient(0, h - bh, 0, h);
                g2.addColorStop(0, `rgba(20, 200, 160, ${alpha})`);
                g2.addColorStop(1, `rgba(8, 120, 90, ${alpha * 0.4})`);
                ctx.fillStyle = g2;
                ctx.fillRect(x, h - bh, bw, bh);
            }
            // Vignette
            const vig = ctx.createRadialGradient(w * 0.5, h * 0.4, h * 0.1, w * 0.5, h * 0.4, w * 0.8);
            vig.addColorStop(0, 'rgba(0,0,0,0)');
            vig.addColorStop(1, 'rgba(0,0,0,0.6)');
            ctx.fillStyle = vig;
            ctx.fillRect(0, 0, w, h);
        };
    },

    makeNotesRenderer(canvas) {
        const count = Math.max(1, Number(canvas.dataset.count || 1));
        // Slow-drifting particles — more particles = more notes
        const particles = Array.from({ length: Math.min(60, 18 + count * 4) }, (_, i) => ({
            x: ((i * 37 + 11) % 997) / 997,
            y: ((i * 71 + 23) % 991) / 991,
            r: 0.8 + ((i * 13) % 10) / 10 * 2.2,
            p: i * 0.31,
            speed: 0.006 + ((i * 7) % 10) / 10 * 0.006,
        }));
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.008;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);
            // Deep indigo base
            ctx.fillStyle = 'rgba(6, 8, 22, 1)';
            ctx.fillRect(0, 0, w, h);
            // Ambient glow
            const amb = ctx.createRadialGradient(w * 0.35, h * 0.4, 0, w * 0.35, h * 0.4, w * 0.65);
            amb.addColorStop(0, 'rgba(80, 60, 200, 0.20)');
            amb.addColorStop(1, 'rgba(80, 60, 200, 0)');
            ctx.fillStyle = amb;
            ctx.fillRect(0, 0, w, h);
            // Particles — slow gentle drift
            particles.forEach((p, idx) => {
                const x = ((p.x * w + Math.sin(t * p.speed * 60 + p.p) * 22) % (w + 30) + w + 30) % (w + 30);
                const y = ((p.y * h + Math.cos(t * p.speed * 55 + p.p) * 16) % (h + 30) + h + 30) % (h + 30);
                const alpha = 0.15 + ((Math.sin(t * 1.2 + idx * 0.6) + 1) / 2) * 0.35;
                ctx.fillStyle = `rgba(140, 120, 255, ${alpha})`;
                ctx.beginPath();
                ctx.arc(x, y, p.r, 0, Math.PI * 2);
                ctx.fill();
            });
            // Vignette
            const vig = ctx.createRadialGradient(w * 0.5, h * 0.45, h * 0.15, w * 0.5, h * 0.45, w * 0.75);
            vig.addColorStop(0, 'rgba(0,0,0,0)');
            vig.addColorStop(1, 'rgba(0,0,0,0.65)');
            ctx.fillStyle = vig;
            ctx.fillRect(0, 0, w, h);
        };
    },

    makeGraphRenderer(canvas) {
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.009;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);
            const bg = ctx.createLinearGradient(0, 0, w, h);
            bg.addColorStop(0, 'rgba(13, 94, 123, 0.17)');
            bg.addColorStop(1, 'rgba(13, 94, 123, 0.02)');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            ctx.strokeStyle = 'rgba(13, 94, 123, 0.12)';
            ctx.lineWidth = 1;
            for (let i = 0; i <= 16; i += 1) {
                const x = (i / 16) * w;
                const yOff = Math.sin(t + i * 0.45) * 4;
                ctx.beginPath();
                ctx.moveTo(x, 0 + yOff);
                ctx.lineTo(x, h + yOff);
                ctx.stroke();
            }
        };
    },

    makeWebdeckRenderer(canvas) {
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.01;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);

            const bg = ctx.createLinearGradient(0, 0, w, h);
            bg.addColorStop(0, 'rgba(21, 36, 60, 0.28)');
            bg.addColorStop(1, 'rgba(21, 36, 60, 0.06)');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);

            const glowA = ctx.createRadialGradient(w * 0.2, h * 0.25, 0, w * 0.2, h * 0.25, Math.min(w, h) * 0.55);
            glowA.addColorStop(0, 'rgba(92, 160, 255, 0.22)');
            glowA.addColorStop(1, 'rgba(92, 160, 255, 0)');
            ctx.fillStyle = glowA;
            ctx.fillRect(0, 0, w, h);

            const glowB = ctx.createRadialGradient(w * 0.82, h * 0.7, 0, w * 0.82, h * 0.7, Math.min(w, h) * 0.5);
            glowB.addColorStop(0, 'rgba(53, 209, 184, 0.16)');
            glowB.addColorStop(1, 'rgba(53, 209, 184, 0)');
            ctx.fillStyle = glowB;
            ctx.fillRect(0, 0, w, h);

            ctx.strokeStyle = 'rgba(173, 207, 255, 0.12)';
            ctx.lineWidth = 1;
            const gap = 26;
            for (let x = -gap; x <= w + gap; x += gap) {
                const yDrift = Math.sin(t + x * 0.014) * 4;
                ctx.beginPath();
                ctx.moveTo(x, 0 + yDrift);
                ctx.lineTo(x, h + yDrift);
                ctx.stroke();
            }
        };
    },

    conditionIcon(condition) {
        const c = String(condition || '').toLowerCase();
        if (c.includes('thunder') || c.includes('storm')) return '⛈';
        if (c.includes('rain') || c.includes('drizzle') || c.includes('shower')) return '🌧';
        if (c.includes('snow') || c.includes('sleet') || c.includes('ice')) return '❄';
        if (c.includes('fog') || c.includes('haze') || c.includes('mist')) return '🌫';
        if (c.includes('overcast')) return '☁';
        if (c.includes('cloud')) return '⛅';
        if (c.includes('clear') || c.includes('sun')) return '☀';
        return '🌤';
    },

    weatherSeed(text) {
        const value = String(text || '').toLowerCase();
        let hash = 2166136261;
        for (let i = 0; i < value.length; i += 1) {
            hash ^= value.charCodeAt(i);
            hash = Math.imul(hash, 16777619);
        }
        return Math.abs(hash >>> 0);
    },

    buildWeatherForecastPoints(info) {
        const live = Array.isArray(info?.forecast) ? info.forecast : [];
        if (live.length >= 4) {
            return live.slice(0, 8).map((item, idx) => ({
                hour: String(item.hourLabel || `+${idx}h`),
                temp: Number(item.tempF || 0),
                precip: Number(item.precipChance || 0),
                wind: Number(item.windMph || 0),
            }));
        }
        const location = String(info.location || 'weather').trim();
        const condition = String(info.condition || '').trim().toLowerCase();
        const baseTemp = Number(String(info.temperature || '').replace(/[^0-9.-]/g, '')) || 60;
        const seed = this.weatherSeed(`${location}|${condition}|${baseTemp}`);
        const conditionBias = condition.includes('rain') ? -3 : condition.includes('snow') ? -6 : condition.includes('sun') || condition.includes('clear') ? 4 : 0;
        const points = [];
        for (let i = 0; i < 8; i += 1) {
            const jitter = ((seed >> (i * 4)) & 0xf) - 7;
            const wave = Math.sin((i / 5) * Math.PI) * 3;
            const temp = Math.round(baseTemp + conditionBias + wave + jitter * 0.35);
            const precip = Math.max(0, Math.min(100, (condition.includes('rain') ? 40 : 16) + jitter * 3 + i * 2));
            const wind = Math.max(0, 4 + (jitter * 0.4) + (i * 0.2));
            points.push({ hour: `${i * 2}h`, temp, precip, wind });
        }
        return points;
    },

    buildWeatherForecastVisual(info) {
        const points = this.buildWeatherForecastPoints(info);
        if (!points.length) return '';
        const min = Math.min(...points.map((p) => p.temp));
        const max = Math.max(...points.map((p) => p.temp));
        const span = Math.max(1, max - min);
        const maxWind = Math.max(1, ...points.map((p) => Number(p.wind || 0)));
        const width = 560;
        const height = 176;
        const padX = 12;
        const padY = 16;
        const step = (width - padX * 2) / (points.length - 1);
        const toY = (temp) => {
            const norm = (temp - min) / span;
            return Math.round(height - padY - norm * (height - padY * 2));
        };
        const toWindY = (wind) => {
            const norm = Number(wind || 0) / maxWind;
            return Math.round(height - padY - norm * (height - padY * 2));
        };
        const path = points
            .map((p, idx) => `${idx === 0 ? 'M' : 'L'} ${Math.round(padX + idx * step)} ${toY(p.temp)}`)
            .join(' ');
        const windPath = points
            .map((p, idx) => `${idx === 0 ? 'M' : 'L'} ${Math.round(padX + idx * step)} ${toWindY(p.wind)}`)
            .join(' ');
        const area = `${path} L ${Math.round(width - padX)} ${height - padY} L ${padX} ${height - padY} Z`;
        const bars = points.map((p, idx) => {
            const x = Math.round(padX + idx * step - 9);
            const h = Math.max(2, Math.round((Math.max(0, Number(p.precip || 0)) / 100) * (height - padY * 2)));
            const y = Math.round(height - padY - h);
            return `<rect class="forecast-bar" x="${x}" y="${y}" width="18" height="${h}" rx="4"></rect>`;
        }).join('');
        const labels = points.map((p) => `<div class="forecast-tick">${escapeHtml(p.hour)}</div>`).join('');
        const chips = points
            .map((p) => `<div class="forecast-chip">${escapeHtml(String(p.temp))}F</div>`)
            .join('');
        return `
            <div class="forecast-panel">
                <div class="forecast-head">12h forecast</div>
                <svg class="forecast-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-label="Forecast trend">
                    ${bars}
                    <path class="forecast-area" d="${area}"></path>
                    <path class="forecast-line" d="${path}"></path>
                    <path class="forecast-wind" d="${windPath}"></path>
                </svg>
                <div class="forecast-labels">${labels}</div>
                <div class="forecast-chips">${chips}</div>
                <div class="forecast-range">low ${min}F | high ${max}F | wind max ${Math.round(maxWind)} mph</div>
            </div>
        `;
    },

    buildHandoffItems(runtimeTrace) {
        const runtime = runtimeTrace || {};
        const handoff = runtime.handoff || this.state.session.handoff || {};
        const active = String(handoff.activeDeviceId || '-');
        const pending = handoff.pending && typeof handoff.pending === 'object' ? handoff.pending : null;
        const lines = [`active: ${active}`];
        if (pending) {
            const token = String(pending.token || '').trim();
            const eta = this.formatEta(pending.expiresAt);
            lines.push(`pending: ${token || '-'}`);
            lines.push(`expires: ${eta}`);
            if (token) lines.push({ text: `claim handoff ${token}`, command: `claim handoff ${token}` });
        } else {
            lines.push('pending: none');
        }
        lines.push({ text: 'start handoff', command: 'start handoff' });
        return lines.slice(0, 5);
    },

    buildRuntimeJobItems(runtimeTrace) {
        const runtime = runtimeTrace || {};
        const preview = Array.isArray(runtime.jobsPreview) ? runtime.jobsPreview : [];
        const deadLetters = Number(runtime?.deadLetters?.count || 0);
        const lines = preview.flatMap((job, index) => {
            const state = job?.active ? 'active' : 'paused';
            const eta = this.formatEta(job?.nextRunAt);
            const failures = Number(job?.failureCount || 0);
            const head = `${index + 1}. ${state} ${job?.kind || 'job'} (${job?.intervalMinutes || 0}m) ${eta}${failures ? ` | fail:${failures}` : ''}`;
            const detail = String(job?.lastResult || job?.lastError || '').trim();
            return detail ? [head, `last: ${detail.slice(0, 80)}`] : [head];
        });
        if (deadLetters > 0) {
            lines.unshift(`dead letters: ${deadLetters} (show dead letters)`);
        }
        if (!lines.length) {
            return ['No active jobs. Use: watch task 1 every 10m'];
        }
        return lines.slice(0, 6);
    },

    buildPerformanceItems(runtimeTrace) {
        const perf = runtimeTrace?.performance || null;
        if (!perf) return ['No turn timing yet.'];
        const total = Number(perf.totalMs || 0);
        const budget = Number(perf.budgetMs || 0);
        const ok = Boolean(perf.withinBudget);
        return [
            `total: ${total}ms`,
            `budget: ${budget}ms`,
            `status: ${ok ? 'within' : 'over'}`,
            `parse: ${Number(perf.parseMs || 0)}ms | execute: ${Number(perf.executeMs || 0)}ms | plan: ${Number(perf.planMs || 0)}ms`
        ];
    },

    buildSloItems(runtimeTrace) {
        const slo = runtimeTrace?.slo || null;
        if (!slo) return ['No SLO signal yet.'];
        const throttled = Boolean(slo.throttled);
        return [
            `breach streak: ${Number(slo.breachStreak || 0)}`,
            `throttled: ${throttled ? 'yes' : 'no'}`,
            `last total: ${Number(slo.lastTotalMs || 0)}ms`,
            `alerts: ${Array.isArray(slo.alerts) ? slo.alerts.length : 0}`
        ];
    },

    buildRestoreItems(runtimeTrace) {
        const restore = runtimeTrace?.restore || null;
        if (!restore || typeof restore !== 'object') return ['No restore actions yet.'];
        const source = String(restore.source || 'unknown');
        const ts = Number(restore.ts || 0);
        const at = ts ? new Date(ts).toLocaleTimeString() : '-';
        const lines = [`source: ${source}`, `time: ${at}`];
        if (restore.checkpointId) lines.push(`checkpoint: ${String(restore.checkpointId)}`);
        if (typeof restore.entriesReplayed !== 'undefined') lines.push(`entries: ${Number(restore.entriesReplayed)}`);
        if (typeof restore.journalBase !== 'undefined') lines.push(`journal base: ${Number(restore.journalBase)}`);
        return lines.slice(0, 5);
    },

    buildFaultItems(runtimeTrace) {
        const persist = runtimeTrace?.faults?.persist || null;
        if (!persist) return ['No fault signal yet.'];
        return [
            `persist degraded: ${persist.degraded ? 'yes' : 'no'}`,
            `pending writes: ${Number(persist.pendingWrites || 0)}`,
            `simulation: ${persist.simulation ? 'on' : 'off'}`,
            `error: ${String(persist.lastError || '-').slice(0, 80)}`
        ];
    },

    formatEta(nextRunAt) {
        const ts = Number(nextRunAt || 0);
        if (!ts) return 'eta: -';
        const deltaMs = ts - Date.now();
        if (deltaMs <= 0) return 'eta: due';
        const mins = Math.ceil(deltaMs / 60000);
        return `eta: ${mins}m`;
    },

    buildGraphContextItems(graphTrace, runtimeTrace) {
        const g = graphTrace || {};
        const runtime = runtimeTrace || {};
        const relationKinds = g.relationKinds && typeof g.relationKinds === 'object' ? g.relationKinds : {};
        const kindLines = Object.entries(relationKinds)
            .sort((a, b) => Number(b[1]) - Number(a[1]))
            .slice(0, 2)
            .map(([kind, count]) => `${kind}: ${count}`);
        const recent = Array.isArray(g.recentRelationEvents) ? g.recentRelationEvents.slice(-2).reverse() : [];
        const recentLines = recent.map((event) => {
            const payload = event?.payload || {};
            const relation = payload.relation || 'link';
            const source = String(payload.sourceId || '').slice(0, 6);
            const target = String(payload.targetId || '').slice(0, 6);
            return `${relation} ${source}->${target}`;
        });

        const counts = [
            `entities: ${Number(g.entities || 0)}`,
            `relations: ${Number(g.relations || 0)}`,
            `events: ${Number(g.events || 0)}`,
            `jobs: ${Number(runtime.jobsActive || 0)}`
        ];

        return [...counts, ...kindLines, ...recentLines].slice(0, 6);
    },

    extractConfirmationCommand(message) {
        const text = String(message || '');
        const match = text.match(/Try:\s*([a-z0-9 _-]+)/i);
        if (!match) return '';
        return match[1].trim();
    },

    extractTryCommand(message) {
        const text = String(message || '');
        const match = text.match(/Try:\s*([^\n\r]+)/i);
        if (!match) return '';
        return String(match[1] || '').trim();
    },

    deriveKernelTrace(execution, route) {
        const toolResults = Array.isArray(execution?.toolResults) ? execution.toolResults : [];
        const diff = { tasks: 0, expenses: 0, notes: 0 };
        for (const item of toolResults) {
            diff.tasks += Number(item?.diff?.tasks || 0);
            diff.expenses += Number(item?.diff?.expenses || 0);
            diff.notes += Number(item?.diff?.notes || 0);
        }

        return {
            route: {
                target: route?.target || 'deterministic',
                reason: route?.reason || 'local',
                model: route?.model || null,
                intentClass: route?.intentClass || 'local_fallback',
                confidence: Number(route?.confidence || 0.5)
            },
            policy: {
                allAllowed: Boolean(execution?.ok),
                codes: toolResults.map((item) => String(item?.policy?.code || (item?.ok ? 'ok' : 'unknown')))
            },
            diff,
            graph: {
                entities: 0,
                relations: 0,
                events: 0,
                relationKinds: {},
                recentRelationEvents: []
            },
            runtime: {
                jobsActive: 0,
                nextRunAt: null,
                jobsPreview: [],
                performance: {
                    parseMs: 0,
                    executeMs: 0,
                    planMs: 0,
                    totalMs: Math.max(0, Number(this.state.metrics.latency || 0)),
                    budgetMs: 800,
                    withinBudget: true
                },
                slo: {
                    breachStreak: 0,
                    throttleUntil: 0,
                    throttled: false,
                    lastTotalMs: Math.max(0, Number(this.state.metrics.latency || 0)),
                    alerts: []
                },
                restore: null,
                faults: {
                    persist: {
                        degraded: false,
                        simulation: false,
                        lastError: '',
                        lastFailureAt: 0,
                        lastSuccessAt: 0
                    }
                },
                handoff: this.state.session.handoff || { activeDeviceId: null, pending: null, lastClaimAt: null }
            },
            journalTail: toolResults.map((item) => ({
                ok: Boolean(item?.ok),
                op: item?.op || 'unknown',
                diff: item?.diff || { tasks: 0, expenses: 0, notes: 0 }
            }))
        };
    },

    renderFeedBlock(block) {
        if (!block) return '';

        if (block.type === 'metric') {
            return `
                <div class="feed-card">
                    <div class="surface-label">${escapeHtml(block.label || 'Metric')}</div>
                    <div class="feed-value" style="color:${safeCssColor(block.color)}">${escapeHtml(String(block.value ?? ''))}</div>
                    ${block.meta ? `<div class="feed-meta">${escapeHtml(block.meta)}</div>` : ''}
                </div>
            `;
        }

        if (block.type === 'list') {
            const items = Array.isArray(block.items) ? block.items.slice(0, 5) : [];
            return `
                <div class="feed-card">
                    <div class="surface-label">${escapeHtml(block.label || 'List')}</div>
                    <div class="feed-lines">
                        ${items.length ? items.map((item) => this.renderFeedLine(item)).join('') : '<div class="feed-meta">No entries.</div>'}
                    </div>
                </div>
            `;
        }

        if (block.type === 'table') {
            const rows = Array.isArray(block.rows) ? block.rows.length : 0;
            return `
                <div class="feed-card">
                    <div class="surface-label">${escapeHtml(block.label || 'Table')}</div>
                    <div class="feed-value">${rows}</div>
                    <div class="feed-meta">rows</div>
                </div>
            `;
        }

        return `
            <div class="feed-card">
                <div class="surface-label">${escapeHtml(block.label || 'Signal')}</div>
                <div class="feed-meta">${escapeHtml(block.text || block.value || '')}</div>
            </div>
        `;
    },

    renderFeedLine(item) {
        if (item && typeof item === 'object' && item.command) {
            const command = String(item.command).trim();
            const text = String(item.text || command);
            return `<button class="feed-line command-link" data-command="${escapeAttr(command)}">${escapeHtml(text)}</button>`;
        }
        return `<div class="feed-line">${escapeHtml(String(item))}</div>`;
    },

    selectPrimaryBlock(blocks) {
        if (!Array.isArray(blocks) || blocks.length === 0) {
            return { type: 'narrative', label: 'Workspace', text: 'Ready for intent.' };
        }
        return blocks.find((b) => b.type === 'list' || b.type === 'table')
            || blocks.find((b) => b.type === 'narrative')
            || blocks[0];
    },

    renderPrimaryBlock(block) {
        if (!block) return '';
        if (block.type === 'list') {
            const items = Array.isArray(block.items) ? block.items : [];
            return `
                <div class="surface-panel">
                    <div class="surface-label">${escapeHtml(block.label || 'Surface')}</div>
                    <div class="surface-stream">
                        ${items.length ? items.map((item) => `<div class="surface-row">${escapeHtml(String(item))}</div>`).join('') : '<div class="surface-row muted">No active entries.</div>'}
                    </div>
                </div>
            `;
        }

        if (block.type === 'table') {
            const headers = Array.isArray(block.headers) ? block.headers : [];
            const rows = Array.isArray(block.rows) ? block.rows : [];
            return `
                <div class="surface-panel">
                    <div class="surface-label">${escapeHtml(block.label || 'Surface')}</div>
                    <div class="table-container">
                        <table>
                            <thead><tr>${headers.map((h) => `<th>${escapeHtml(String(h))}</th>`).join('')}</tr></thead>
                            <tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(String(cell))}</td>`).join('')}</tr>`).join('')}</tbody>
                        </table>
                    </div>
                </div>
            `;
        }

        return `
            <div class="surface-panel">
                <div class="surface-label">${escapeHtml(block.label || 'Surface')}</div>
                <div class="surface-stream">
                    <div class="surface-row">${escapeHtml(block.text || block.value || 'Ready')}</div>
                </div>
            </div>
        `;
    },

    renderCompactBlock(block) {
        if (block.type === 'metric') {
            return `
                <div class="compact-card">
                    <div class="label">${escapeHtml(block.label || '')}</div>
                    <div class="value compact-value" style="color:${safeCssColor(block.color)}">${escapeHtml(String(block.value ?? ''))}</div>
                    ${block.meta ? `<div class="meta-line">${escapeHtml(block.meta)}</div>` : ''}
                </div>
            `;
        }

        if (block.type === 'narrative') {
            return `
                <div class="compact-card">
                    <div class="label">${escapeHtml(block.label || 'Signal')}</div>
                    <div class="meta-line">${escapeHtml(block.text || '')}</div>
                </div>
            `;
        }

        const count = block.type === 'list'
            ? (Array.isArray(block.items) ? block.items.length : 0)
            : (Array.isArray(block.rows) ? block.rows.length : 0);
        return `
            <div class="compact-card">
                <div class="label">${escapeHtml(block.label || 'Signal')}</div>
                <div class="value compact-value">${count}</div>
            </div>
        `;
    },

    renderBlock(block) {
        if (block.type === 'metric') {
            return `
                <div class="card ${block.span === 2 ? 'card-span-2' : ''}">
                    <div class="label">${escapeHtml(block.label || '')}</div>
                    <div class="value" style="color:${safeCssColor(block.color)}">${escapeHtml(String(block.value ?? ''))}</div>
                    ${block.meta ? `<div class="meta-line">${escapeHtml(block.meta)}</div>` : ''}
                </div>
            `;
        }

        if (block.type === 'list') {
            const items = Array.isArray(block.items) ? block.items : [];
            return `
                <div class="card ${block.span === 2 ? 'card-span-2' : ''}">
                    <div class="label">${escapeHtml(block.label || '')}</div>
                    <div class="list-wrap">
                        ${items.length ? items.map((item) => `<div class="list-row">${escapeHtml(String(item))}</div>`).join('') : '<div class="muted">No items</div>'}
                    </div>
                </div>
            `;
        }

        if (block.type === 'table') {
            const headers = Array.isArray(block.headers) ? block.headers : [];
            const rows = Array.isArray(block.rows) ? block.rows : [];
            return `
                <div class="card card-span-2">
                    <div class="label" style="margin-bottom: 16px">${escapeHtml(block.label || '')}</div>
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr>${headers.map((header) => `<th>${escapeHtml(String(header))}</th>`).join('')}</tr>
                            </thead>
                            <tbody>
                                ${rows.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(String(cell))}</td>`).join('')}</tr>`).join('')}
                            </tbody>
                        </table>
                    </div>
                </div>
            `;
        }

        if (block.type === 'narrative') {
            return `
                <div class="card card-span-2">
                    <div class="label">${escapeHtml(block.label || 'Narrative')}</div>
                    <div class="list-wrap"><div class="list-row">${escapeHtml(block.text || '')}</div></div>
                </div>
            `;
        }

        return '';
    },

    pushHistory(intent, envelope, execution, plan, plannerSource = 'local', kernelTrace = null, merge = null) {
        const entry = {
            intent,
            summary: execution?.message || plan?.subtitle || '',
            timestamp: Date.now(),
            envelope,
            executionSnapshot: execution ? JSON.parse(JSON.stringify(execution)) : null,
            plan,
            kernelTrace,
            merge,
            plannerSource,
            memorySnapshot: JSON.parse(JSON.stringify(this.state.memory)),
            thumbnail: null
        };
        this.state.history.push(entry);

        if (this.state.history.length > HISTORY_LIMIT) this.state.history.shift();
        this.state.session.activeHistoryIndex = this.state.history.length - 1;
        this.updateHistoryReel();

        // Async thumbnail: capture canvas after animation settles
        setTimeout(() => {
            const thumb = this._captureThumbnail();
            if (thumb) {
                entry.thumbnail = thumb;
                this.updateHistoryReel();
            }
        }, 320);
    },

    stepHistory(delta) {
        const size = this.state.history.length;
        if (!size) return;
        const current = Number.isInteger(this.state.session.activeHistoryIndex)
            ? this.state.session.activeHistoryIndex
            : (size - 1);
        const target = Math.max(0, Math.min(size - 1, current + Number(delta || 0)));
        if (target === current) return;
        this.restoreFromHistory(target);
    },

    executeQuickCommand(index = 0) {
        const roots = [
            this.container.querySelector('.workspace-main'),
            this.container.querySelector('.workspace-side'),
        ].filter(Boolean);
        const commands = [];
        for (const root of roots) {
            const nodes = Array.from(root.querySelectorAll('[data-command]'));
            for (const node of nodes) {
                const cmd = String(node.getAttribute('data-command') || '').trim();
                if (cmd) commands.push(cmd);
            }
        }
        const cmd = commands[Math.max(0, Number(index || 0))];
        if (!cmd) return;
        this.input.value = cmd;
        this.input.focus();
        this.handleIntent(cmd);
    },

    showHistoryReel() {
        this.updateHistoryReel();
        this.historyReel.classList.add('reel-visible');
    },

    hideHistoryReel() {
        this.historyReel.classList.remove('reel-visible');
    },

    updateHistoryReel() {
        this.historyReel.innerHTML = this.state.history.map((entry, index) => {
            const isActive = index === this.state.session.activeHistoryIndex;
            const inner = entry.thumbnail
                ? `<img src="${entry.thumbnail}" alt="" />`
                : `<div class="history-node-placeholder">${escapeHtml(String(entry.intent || '').slice(0, 6))}</div>`;
            return `<button class="history-node${isActive ? ' active' : ''}" data-history-index="${index}" title="${escapeAttr(entry.summary || entry.intent)}">
                ${inner}
                <div class="history-node-label">${escapeHtml(String(entry.intent || '').slice(0, 32))}</div>
            </button>`;
        }).join('');
    },

    _captureThumbnail() {
        // For functional surfaces the canvas is hidden — use a solid color placeholder via bg
        const canvas = this.container.querySelector('canvas.scene-canvas');
        if (!canvas || canvas.offsetParent === null) return null; // hidden or detached
        try {
            if (canvas.width === 0 || canvas.height === 0) return null;
            return canvas.toDataURL('image/jpeg', 0.4);
        } catch (_) { return null; }
    },

    restoreFromHistory(index) {
        const entry = this.state.history[index];
        if (!entry) return;

        this.markUserEngaged();
        if (!entry.envelope || !entry.plan) {
            const replayIntent = String(entry.intent || '').trim();
            if (!replayIntent) return;
            this.updateStatus('RESTORING');
            this.showToast('Replaying preserved history trace.', 'info', 2200);
            this.input.value = replayIntent;
            this.input.focus();
            this.handleIntent(replayIntent);
            return;
        }
        this.state.memory = JSON.parse(JSON.stringify(entry.memorySnapshot));
        this.state.session.activeHistoryIndex = index;
        this.state.session.lastIntent = entry.intent;
        this.state.session.lastEnvelope = entry.envelope;
        this.state.session.lastKernelTrace = entry.kernelTrace || null;
        this.state.session.lastExecution = entry.executionSnapshot || null;

        const plan = UIPlanSchema.normalize(entry.plan || UIPlanner.build(entry.envelope, this.state.memory, { ok: true, message: 'History restore' }));
        this.render(plan, entry.envelope, this.state.session.lastKernelTrace);

        this.updateHistoryReel();
        this.updateStatus('RESTORED');
        this.saveState();
    },

    refreshSurface() {
        const prior = this.state.history[this.state.history.length - 1] || null;
        if (prior?.plan && prior?.envelope) {
            const plan = UIPlanSchema.normalize(prior.plan);
            this.render(plan, prior.envelope, this.state.session.lastKernelTrace);
            return;
        }
        const seedIntent = this.state.session.lastIntent || 'Show me what I can do';
        const envelope = IntentLayerCompiler.compile(seedIntent, this.state.memory);
        const plan = UIPlanSchema.normalize(UIPlanner.build(envelope, this.state.memory, { ok: true, message: 'Surface online.' }));
        this.render(plan, envelope, this.state.session.lastKernelTrace);
    },

    loadState() {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return;

        try {
            const parsed = JSON.parse(raw);
            this.state.memory = parsed.memory || safeStructuredClone(DEFAULT_MEMORY);
            this.state.history = parsed.history || [];
            this.state.intentHistory = Array.isArray(parsed.intentHistory)
                ? parsed.intentHistory.filter((x) => typeof x === 'string').slice(-60)
                : [];
            this.state.intentHistoryIndex = this.state.intentHistory.length;
            this.state.session.sessionId = parsed.sessionId || localStorage.getItem(SESSION_STORAGE_KEY) || '';
            this.state.session.deviceId = parsed.deviceId || localStorage.getItem(DEVICE_STORAGE_KEY) || '';
            this.state.session.handoff = parsed.handoff || this.state.session.handoff;
            this.state.session.presence = parsed.presence || this.state.session.presence;
            this.state.session.workspace = parsed.workspace || this.state.session.workspace;
            this.state.session.revision = Number(parsed.revision || 0);
            this.state.session.locationHint = String(parsed.locationHint || '').trim();
            this.state.runtimeEvents = Array.isArray(parsed.runtimeEvents)
                ? parsed.runtimeEvents.filter((item) => item && typeof item === 'object').slice(-20)
                : [];
            const mode = String(parsed?.uiPrefs?.webdeckMode || '').toLowerCase();
            if (mode === 'surface' || mode === 'full') {
                this.state.webdeck.mode = mode;
            }
        } catch {
            this.state.memory = safeStructuredClone(DEFAULT_MEMORY);
            this.state.history = [];
            this.state.intentHistory = [];
            this.state.intentHistoryIndex = -1;
        }
    },

    saveState() {
        this.syncActiveSurfaceWorkspaceFocus();
        localStorage.setItem(STORAGE_KEY, JSON.stringify({
            memory: this.state.memory,
            history: this.state.history,
            intentHistory: this.state.intentHistory,
            sessionId: this.state.session.sessionId,
            deviceId: this.state.session.deviceId,
            handoff: this.state.session.handoff,
            presence: this.state.session.presence,
            workspace: this.state.session.workspace,
            revision: this.state.session.revision,
            locationHint: this.state.session.locationHint,
            runtimeEvents: this.state.runtimeEvents,
            uiPrefs: {
                webdeckMode: this.state.webdeck.mode
            }
        }));
        if (this.state.session.sessionId) {
            localStorage.setItem(SESSION_STORAGE_KEY, this.state.session.sessionId);
        }
    },

    updateStatus(mode) {
        this.state.session.statusMode = String(mode || 'READY');
        const m = this.state.memory;
        const objectCount = m.tasks.length + m.expenses.length + m.notes.length;
        const sid = this.state.session.sessionId ? this.state.session.sessionId.slice(0, 8) : '-';
        const sync = this.state.session.syncTransport.toUpperCase();
        const net = this.state.session.networkOnline ? 'ONLINE' : 'OFFLINE';
        const retry = Number(this.state.session.reconnectAttempts || 0);
        const retryPart = retry > 0 && !this.state.session.networkOnline ? ` | RETRY: ${retry}` : '';
        this.status.innerText = `MODE: ${this.state.session.statusMode} | SYNC: ${sync} | NET: ${net}${retryPart} | SESSION: ${sid} | OBJECTS: ${objectCount} | LATENCY: ${this.state.metrics.latency}ms | ENTROPY: ${this.state.metrics.entropy.toFixed(3)}`;
        this.updateShortcutHint();
    },

    updateShortcutHint() {
        if (!this.shortcutHint) return;
        const isTouchLike = typeof window !== 'undefined'
            && ((window.matchMedia && window.matchMedia('(pointer: coarse)').matches) || window.innerWidth <= 900);
        if (isTouchLike) {
            this.shortcutHint.innerText = 'Top-edge swipe: scene dock | Swipe left/right: history';
            return;
        }
        this.shortcutHint.innerText = 'Alt+1..9 run | Alt+<-/-> history | Alt+Shift+<-/-> scenes | Alt+M webdeck mode | Ctrl/Cmd+K focus';
    },

    isStandalonePwa() {
        return !!(
            (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)
            || window.navigator.standalone
        );
    },

    isPhoneSurface() {
        return typeof window !== 'undefined'
            && ((window.matchMedia && window.matchMedia('(pointer: coarse)').matches) || window.innerWidth <= 900);
    },

    setupPwaRuntime() {
        this.state.pwa.installed = this.isStandalonePwa();
        this.state.pwa.installReady = false;
        if (typeof window === 'undefined' || isElectronRuntime()) return;
        window.addEventListener('beforeinstallprompt', (event) => {
            event.preventDefault();
            this._deferredInstallPrompt = event;
            this.state.pwa.installReady = true;
            this.renderPwaInstallPrompt();
        });
        window.addEventListener('appinstalled', () => {
            this._deferredInstallPrompt = null;
            this.state.pwa.installReady = false;
            this.state.pwa.installed = true;
            this.renderPwaInstallPrompt();
            this.showToast('Genome installed on this screen.', 'ok', 2600);
        });
        this.renderPwaInstallPrompt();
    },

    renderPwaInstallPrompt() {
        if (typeof document === 'undefined') return;
        document.getElementById('genome-pwa-install')?.remove();
        if (isElectronRuntime() || this.state.pwa.installed || !this.isPhoneSurface()) return;
        const wrapper = document.createElement('div');
        wrapper.id = 'genome-pwa-install';
        wrapper.style.cssText = 'position:fixed;top:16px;right:16px;z-index:1200;display:flex;gap:8px;align-items:center;padding:10px 12px;border-radius:16px;background:rgba(8,12,20,.88);border:1px solid rgba(90,214,190,.35);box-shadow:0 12px 40px rgba(0,0,0,.28);backdrop-filter:blur(14px);';
        wrapper.innerHTML = `
            <div style="font:600 12px var(--font-mono);letter-spacing:.08em;color:#9fd7c5;">GENOME PHONE SURFACE</div>
            <button type="button" id="genome-pwa-install-btn" style="border:0;border-radius:999px;padding:8px 12px;background:#0d7c66;color:#f7fffb;font:600 12px var(--font-mono);">${this.state.pwa.installReady ? 'install' : 'share → add to home screen'}</button>
        `;
        document.body.appendChild(wrapper);
        wrapper.querySelector('#genome-pwa-install-btn')?.addEventListener('click', async () => {
            if (this._deferredInstallPrompt) {
                const prompt = this._deferredInstallPrompt;
                this._deferredInstallPrompt = null;
                await prompt.prompt();
                await prompt.userChoice.catch(() => null);
                this.state.pwa.installReady = false;
                this.renderPwaInstallPrompt();
                return;
            }
            this.showToast('Use Share > Add to Home Screen on your phone.', 'ok', 3200);
        });
    },

    weatherVisualContext(location) {
        const lower = String(location || '').toLowerCase();
        if (/\b(tulsa|oklahoma|okc|kansas|wichita|plains|prairie)\b/.test(lower)) return 'terrain: plains + urban';
        if (/\b(new york|nyc|manhattan|brooklyn|queens|jersey)\b/.test(lower)) return 'terrain: dense urban';
        if (/\b(seattle|portland|pnw|pacific northwest)\b/.test(lower)) return 'terrain: coastal + evergreen';
        if (/\b(denver|boulder|aspen|rocky|alps|mountain)\b/.test(lower)) return 'terrain: mountain';
        return 'terrain: local context';
    },

    locationTerrain(location) {
        const l = String(location || '').toLowerCase();
        if (/\b(tulsa|oklahoma|okc|kansas|wichita|nebraska|iowa|south dakota|north dakota|amarillo|lubbock|abilene|springfield|topeka|lincoln)\b/.test(l)) return 'plains';
        if (/\b(miami|fort lauderdale|jacksonville|charleston|savannah|galveston|corpus christi|virginia beach|long island|cape cod|malibu|santa barbara|san diego|santa monica|honolulu|waikiki)\b/.test(l)) return 'coast';
        if (/\b(los angeles|la|san francisco|sf|seattle|portland|boston|new york|nyc|manhattan|chicago|houston|dallas|atlanta|philadelphia|detroit|phoenix|san jose)\b/.test(l)) return 'city';
        if (/\b(denver|boulder|aspen|vail|breckenridge|salt lake|reno|flagstaff|asheville|missoula|bozeman|jackson hole|tahoe|colorado springs|fort collins)\b/.test(l)) return 'mountains';
        if (/\b(las vegas|phoenix|tucson|albuquerque|santa fe|el paso|palm springs|scottsdale|yuma|sedona|moab|st george)\b/.test(l)) return 'desert';
        return 'hills';
    },

    weatherHeroImageUrl(condition, location) {
        const lower = String(condition || '').toLowerCase();
        const locale = String(location || '').toLowerCase();
        const urbanLike = /\b(new york|nyc|manhattan|brooklyn|queens|jersey|chicago|dallas|houston)\b/.test(locale);
        // Picsum seeds: consistent image per condition, 1800×900, no auth required
        if (lower.includes('storm') || lower.includes('thunder')) {
            return 'https://picsum.photos/seed/storm-dark/1800/900';
        }
        if (lower.includes('rain') || lower.includes('drizzle') || lower.includes('shower')) {
            return 'https://picsum.photos/seed/rain-city/1800/900';
        }
        if (lower.includes('snow') || lower.includes('blizzard') || lower.includes('sleet')) {
            return 'https://picsum.photos/seed/snow-winter/1800/900';
        }
        if (lower.includes('fog') || lower.includes('mist') || lower.includes('haze')) {
            return 'https://picsum.photos/seed/fog-morning/1800/900';
        }
        if (lower.includes('clear') || lower.includes('sun') || lower.includes('mainly clear')) {
            return urbanLike
                ? 'https://picsum.photos/seed/clear-urban/1800/900'
                : 'https://picsum.photos/seed/clear-sky/1800/900';
        }
        if (lower.includes('partly') || lower.includes('mostly')) {
            return 'https://picsum.photos/seed/partly-cloudy/1800/900';
        }
        if (lower.includes('overcast') || lower.includes('cloud')) {
            return 'https://picsum.photos/seed/overcast-sky/1800/900';
        }
        // Default: atmospheric landscape
        return 'https://picsum.photos/seed/weather-default/1800/900';
    },

    // ── Wave-3/4 scene renderers ────────────────────────────────────────────

    makePlanRenderer(canvas) {
        // Flowing node-graph: step nodes connected by animated edges, with a
        // cascading "complete" glow as each node activates left-to-right.
        let t = 0;
        const execution = this.state.session.lastExecution;
        const steps = Array.isArray(execution?.toolResults) ? execution.toolResults : [];
        const nodeCount = Math.max(steps.length, 2);

        return () => {
            const W = canvas.width  = canvas.offsetWidth;
            const H = canvas.height = canvas.offsetHeight;
            const ctx = canvas.getContext('2d');
            ctx.clearRect(0, 0, W, H);
            t += 0.016;

            // Background gradient — dark slate
            const bg = ctx.createLinearGradient(0, 0, W, H);
            bg.addColorStop(0, '#060d14');
            bg.addColorStop(1, '#0d1a2a');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, W, H);

            // Compute node positions (horizontal chain)
            const nodeY = H * 0.48;
            const margin = W * 0.12;
            const spacing = nodeCount > 1 ? (W - margin * 2) / (nodeCount - 1) : 0;
            const nodes = Array.from({ length: nodeCount }, (_, i) => ({
                x: nodeCount === 1 ? W / 2 : margin + i * spacing,
                y: nodeY + Math.sin(t * 0.6 + i * 1.2) * 6,
                ok: (steps[i]?.ok !== false),
                active: i <= Math.floor(((Math.sin(t * 0.4) + 1) / 2) * nodeCount),
            }));

            // Draw edges
            for (let i = 0; i < nodes.length - 1; i++) {
                const a = nodes[i], b = nodes[i + 1];
                const progress = Math.min(1, Math.max(0, (t * 0.5 - i * 0.3)));
                const ex = a.x + (b.x - a.x) * progress;
                const ey = a.y + (b.y - a.y) * progress;
                ctx.beginPath();
                ctx.moveTo(a.x, a.y);
                ctx.lineTo(ex, ey);
                ctx.strokeStyle = 'rgba(56, 189, 248, 0.22)';
                ctx.lineWidth = 1.5;
                ctx.stroke();
            }

            // Draw nodes
            nodes.forEach((n, i) => {
                const glow = n.ok ? 'rgba(56,189,248,0.55)' : 'rgba(239,68,68,0.5)';
                const core = n.ok ? '#38bdf8' : '#ef4444';
                const r = 8 + Math.sin(t * 1.2 + i) * 2;
                // Glow halo
                const grad = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, r * 2.5);
                grad.addColorStop(0, glow);
                grad.addColorStop(1, 'transparent');
                ctx.fillStyle = grad;
                ctx.beginPath();
                ctx.arc(n.x, n.y, r * 2.5, 0, Math.PI * 2);
                ctx.fill();
                // Core dot
                ctx.fillStyle = core;
                ctx.beginPath();
                ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
                ctx.fill();
            });
        };
    },

    makeMusicRenderer(canvas) {
        // Deep violet-to-indigo field with slow radial pulse rings and floating
        // note-like particles suggesting audio waveform energy.
        let t = 0;
        const particles = Array.from({ length: 22 }, (_, i) => ({
            x: Math.random(), y: Math.random(),
            r: 1.5 + Math.random() * 3,
            phase: i * 0.41,
            speed: 0.002 + Math.random() * 0.003,
            amp: 0.05 + Math.random() * 0.1,
        }));
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.014;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const bg = ctx.createLinearGradient(0, 0, w, h);
            bg.addColorStop(0, '#0d0420');
            bg.addColorStop(0.5, '#1a0a3a');
            bg.addColorStop(1, '#0a0d28');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Pulse rings from center
            const cx = w * 0.5, cy = h * 0.5;
            for (let ring = 0; ring < 5; ring++) {
                const prog = ((t * 0.3 + ring * 0.2) % 1);
                const radius = prog * Math.min(w, h) * 0.6;
                const alpha = (1 - prog) * 0.22;
                ctx.beginPath();
                ctx.arc(cx, cy, radius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(167, 100, 255, ${alpha})`;
                ctx.lineWidth = 2;
                ctx.stroke();
            }
            // Floating particles
            for (const p of particles) {
                const px = (p.x + Math.sin(t * p.speed * 40 + p.phase) * p.amp) * w;
                const py = (p.y + Math.cos(t * p.speed * 30 + p.phase) * p.amp) * h;
                const alpha = 0.4 + 0.3 * Math.sin(t * 2 + p.phase);
                ctx.beginPath();
                ctx.arc(px, py, p.r, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(200, 140, 255, ${alpha})`;
                ctx.fill();
            }
            // Waveform bar in lower third
            const barCount = 40;
            const barW = w / barCount;
            for (let i = 0; i < barCount; i++) {
                const barH = (0.04 + 0.1 * Math.abs(Math.sin(t * 3 + i * 0.4))) * h;
                const alpha = 0.5 + 0.3 * Math.sin(t * 2 + i * 0.3);
                ctx.fillStyle = `rgba(180, 110, 255, ${alpha})`;
                ctx.fillRect(i * barW + 1, h * 0.75 - barH, barW - 2, barH);
            }
        };
    },

    makeSmarthomeRenderer(canvas) {
        // Warm amber-to-teal gradient with slow-breathing light orbs representing
        // individual smart devices coming alive.
        let t = 0;
        const orbs = [
            { x: 0.2, y: 0.35, phase: 0,    color: [255, 180, 60],  r: 14 },
            { x: 0.5, y: 0.25, phase: 1.2,  color: [60,  200, 200], r: 10 },
            { x: 0.78, y: 0.4, phase: 2.4,  color: [255, 140, 40],  r: 12 },
            { x: 0.35, y: 0.65, phase: 0.7, color: [100, 220, 180], r: 8  },
            { x: 0.65, y: 0.7, phase: 1.9,  color: [255, 200, 80],  r: 10 },
        ];
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.008;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const bg = ctx.createLinearGradient(0, 0, w, h);
            bg.addColorStop(0, '#0c1a14');
            bg.addColorStop(1, '#0a1520');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Subtle grid (floor plan suggestion)
            ctx.strokeStyle = 'rgba(80, 120, 100, 0.08)';
            ctx.lineWidth = 1;
            for (let x = 0; x <= w; x += 48) {
                ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
            }
            for (let y = 0; y <= h; y += 48) {
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            }
            // Breathing light orbs
            for (const orb of orbs) {
                const pulse = 0.7 + 0.3 * Math.sin(t * 1.5 + orb.phase);
                const [r, g, b] = orb.color;
                const px = orb.x * w, py = orb.y * h;
                const grad = ctx.createRadialGradient(px, py, 0, px, py, orb.r * 6 * pulse);
                grad.addColorStop(0, `rgba(${r},${g},${b},${0.5 * pulse})`);
                grad.addColorStop(1, `rgba(${r},${g},${b},0)`);
                ctx.fillStyle = grad;
                ctx.beginPath();
                ctx.arc(px, py, orb.r * 6 * pulse, 0, Math.PI * 2);
                ctx.fill();
                ctx.beginPath();
                ctx.arc(px, py, orb.r * pulse, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(${r},${g},${b},0.85)`;
                ctx.fill();
            }
        };
    },

    makeHealthRenderer(canvas) {
        // Deep green biometric aesthetic — slow ECG-style line across midscreen
        // with circular ring progress indicators in the background.
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.01;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const bg = ctx.createLinearGradient(0, 0, 0, h);
            bg.addColorStop(0, '#041410');
            bg.addColorStop(1, '#061a12');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Background rings (activity ring style)
            const rings = [
                { cx: w * 0.15, cy: h * 0.3, r: 40, color: '255,80,100',  progress: 0.75 },
                { cx: w * 0.15, cy: h * 0.3, r: 28, color: '80,210,140',  progress: 0.60 },
                { cx: w * 0.15, cy: h * 0.3, r: 16, color: '80,180,255',  progress: 0.85 },
            ];
            for (const ring of rings) {
                ctx.beginPath();
                ctx.arc(ring.cx, ring.cy, ring.r, -Math.PI / 2, Math.PI * 2 * ring.progress - Math.PI / 2);
                ctx.strokeStyle = `rgba(${ring.color}, 0.25)`;
                ctx.lineWidth = 5;
                ctx.stroke();
                const prog = (ring.progress + Math.sin(t + ring.r) * 0.05) % 1;
                ctx.beginPath();
                ctx.arc(ring.cx, ring.cy, ring.r, -Math.PI / 2, Math.PI * 2 * prog - Math.PI / 2);
                ctx.strokeStyle = `rgba(${ring.color}, 0.8)`;
                ctx.lineWidth = 5;
                ctx.stroke();
            }
            // ECG line across center
            ctx.beginPath();
            const lineY = h * 0.62;
            const speed = t * 60;
            ctx.moveTo(0, lineY);
            for (let x = 0; x <= w; x += 2) {
                const phase = (x + speed) % w;
                let y = lineY;
                // QRS complex shape — spike near phase 120-180
                const localPhase = phase % 200;
                if (localPhase > 100 && localPhase < 110) y = lineY - 28;
                else if (localPhase > 110 && localPhase < 118) y = lineY + 14;
                else if (localPhase > 118 && localPhase < 130) y = lineY - 8;
                else y = lineY + Math.sin(phase * 0.04) * 3;
                ctx.lineTo(x, y);
            }
            ctx.strokeStyle = 'rgba(80, 220, 140, 0.7)';
            ctx.lineWidth = 2;
            ctx.stroke();
            // Subtle glow under ECG
            ctx.beginPath();
            ctx.moveTo(0, lineY);
            for (let x = 0; x <= w; x += 2) {
                const phase = (x + speed) % w;
                const localPhase = phase % 200;
                let y = lineY;
                if (localPhase > 100 && localPhase < 110) y = lineY - 28;
                else if (localPhase > 110 && localPhase < 118) y = lineY + 14;
                else if (localPhase > 118 && localPhase < 130) y = lineY - 8;
                else y = lineY + Math.sin(phase * 0.04) * 3;
                ctx.lineTo(x, y);
            }
            ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
            ctx.fillStyle = 'rgba(80, 220, 140, 0.06)';
            ctx.fill();
        };
    },

    makeMessagingRenderer(canvas) {
        // Soft blue-grey chat bubble aesthetic — floating rounded rectangles
        // drift upward like a conversation in motion.
        let t = 0;
        const bubbles = Array.from({ length: 10 }, (_, i) => ({
            x: 0.08 + Math.random() * 0.84,
            y: 0.2 + Math.random() * 0.7,
            w: 0.15 + Math.random() * 0.25,
            h: 0.04 + Math.random() * 0.04,
            side: i % 2,   // 0 = left, 1 = right
            phase: i * 0.63,
            speed: 0.0008 + Math.random() * 0.0006,
        }));
        const rr = (ctx, x, y, bw, bh, r) => {
            ctx.beginPath();
            ctx.moveTo(x + r, y);
            ctx.lineTo(x + bw - r, y);
            ctx.quadraticCurveTo(x + bw, y, x + bw, y + r);
            ctx.lineTo(x + bw, y + bh - r);
            ctx.quadraticCurveTo(x + bw, y + bh, x + bw - r, y + bh);
            ctx.lineTo(x + r, y + bh);
            ctx.quadraticCurveTo(x, y + bh, x, y + bh - r);
            ctx.lineTo(x, y + r);
            ctx.quadraticCurveTo(x, y, x + r, y);
            ctx.closePath();
        };
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.006;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const bg = ctx.createLinearGradient(0, 0, 0, h);
            bg.addColorStop(0, '#0a0f1a');
            bg.addColorStop(1, '#0d1428');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            for (const b of bubbles) {
                const drift = (b.phase + t * b.speed * 40) % 1.2 - 0.1;
                const by = (1 - drift) * h;
                const bx = b.side === 1 ? (1 - b.x - b.w) * w : b.x * w;
                const bw2 = b.w * w, bh2 = b.h * h;
                const alpha = 0.08 + 0.06 * Math.sin(t * 2 + b.phase);
                const color = b.side === 1 ? `rgba(80,140,255,${alpha})` : `rgba(60,200,180,${alpha})`;
                rr(ctx, bx, by, bw2, bh2, 10);
                ctx.fillStyle = color;
                ctx.fill();
            }
        };
    },

    makeTravelRenderer(canvas) {
        // Dark navy with a slow star-field and a horizon glow — suggests
        // looking out an airplane window over city lights at night.
        let t = 0;
        const stars = Array.from({ length: 80 }, () => ({
            x: Math.random(), y: Math.random() * 0.6,
            r: 0.5 + Math.random() * 1.5,
            twinkle: Math.random() * Math.PI * 2,
        }));
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.008;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const bg = ctx.createLinearGradient(0, 0, 0, h);
            bg.addColorStop(0, '#02040f');
            bg.addColorStop(0.55, '#050d20');
            bg.addColorStop(0.75, '#0a1a2e');
            bg.addColorStop(1, '#091420');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Stars
            for (const s of stars) {
                const alpha = 0.5 + 0.4 * Math.sin(t * 1.5 + s.twinkle);
                ctx.beginPath();
                ctx.arc(s.x * w, s.y * h, s.r, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(220,230,255,${alpha})`;
                ctx.fill();
            }
            // Horizon glow (city lights)
            const horizonY = h * 0.72;
            const glow = ctx.createLinearGradient(0, horizonY - 30, 0, horizonY + 60);
            glow.addColorStop(0, 'rgba(255,180,60,0)');
            glow.addColorStop(0.5, 'rgba(255,160,40,0.18)');
            glow.addColorStop(1, 'rgba(255,120,20,0.08)');
            ctx.fillStyle = glow;
            ctx.fillRect(0, horizonY - 30, w, 90);
            // Silhouette ground
            const ground = ctx.createLinearGradient(0, horizonY + 30, 0, h);
            ground.addColorStop(0, 'rgba(5,12,28,0.9)');
            ground.addColorStop(1, 'rgba(2,6,16,1)');
            ctx.fillStyle = ground;
            ctx.fillRect(0, horizonY + 30, w, h - horizonY - 30);
            // Scattered city light dots
            for (let i = 0; i < 50; i++) {
                const lx = ((i * 0.618033 + t * 0.01) % 1) * w;
                const ly = horizonY + 35 + (i % 5) * 6;
                const alpha = 0.3 + 0.4 * Math.sin(t * 3 + i * 0.7);
                ctx.beginPath();
                ctx.arc(lx, ly, 1, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(255,200,120,${alpha})`;
                ctx.fill();
            }
        };
    },

    makePaymentsRenderer(canvas) {
        // Dark emerald with floating card silhouettes and a subtle radial
        // glow suggesting tap-to-pay proximity.
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.01;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const bg = ctx.createLinearGradient(0, 0, w, h);
            bg.addColorStop(0, '#030e0a');
            bg.addColorStop(1, '#04140c');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Tap-to-pay ripple rings
            const cx = w * 0.72, cy = h * 0.42;
            for (let ring = 0; ring < 4; ring++) {
                const prog = ((t * 0.5 + ring * 0.25) % 1);
                const radius = 20 + prog * 80;
                const alpha = (1 - prog) * 0.3;
                ctx.beginPath();
                ctx.arc(cx, cy, radius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(60,220,140,${alpha})`;
                ctx.lineWidth = 2;
                ctx.stroke();
            }
            // Card shape at left
            const cardX = w * 0.06, cardY = h * 0.28, cardW = w * 0.42, cardH = h * 0.22;
            const cardRad = 12;
            ctx.beginPath();
            ctx.moveTo(cardX + cardRad, cardY);
            ctx.lineTo(cardX + cardW - cardRad, cardY);
            ctx.quadraticCurveTo(cardX + cardW, cardY, cardX + cardW, cardY + cardRad);
            ctx.lineTo(cardX + cardW, cardY + cardH - cardRad);
            ctx.quadraticCurveTo(cardX + cardW, cardY + cardH, cardX + cardW - cardRad, cardY + cardH);
            ctx.lineTo(cardX + cardRad, cardY + cardH);
            ctx.quadraticCurveTo(cardX, cardY + cardH, cardX, cardY + cardH - cardRad);
            ctx.lineTo(cardX, cardY + cardRad);
            ctx.quadraticCurveTo(cardX, cardY, cardX + cardRad, cardY);
            ctx.closePath();
            const cardGrad = ctx.createLinearGradient(cardX, cardY, cardX + cardW, cardY + cardH);
            cardGrad.addColorStop(0, 'rgba(30,80,60,0.5)');
            cardGrad.addColorStop(1, 'rgba(20,60,44,0.3)');
            ctx.fillStyle = cardGrad;
            ctx.fill();
            ctx.strokeStyle = 'rgba(60,180,120,0.3)';
            ctx.lineWidth = 1;
            ctx.stroke();
            // Chip
            ctx.fillStyle = 'rgba(180,200,160,0.25)';
            ctx.fillRect(cardX + 16, cardY + cardH * 0.3, 28, 20);
        };
    },

    makeFocusRenderer(canvas) {
        // Minimal dark slate with a single breathing orb and slow concentric
        // rings — Pomodoro / deep-work atmosphere.
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.007;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.fillStyle = '#060810';
            ctx.fillRect(0, 0, w, h);
            const cx = w * 0.5, cy = h * 0.45;
            // Concentric rings (timer / progress feel)
            for (let ring = 5; ring >= 1; ring--) {
                const radius = ring * 28 + Math.sin(t * 0.8 + ring) * 3;
                const alpha = 0.04 + 0.02 * ring;
                ctx.beginPath();
                ctx.arc(cx, cy, radius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(140,180,255,${alpha})`;
                ctx.lineWidth = 1.5;
                ctx.stroke();
            }
            // Breathing central orb
            const breathe = 0.85 + 0.15 * Math.sin(t * 0.9);
            const orbR = 22 * breathe;
            const orbGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, orbR * 3);
            orbGrad.addColorStop(0, `rgba(120,160,255,${0.55 * breathe})`);
            orbGrad.addColorStop(0.4, `rgba(100,140,220,${0.2 * breathe})`);
            orbGrad.addColorStop(1, 'rgba(80,100,180,0)');
            ctx.fillStyle = orbGrad;
            ctx.beginPath();
            ctx.arc(cx, cy, orbR * 3, 0, Math.PI * 2);
            ctx.fill();
            ctx.beginPath();
            ctx.arc(cx, cy, orbR, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(160,200,255,${0.7 * breathe})`;
            ctx.fill();
        };
    },

    makeNetworkRenderer(canvas) {
        // Mesh network scene — dark void with slow travelling signal nodes
        // and faint edge lines, evoking a P2P gossip network topology.
        let t = 0;
        const nodes = Array.from({ length: 14 }, () => ({
            x: 0.1 + 0.8 * Math.random(),
            y: 0.1 + 0.8 * Math.random(),
            vx: (Math.random() - 0.5) * 0.0006,
            vy: (Math.random() - 0.5) * 0.0006,
            r: 3 + Math.random() * 4,
            phase: Math.random() * Math.PI * 2,
        }));
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.004;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.fillStyle = '#04070f';
            ctx.fillRect(0, 0, w, h);
            // Update node positions (wrap)
            for (const n of nodes) {
                n.x = ((n.x + n.vx) + 1) % 1;
                n.y = ((n.y + n.vy) + 1) % 1;
            }
            // Draw edges between nearby nodes
            for (let i = 0; i < nodes.length; i++) {
                for (let j = i + 1; j < nodes.length; j++) {
                    const dx = (nodes[i].x - nodes[j].x) * w;
                    const dy = (nodes[i].y - nodes[j].y) * h;
                    const dist = Math.sqrt(dx * dx + dy * dy);
                    if (dist < 180) {
                        const alpha = (1 - dist / 180) * 0.18;
                        ctx.beginPath();
                        ctx.moveTo(nodes[i].x * w, nodes[i].y * h);
                        ctx.lineTo(nodes[j].x * w, nodes[j].y * h);
                        ctx.strokeStyle = `rgba(60,160,255,${alpha})`;
                        ctx.lineWidth = 0.8;
                        ctx.stroke();
                    }
                }
            }
            // Draw nodes
            for (const n of nodes) {
                const pulse = 0.7 + 0.3 * Math.sin(t * 1.2 + n.phase);
                const grd = ctx.createRadialGradient(n.x * w, n.y * h, 0, n.x * w, n.y * h, n.r * 3);
                grd.addColorStop(0, `rgba(80,180,255,${0.6 * pulse})`);
                grd.addColorStop(1, 'rgba(40,100,200,0)');
                ctx.fillStyle = grd;
                ctx.beginPath();
                ctx.arc(n.x * w, n.y * h, n.r * 3, 0, Math.PI * 2);
                ctx.fill();
                ctx.beginPath();
                ctx.arc(n.x * w, n.y * h, n.r * pulse, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(140,210,255,${0.85 * pulse})`;
                ctx.fill();
            }
        };
    },

    makeBankingRenderer(canvas) {
        // Dark navy with a slowly scrolling candlestick/bar chart and a
        // faint grid — financial terminal atmosphere.
        let t = 0;
        const bars = Array.from({ length: 28 }, (_, i) => ({
            v: 0.25 + 0.55 * Math.random(),
            up: Math.random() > 0.45,
            phase: i * 0.38,
        }));
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.004;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            // Background
            const bg = ctx.createLinearGradient(0, 0, 0, h);
            bg.addColorStop(0, '#020b14');
            bg.addColorStop(1, '#030f1c');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Horizontal grid lines
            for (let g = 1; g < 5; g++) {
                const y = h * g / 5;
                ctx.beginPath();
                ctx.moveTo(0, y); ctx.lineTo(w, y);
                ctx.strokeStyle = 'rgba(60,120,200,0.08)';
                ctx.lineWidth = 1;
                ctx.stroke();
            }
            // Bar chart — scrolls slightly right-to-left
            const barW = w / bars.length;
            const scroll = (t * 18) % barW;
            for (let i = 0; i < bars.length; i++) {
                const bar = bars[i];
                const x = i * barW - scroll + barW;
                const bh = bar.v * h * 0.55;
                const y = h * 0.75 - bh;
                const wave = 0.92 + 0.08 * Math.sin(t * 0.6 + bar.phase);
                const color = bar.up ? `rgba(40,200,120,${0.55 * wave})` : `rgba(220,70,70,${0.5 * wave})`;
                ctx.fillStyle = color;
                ctx.fillRect(x + 1, y, barW - 3, bh);
                // Wick
                ctx.strokeStyle = bar.up ? `rgba(40,200,120,${0.4 * wave})` : `rgba(220,70,70,${0.35 * wave})`;
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(x + barW / 2, y - 6);
                ctx.lineTo(x + barW / 2, y + bh + 6);
                ctx.stroke();
            }
            // Accent glow at bottom-left
            const grd = ctx.createRadialGradient(0, h, 0, 0, h, w * 0.5);
            grd.addColorStop(0, 'rgba(20,100,200,0.12)');
            grd.addColorStop(1, 'rgba(20,100,200,0)');
            ctx.fillStyle = grd;
            ctx.fillRect(0, 0, w, h);
        };
    },

    makeContactsRenderer(canvas) {
        // Warm charcoal with avatar-orbs orbiting a central hub — people network.
        let t = 0;
        const orbs = Array.from({ length: 8 }, (_, i) => ({
            angle: (i / 8) * Math.PI * 2,
            radius: 0.22 + 0.12 * (i % 3) / 2,
            speed: 0.0004 + 0.0002 * (i % 3),
            r: 6 + (i % 3) * 3,
            hue: 20 + i * 30,
        }));
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.006;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.fillStyle = '#0c0b0f';
            ctx.fillRect(0, 0, w, h);
            const cx = w * 0.5, cy = h * 0.44;
            // Hub glow
            const hubGrd = ctx.createRadialGradient(cx, cy, 0, cx, cy, 60);
            hubGrd.addColorStop(0, 'rgba(255,180,100,0.18)');
            hubGrd.addColorStop(1, 'rgba(255,140,60,0)');
            ctx.fillStyle = hubGrd;
            ctx.fillRect(0, 0, w, h);
            // Hub dot
            ctx.beginPath();
            ctx.arc(cx, cy, 8, 0, Math.PI * 2);
            ctx.fillStyle = 'rgba(255,200,140,0.8)';
            ctx.fill();
            // Orbiting avatar orbs
            for (const orb of orbs) {
                orb.angle += orb.speed;
                const ox = cx + Math.cos(orb.angle) * orb.radius * Math.min(w, h);
                const oy = cy + Math.sin(orb.angle) * orb.radius * Math.min(w, h) * 0.6;
                // Connection line
                ctx.beginPath();
                ctx.moveTo(cx, cy); ctx.lineTo(ox, oy);
                ctx.strokeStyle = `hsla(${orb.hue},60%,65%,0.12)`;
                ctx.lineWidth = 1;
                ctx.stroke();
                // Orb glow
                const orbGrd = ctx.createRadialGradient(ox, oy, 0, ox, oy, orb.r * 3);
                orbGrd.addColorStop(0, `hsla(${orb.hue},70%,70%,0.5)`);
                orbGrd.addColorStop(1, `hsla(${orb.hue},60%,60%,0)`);
                ctx.fillStyle = orbGrd;
                ctx.beginPath();
                ctx.arc(ox, oy, orb.r * 3, 0, Math.PI * 2);
                ctx.fill();
                // Orb dot
                ctx.beginPath();
                ctx.arc(ox, oy, orb.r, 0, Math.PI * 2);
                ctx.fillStyle = `hsla(${orb.hue},75%,72%,0.85)`;
                ctx.fill();
            }
        };
    },

    makeLocationRenderer(canvas) {
        // Midnight blue with a pulsing pin at center and radiating range rings.
        let t = 0;
        const particles = Array.from({ length: 30 }, () => ({
            x: Math.random(), y: Math.random(),
            vx: (Math.random() - 0.5) * 0.0003,
            vy: (Math.random() - 0.5) * 0.0003,
            a: 0.05 + Math.random() * 0.12,
        }));
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.008;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const bg = ctx.createLinearGradient(0, 0, 0, h);
            bg.addColorStop(0, '#010814');
            bg.addColorStop(1, '#020c1c');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            const cx = w * 0.5, cy = h * 0.42;
            // Radiating range rings
            for (let ring = 0; ring < 5; ring++) {
                const prog = ((t * 0.35 + ring * 0.2) % 1);
                const radius = 30 + prog * Math.min(w, h) * 0.45;
                const alpha = (1 - prog) * 0.22;
                ctx.beginPath();
                ctx.arc(cx, cy, radius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(60,180,255,${alpha})`;
                ctx.lineWidth = 1.5;
                ctx.stroke();
            }
            // Particles (map points)
            for (const p of particles) {
                p.x = ((p.x + p.vx) + 1) % 1;
                p.y = ((p.y + p.vy) + 1) % 1;
                ctx.beginPath();
                ctx.arc(p.x * w, p.y * h, 1.5, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(100,200,255,${p.a})`;
                ctx.fill();
            }
            // Pin glow
            const pinGrd = ctx.createRadialGradient(cx, cy, 0, cx, cy, 50);
            pinGrd.addColorStop(0, 'rgba(60,180,255,0.35)');
            pinGrd.addColorStop(1, 'rgba(30,120,220,0)');
            ctx.fillStyle = pinGrd;
            ctx.fillRect(0, 0, w, h);
            // Pin shape
            const pulse = 0.88 + 0.12 * Math.sin(t * 1.4);
            ctx.beginPath();
            ctx.arc(cx, cy - 4, 10 * pulse, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(80,200,255,${0.85 * pulse})`;
            ctx.fill();
            ctx.beginPath();
            ctx.moveTo(cx, cy + 14 * pulse);
            ctx.lineTo(cx - 7 * pulse, cy - 2 * pulse);
            ctx.lineTo(cx + 7 * pulse, cy - 2 * pulse);
            ctx.closePath();
            ctx.fillStyle = `rgba(80,200,255,${0.7 * pulse})`;
            ctx.fill();
        };
    },

    makeSocialRenderer(canvas) {
        // Deep indigo with a flowing feed-wave and orbiting reaction sparks.
        let t = 0;
        const sparks = Array.from({ length: 18 }, (_, i) => ({
            x: Math.random(), y: Math.random(),
            vx: (Math.random() - 0.5) * 0.0005,
            vy: -0.0004 - Math.random() * 0.0004,
            r: 1.5 + Math.random() * 2.5,
            hue: [340, 30, 200, 140, 280][i % 5],
            phase: Math.random() * Math.PI * 2,
        }));
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.007;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const bg = ctx.createLinearGradient(0, 0, w * 0.4, h);
            bg.addColorStop(0, '#08040f');
            bg.addColorStop(1, '#0c0618');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Feed wave lines
            for (let row = 0; row < 4; row++) {
                const y = h * (0.25 + row * 0.15);
                const amp = 4 + row;
                ctx.beginPath();
                for (let x = 0; x <= w; x += 4) {
                    const wy = y + amp * Math.sin((x / w) * 6 + t * 1.2 + row);
                    x === 0 ? ctx.moveTo(x, wy) : ctx.lineTo(x, wy);
                }
                ctx.strokeStyle = `rgba(160,100,255,${0.06 + 0.03 * row})`;
                ctx.lineWidth = 1.2;
                ctx.stroke();
            }
            // Accent glow
            const grd = ctx.createRadialGradient(w * 0.3, h * 0.4, 0, w * 0.3, h * 0.4, w * 0.4);
            grd.addColorStop(0, 'rgba(140,80,255,0.1)');
            grd.addColorStop(1, 'rgba(100,40,200,0)');
            ctx.fillStyle = grd;
            ctx.fillRect(0, 0, w, h);
            // Reaction sparks drifting upward
            for (const s of sparks) {
                s.x = ((s.x + s.vx) + 1) % 1;
                s.y = ((s.y + s.vy) + 1) % 1;
                const alpha = 0.5 + 0.4 * Math.sin(t * 1.5 + s.phase);
                const grd2 = ctx.createRadialGradient(s.x * w, s.y * h, 0, s.x * w, s.y * h, s.r * 2.5);
                grd2.addColorStop(0, `hsla(${s.hue},80%,72%,${alpha})`);
                grd2.addColorStop(1, `hsla(${s.hue},70%,60%,0)`);
                ctx.fillStyle = grd2;
                ctx.beginPath();
                ctx.arc(s.x * w, s.y * h, s.r * 2.5, 0, Math.PI * 2);
                ctx.fill();
            }
        };
    },

    makeTelephonyRenderer(canvas) {
        // Deep charcoal with a live soundwave radiating from center — call signal.
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.012;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.fillStyle = '#080a0c';
            ctx.fillRect(0, 0, w, h);
            const cx = w * 0.5, cy = h * 0.44;
            // Call rings radiating outward
            for (let ring = 0; ring < 6; ring++) {
                const prog = ((t * 0.4 + ring / 6) % 1);
                const radius = 18 + prog * Math.min(w, h) * 0.42;
                const alpha = (1 - prog) * 0.28;
                ctx.beginPath();
                ctx.arc(cx, cy, radius, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(80,220,140,${alpha})`;
                ctx.lineWidth = 2;
                ctx.stroke();
            }
            // Soundwave bars either side of center
            const bars = 14;
            const barSpan = w * 0.28;
            for (let i = 0; i < bars; i++) {
                const x = cx - barSpan + (i / (bars - 1)) * barSpan * 2;
                const amp = 0.5 + 0.5 * Math.sin(t * 4 + i * 0.7 + Math.sin(t * 2 + i * 0.3));
                const bh = 6 + amp * 28;
                const alpha = 0.35 + 0.35 * amp;
                ctx.fillStyle = `rgba(60,220,130,${alpha})`;
                ctx.fillRect(x - 2, cy - bh / 2, 4, bh);
            }
            // Center glow
            const grd = ctx.createRadialGradient(cx, cy, 0, cx, cy, 45);
            grd.addColorStop(0, 'rgba(60,220,130,0.22)');
            grd.addColorStop(1, 'rgba(40,180,100,0)');
            ctx.fillStyle = grd;
            ctx.fillRect(0, 0, w, h);
        };
    },

    makeRemindersRenderer(canvas) {
        // Dark olive-green with an analog clock face and gentle glow rings
        // for each pending reminder.
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.005;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const bg = ctx.createLinearGradient(0, 0, 0, h);
            bg.addColorStop(0, '#050a06');
            bg.addColorStop(1, '#070d07');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            const cx = w * 0.5, cy = h * 0.44;
            const R = Math.min(w, h) * 0.22;
            // Outer glow ring
            const grd = ctx.createRadialGradient(cx, cy, R * 0.7, cx, cy, R * 1.6);
            grd.addColorStop(0, 'rgba(120,200,100,0.08)');
            grd.addColorStop(1, 'rgba(80,160,70,0)');
            ctx.fillStyle = grd;
            ctx.fillRect(0, 0, w, h);
            // Clock face ring
            ctx.beginPath();
            ctx.arc(cx, cy, R, 0, Math.PI * 2);
            ctx.strokeStyle = 'rgba(100,180,80,0.25)';
            ctx.lineWidth = 1.5;
            ctx.stroke();
            // Hour ticks
            for (let tick = 0; tick < 12; tick++) {
                const angle = (tick / 12) * Math.PI * 2 - Math.PI / 2;
                const isMain = tick % 3 === 0;
                const r0 = isMain ? R * 0.85 : R * 0.9;
                ctx.beginPath();
                ctx.moveTo(cx + Math.cos(angle) * r0, cy + Math.sin(angle) * r0);
                ctx.lineTo(cx + Math.cos(angle) * R, cy + Math.sin(angle) * R);
                ctx.strokeStyle = `rgba(120,200,90,${isMain ? 0.4 : 0.2})`;
                ctx.lineWidth = isMain ? 2 : 1;
                ctx.stroke();
            }
            // Animated hands (wall-clock driven by t for smoothness)
            const nowS = t * 60; // fake seconds from t
            const minAngle = (nowS / 60) * Math.PI * 2 - Math.PI / 2;
            const hrAngle = (nowS / 720) * Math.PI * 2 - Math.PI / 2;
            // Minute hand
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.lineTo(cx + Math.cos(minAngle) * R * 0.78, cy + Math.sin(minAngle) * R * 0.78);
            ctx.strokeStyle = 'rgba(140,220,100,0.7)';
            ctx.lineWidth = 2;
            ctx.lineCap = 'round';
            ctx.stroke();
            // Hour hand
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.lineTo(cx + Math.cos(hrAngle) * R * 0.5, cy + Math.sin(hrAngle) * R * 0.5);
            ctx.strokeStyle = 'rgba(140,220,100,0.55)';
            ctx.lineWidth = 3;
            ctx.stroke();
            ctx.lineCap = 'butt';
            // Center dot
            ctx.beginPath();
            ctx.arc(cx, cy, 4, 0, Math.PI * 2);
            ctx.fillStyle = 'rgba(160,240,120,0.85)';
            ctx.fill();
        };
    },

    makeFinanceRenderer(canvas) {
        // Dark navy with a scrolling line chart and candlestick-style
        // price action — portfolio / market atmosphere.
        let t = 0;
        const points = Array.from({ length: 60 }, (_, i) => {
            let v = 0.5;
            return { v: (v += (Math.random() - 0.48) * 0.08), i };
        });
        // Normalize
        const min = Math.min(...points.map(p => p.v));
        const max = Math.max(...points.map(p => p.v));
        const rng = max - min || 1;
        for (const p of points) p.v = (p.v - min) / rng;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.003;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const bg = ctx.createLinearGradient(0, 0, w, h);
            bg.addColorStop(0, '#02080f');
            bg.addColorStop(1, '#020c16');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Grid
            for (let g = 1; g < 4; g++) {
                ctx.beginPath();
                ctx.moveTo(0, h * g / 4); ctx.lineTo(w, h * g / 4);
                ctx.strokeStyle = 'rgba(40,100,180,0.07)';
                ctx.lineWidth = 1;
                ctx.stroke();
            }
            // Scrolling line chart
            const scroll = (t * 12) % (w / points.length);
            const step = w / (points.length - 1);
            const chartTop = h * 0.15, chartH = h * 0.55;
            // Area fill
            ctx.beginPath();
            ctx.moveTo(-scroll, chartTop + chartH);
            for (let i = 0; i < points.length; i++) {
                const x = i * step - scroll;
                const y = chartTop + (1 - points[i].v) * chartH;
                i === 0 ? ctx.lineTo(x, y) : ctx.lineTo(x, y);
            }
            ctx.lineTo((points.length - 1) * step - scroll, chartTop + chartH);
            ctx.closePath();
            const areaGrd = ctx.createLinearGradient(0, chartTop, 0, chartTop + chartH);
            areaGrd.addColorStop(0, 'rgba(50,180,120,0.18)');
            areaGrd.addColorStop(1, 'rgba(30,120,80,0)');
            ctx.fillStyle = areaGrd;
            ctx.fill();
            // Line
            ctx.beginPath();
            for (let i = 0; i < points.length; i++) {
                const x = i * step - scroll;
                const y = chartTop + (1 - points[i].v) * chartH;
                i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
            }
            ctx.strokeStyle = 'rgba(60,210,130,0.65)';
            ctx.lineWidth = 2;
            ctx.stroke();
            // Accent glow lower-right
            const grd = ctx.createRadialGradient(w, h, 0, w, h, w * 0.6);
            grd.addColorStop(0, 'rgba(30,100,200,0.1)');
            grd.addColorStop(1, 'rgba(20,80,160,0)');
            ctx.fillStyle = grd;
            ctx.fillRect(0, 0, w, h);
        };
    },

    makeGamingRenderer(canvas) {
        // Dark purple/black with animated scanlines, controller glyph pulse,
        // and XP bar fill — classic gaming atmosphere.
        let t = 0;
        const stars = Array.from({ length: 40 }, () => ({
            x: Math.random(), y: Math.random(),
            s: 0.5 + Math.random() * 1.5,
            o: 0.2 + Math.random() * 0.5,
            speed: 0.00015 + Math.random() * 0.0003,
        }));
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.012;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            // Deep purple background
            const bg = ctx.createLinearGradient(0, 0, w, h);
            bg.addColorStop(0, '#0a0314');
            bg.addColorStop(1, '#110820');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Scanlines
            for (let y = 0; y < h; y += 3) {
                ctx.fillStyle = 'rgba(0,0,0,0.12)';
                ctx.fillRect(0, y, w, 1);
            }
            // Drifting star particles
            for (const star of stars) {
                star.x -= star.speed;
                if (star.x < 0) { star.x = 1; star.y = Math.random(); }
                ctx.beginPath();
                ctx.arc(star.x * w, star.y * h, star.s, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(180,130,255,${star.o})`;
                ctx.fill();
            }
            // XP bar at bottom
            const barW = w * 0.6, barH = 5, barX = w * 0.2, barY = h * 0.82;
            ctx.fillStyle = 'rgba(100,50,200,0.25)';
            ctx.beginPath();
            ctx.roundRect(barX, barY, barW, barH, 3);
            ctx.fill();
            const fill = (0.55 + 0.35 * Math.sin(t * 0.18)) * barW;
            const fillGrd = ctx.createLinearGradient(barX, 0, barX + fill, 0);
            fillGrd.addColorStop(0, 'rgba(140,80,255,0.8)');
            fillGrd.addColorStop(1, 'rgba(200,100,255,0.9)');
            ctx.fillStyle = fillGrd;
            ctx.beginPath();
            ctx.roundRect(barX, barY, fill, barH, 3);
            ctx.fill();
            // Controller icon (simplified D-pad cross)
            const cx = w * 0.5, cy = h * 0.52;
            const sz = Math.min(w, h) * 0.06;
            const pulse = 0.6 + 0.4 * Math.sin(t * 0.7);
            ctx.strokeStyle = `rgba(160,100,255,${pulse * 0.5})`;
            ctx.lineWidth = 2;
            // Horizontal bar
            ctx.beginPath();
            ctx.moveTo(cx - sz, cy); ctx.lineTo(cx + sz, cy);
            ctx.stroke();
            // Vertical bar
            ctx.beginPath();
            ctx.moveTo(cx, cy - sz); ctx.lineTo(cx, cy + sz);
            ctx.stroke();
            // Accent glow center
            const grd = ctx.createRadialGradient(cx, cy, 0, cx, cy, sz * 2.5);
            grd.addColorStop(0, `rgba(140,80,255,${0.15 * pulse})`);
            grd.addColorStop(1, 'rgba(80,40,160,0)');
            ctx.fillStyle = grd;
            ctx.fillRect(0, 0, w, h);
        };
    },

    makeArVrRenderer(canvas) {
        // Holographic teal grid on deep black — spatial computing atmosphere.
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.008;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.fillStyle = '#010508';
            ctx.fillRect(0, 0, w, h);
            // Perspective grid floor
            const vx = w * 0.5, vy = h * 0.55;
            const cols = 10, rows = 8;
            ctx.strokeStyle = 'rgba(0,200,180,0.18)';
            ctx.lineWidth = 0.8;
            for (let c = 0; c <= cols; c++) {
                const px = (c / cols) * w;
                ctx.beginPath();
                ctx.moveTo(px, vy);
                ctx.lineTo(vx, h * 0.05);
                ctx.stroke();
            }
            for (let r = 1; r <= rows; r++) {
                const frac = r / rows;
                const y = vy + (h - vy) * (frac * frac);
                const xSpread = w * frac * 0.5;
                ctx.beginPath();
                ctx.moveTo(w * 0.5 - xSpread, y);
                ctx.lineTo(w * 0.5 + xSpread, y);
                ctx.stroke();
            }
            // Floating HUD ring
            const rx = w * 0.5, ry = h * 0.32;
            const radius = Math.min(w, h) * 0.1;
            const pulse = 0.5 + 0.5 * Math.sin(t * 0.9);
            const arcGrd = ctx.createRadialGradient(rx, ry, radius * 0.5, rx, ry, radius * 1.4);
            arcGrd.addColorStop(0, `rgba(0,220,200,${0.3 * pulse})`);
            arcGrd.addColorStop(1, 'rgba(0,180,160,0)');
            ctx.fillStyle = arcGrd;
            ctx.fillRect(0, 0, w, h);
            ctx.beginPath();
            ctx.arc(rx, ry, radius, 0, Math.PI * 2);
            ctx.strokeStyle = `rgba(0,220,200,${0.5 * pulse})`;
            ctx.lineWidth = 1.5;
            ctx.stroke();
            // Rotating inner tick marks
            for (let i = 0; i < 12; i++) {
                const angle = (i / 12) * Math.PI * 2 + t;
                const x1 = rx + Math.cos(angle) * radius * 0.85;
                const y1 = ry + Math.sin(angle) * radius * 0.85;
                const x2 = rx + Math.cos(angle) * radius * 1.0;
                const y2 = ry + Math.sin(angle) * radius * 1.0;
                ctx.beginPath();
                ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
                ctx.strokeStyle = `rgba(0,255,220,${0.6 * pulse})`;
                ctx.lineWidth = 1;
                ctx.stroke();
            }
        };
    },

    makeDatingRenderer(canvas) {
        // Warm deep rose with floating heart particles and soft bokeh glow.
        let t = 0;
        const hearts = Array.from({ length: 14 }, () => ({
            x: Math.random() * 100,
            y: 20 + Math.random() * 70,
            size: 4 + Math.random() * 10,
            speed: 0.015 + Math.random() * 0.025,
            drift: (Math.random() - 0.5) * 0.008,
            opacity: 0.2 + Math.random() * 0.4,
            phase: Math.random() * Math.PI * 2,
        }));
        const drawHeart = (ctx, cx, cy, size, opacity) => {
            ctx.save();
            ctx.globalAlpha = opacity;
            ctx.beginPath();
            ctx.moveTo(cx, cy + size * 0.25);
            ctx.bezierCurveTo(cx, cy - size * 0.5, cx - size, cy - size * 0.5, cx - size, cy);
            ctx.bezierCurveTo(cx - size, cy + size * 0.5, cx, cy + size * 0.9, cx, cy + size * 0.9);
            ctx.bezierCurveTo(cx, cy + size * 0.9, cx + size, cy + size * 0.5, cx + size, cy);
            ctx.bezierCurveTo(cx + size, cy - size * 0.5, cx, cy - size * 0.5, cx, cy + size * 0.25);
            ctx.fillStyle = 'rgba(240,80,100,0.7)';
            ctx.fill();
            ctx.restore();
        };
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.01;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const bg = ctx.createLinearGradient(0, 0, w, h);
            bg.addColorStop(0, '#18060c');
            bg.addColorStop(1, '#240a12');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            // Soft bokeh glows
            for (let i = 0; i < 5; i++) {
                const bx = w * (0.1 + i * 0.2);
                const by = h * (0.3 + 0.2 * Math.sin(t * 0.3 + i));
                const br = w * 0.12;
                const bgrd = ctx.createRadialGradient(bx, by, 0, bx, by, br);
                bgrd.addColorStop(0, 'rgba(200,60,80,0.1)');
                bgrd.addColorStop(1, 'rgba(160,30,50,0)');
                ctx.fillStyle = bgrd;
                ctx.fillRect(0, 0, w, h);
            }
            // Floating hearts
            for (const heart of hearts) {
                heart.x -= heart.speed;
                heart.y += Math.sin(t + heart.phase) * heart.drift;
                if (heart.x < -5) heart.x = 105;
                const pulse = 0.85 + 0.15 * Math.sin(t * 1.2 + heart.phase);
                drawHeart(ctx, heart.x / 100 * w, heart.y / 100 * h,
                          heart.size * pulse, heart.opacity);
            }
            // Center glow
            const cgrd = ctx.createRadialGradient(w * 0.5, h * 0.45, 0, w * 0.5, h * 0.45, w * 0.35);
            cgrd.addColorStop(0, `rgba(220,60,90,${0.1 + 0.05 * Math.sin(t * 0.5)})`);
            cgrd.addColorStop(1, 'rgba(180,30,60,0)');
            ctx.fillStyle = cgrd;
            ctx.fillRect(0, 0, w, h);
        };
    },

    // ── Mesh: auto-join local network + notification badge ───────────────────

    _initNotifications() {
        // Wire Electron notification click → intent routing
        if (window.electronAPI?.onNotificationClick) {
            window.electronAPI.onNotificationClick(({ route }) => {
                if (route) this.handleIntent(String(route));
            });
        }
        // Auto-updater banner — show a subtle toast when an update is ready
        if (window.electronAPI?.onUpdaterStatus) {
            window.electronAPI.onUpdaterStatus(({ event, version, percent }) => {
                if (event === 'ready') {
                    this.showToast(
                        `Update ${version || ''} ready. Restart now?`,
                        'ok',
                        14000,
                        { label: 'Restart', onClick: () => this.installUpdateNow(version || '') }
                    );
                } else if (event === 'available') {
                    this.showToast(`Downloading update ${version || ''}…`, 'info', 3500);
                } else if (event === 'downloading' && Number.isFinite(Number(percent)) && Number(percent) >= 100) {
                    this.showToast('Update download complete.', 'info', 1800);
                } else if (event === 'error') {
                    this.showToast('Update check failed. The app will keep running normally.', 'warn', 3200);
                }
                // 'checking', 'up-to-date', 'downloading', 'error' — silent
            });
        }
    },

    /**
     * Send a desktop notification via Electron's native Notification API.
     * No-ops gracefully in browser mode (no Electron).
     * @param {string} title
     * @param {string} body
     * @param {string} [route]  Intent to dispatch when user clicks the notification
     */
    osNotify(title, body, route = '') {
        if (window.electronAPI?.notify) {
            window.electronAPI.notify({ title, body, route });
        }
    },

    async _initNetworkMesh() {
        if (this._meshInitDone) return;
        this._meshInitDone = true;
        try {
            // Auto-join public_local (derives geohash from IP on backend)
            await fetch('/api/networks/join', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ networkType: 'public_local', label: 'Local' }),
            });
        } catch (_) { /* non-fatal */ }
        // Start background poll every 30s
        this._pollNetworkMessages();
        this._meshPollInterval = setInterval(() => this._pollNetworkMessages(), 30000);
    },

    async _pollNetworkMessages() {
        try {
            const res = await fetch('/api/networks');
            if (!res.ok) return;
            const data = await res.json();
            const networks = Array.isArray(data.networks) ? data.networks : [];
            let totalNew = 0;
            for (const net of networks) {
                const topic = net.topic;
                if (!topic) continue;
                const msgRes = await fetch(`/api/networks/messages?topic=${encodeURIComponent(topic)}`);
                if (!msgRes.ok) continue;
                const msgData = await msgRes.json();
                const msgs = Array.isArray(msgData.messages) ? msgData.messages : [];
                const lastSeen = this._meshLastSeen?.[topic] || 0;
                const newCount = msgs.filter(m => (m.receivedAt || 0) > lastSeen).length;
                totalNew += newCount;
                if (msgs.length) {
                    const latest = msgs[msgs.length - 1];
                    if (!this._meshLastSeen) this._meshLastSeen = {};
                    this._meshLastSeen[topic] = latest.receivedAt || Date.now();
                    if (newCount > 0) {
                        const sender = String(latest.from || 'Someone').slice(0, 40);
                        const preview = String(latest.text || latest.content || '').slice(0, 80);
                        this.osNotify(`Message from ${sender}`, preview || 'New message', 'show local mesh');
                    }
                }
            }
            this._updateMeshBadge(totalNew);
        } catch (_) { /* non-fatal */ }
    },

    _updateMeshBadge(count) {
        let badge = document.getElementById('genome-mesh-badge');
        if (!badge) {
            badge = document.createElement('button');
            badge.id = 'genome-mesh-badge';
            badge.type = 'button';
            badge.title = 'Genome Mesh';
            badge.setAttribute('aria-label', 'Open Genome Mesh');
            badge.addEventListener('click', () => {
                this.input.value = 'show local mesh';
                this.handleSubmit();
            });
            document.body.appendChild(badge);
        }
        badge.className = 'mesh-badge' + (count > 0 ? ' mesh-badge--active' : '');
        badge.innerHTML = count > 0
            ? `<span class="mesh-badge-icon">◉</span><span class="mesh-badge-count">${count}</span>`
            : `<span class="mesh-badge-icon">◉</span>`;
    },

    // ─── Functional Surfaces ────────────────────────────────────────────────

    _showConfirm(remote, originalIntent) {
        this._dismissConfirm(); // clear any existing
        SoundEngine.confirm();
        const summary = remote.summary || remote.confirmSummary || remote.message || 'This action requires confirmation.';
        const op = remote.op || remote.confirmOp || '';
        const overlay = document.createElement('div');
        overlay.className = 'confirm-overlay';
        overlay.id = 'genome-confirm-overlay';
        overlay.innerHTML = `
            <div class="confirm-card">
                <div class="confirm-icon">⚠️</div>
                <div class="confirm-title">Confirm Action</div>
                <div class="confirm-summary">${escapeHtml(summary)}</div>
                ${op ? `<div class="confirm-summary" style="font-size:12px;font-family:monospace;opacity:0.6">${escapeHtml(op)}</div>` : ''}
                <div class="confirm-actions">
                    <button class="confirm-cancel" id="confirm-cancel-btn">Cancel</button>
                    <button class="confirm-approve" id="confirm-approve-btn">Approve</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);
        document.getElementById('confirm-cancel-btn').addEventListener('click', () => {
            SoundEngine.click();
            this._dismissConfirm();
        });
        document.getElementById('confirm-approve-btn').addEventListener('click', () => {
            SoundEngine.click();
            this._dismissConfirm();
            // Re-submit with confirmed flag appended to body via extras
            this._processTurnConfirmed(originalIntent);
        });
    },

    _dismissConfirm() {
        document.getElementById('genome-confirm-overlay')?.remove();
    },

    async _processTurnConfirmed(text) {
        this.container.classList.add('refracting');
        this.inputContainer.classList.add('active-intent');
        const startTime = performance.now();
        try {
            this.state.session.isApplyingLocalTurn = true;
            const activeSurface = this.state.activeSurface;
            const activeContent = activeSurface?.getData
                ? { domain: activeSurface.domain, name: activeSurface.name, data: activeSurface.getData() }
                : null;
            const remote = await RemoteTurnService.process(
                text,
                this.state.session.sessionId,
                this.state.session.revision,
                this.state.session.deviceId,
                'rebase_if_commutative',
                `${this.state.session.deviceId}:${Date.now().toString(36)}:c`,
                activeContent,
                true // confirmed
            );
            const domain = remote.envelope?.uiIntent?.kind || '';
            if (domain) SemanticCache.invalidate(domain);
            this.state.memory = remote.memory || this.state.memory;
            const envelope = remote.envelope;
            const execution = remote.execution;
            const kernelTrace = remote.kernelTrace || this.deriveKernelTrace(execution, remote.route);
            const safePlan = UIPlanSchema.normalize(remote.plan);
            this.state.session.sessionId = remote.sessionId || this.state.session.sessionId;
            this.state.session.revision = Number(remote.revision || this.state.session.revision);
            this.state.session.lastEnvelope = envelope;
            this.state.session.lastExecution = execution;
            this.state.session.lastKernelTrace = kernelTrace;
            this.render(safePlan, envelope, kernelTrace);
            SoundEngine.transition();
            this.pushHistory(text, envelope, execution, safePlan, 'confirmed', kernelTrace, null);
            this.state.metrics.latency = Math.round(performance.now() - startTime);
            this.updateStatus(execution.ok ? 'STABLE:CONFIRMED' : 'NEEDS INPUT');
            this.showToast(execution.message || 'Action confirmed.', execution.ok ? 'ok' : 'warn');
            this.saveState();
        } catch (err) {
            SoundEngine.error();
            this.showToast('Confirmed action failed.', 'warn', 3000);
            this.handleTransportFailure('turn', err);
        } finally {
            this.state.session.isApplyingLocalTurn = false;
            this.container.classList.remove('refracting');
            this.inputContainer.classList.remove('active-intent');
        }
    },

    _applyUpdatedContent(updatedContent) {
        const surf = this.state.activeSurface;
        if (!surf) return;
        if (surf.domain === 'document') {
            const body = this.container.querySelector('[data-func-domain="document"]');
            if (body && typeof updatedContent === 'string') {
                // Preserve cursor position around the update
                const sel = window.getSelection();
                const hadFocus = body.contains(sel?.anchorNode);
                body.innerHTML = updatedContent;
                if (hadFocus) body.focus();
                this._contentSave(surf.domain, surf.name, updatedContent);
            }
        } else if (surf.domain === 'spreadsheet') {
            const table = this.container.querySelector('[data-func-domain="spreadsheet"]');
            if (table && typeof updatedContent === 'object') {
                for (const [id, raw] of Object.entries(updatedContent)) {
                    const td = table.querySelector(`td[data-cell="${id}"]`);
                    if (td) { td.textContent = raw; }
                }
            }
        } else if (surf.domain === 'code') {
            const textarea = this.container.querySelector('.func-code-textarea');
            if (textarea && typeof updatedContent === 'string') {
                textarea.value = updatedContent;
                this._contentSave(surf.domain, surf.name, updatedContent);
            }
        } else if (surf.domain === 'presentation') {
            const slide = this.container.querySelector('[data-func-domain="presentation"]');
            if (slide && typeof updatedContent === 'string') {
                slide.innerHTML = updatedContent;
            }
        }
    },

    syncActiveSurfaceWorkspaceFocus() {
        const activeSurface = this.state.activeSurface;
        if (!activeSurface || typeof activeSurface !== 'object') return;
        const meta = typeof activeSurface.getMeta === 'function' ? activeSurface.getMeta() : null;
        if (!meta || typeof meta !== 'object') return;
        const domain = String(meta.domain || activeSurface.domain || '').trim();
        const name = String(meta.name || activeSurface.name || '').trim();
        if (!domain || !name) return;
        const itemId = String(meta.itemId || '').trim();
        const branch = String(meta.branch || this.state.session.workspace?.branch || 'main').trim() || 'main';
        const updatedAt = Number(meta.updatedAt || Date.now());
        this.state.session.workspace = this.state.session.workspace || { repoId: 'user-global', branch: 'main', worktrees: {}, activeContent: null };
        const worktrees = (this.state.session.workspace.worktrees && typeof this.state.session.workspace.worktrees === 'object')
            ? this.state.session.workspace.worktrees
            : {};
        worktrees[itemId || `${domain}:${name}`] = {
            itemId,
            name,
            domain,
            branch,
            updatedAt,
        };
        this.state.session.workspace.worktrees = worktrees;
        this.state.session.workspace.branch = branch;
        this.state.session.workspace.activeContent = {
            itemId,
            name,
            domain,
            branch,
            hash: String(meta.hash || '').trim(),
            updatedAt,
        };
    },

    _initFunctionalSurfaces() {
        // Tear down previous surface (clear auto-save timer, etc.)
        this.syncActiveSurfaceWorkspaceFocus();
        if (this.state.activeSurface?.cleanup) this.state.activeSurface.cleanup();
        this.state.activeSurface = null;

        const docBody = this.container.querySelector('[data-func-domain="document"]');
        if (docBody) { this._initDocumentSurface(docBody); return; }

        const sheet = this.container.querySelector('[data-func-domain="spreadsheet"]');
        if (sheet) { this._initSpreadsheetSurface(sheet); return; }

        const codeWrap = this.container.querySelector('[data-func-domain="code"]');
        if (codeWrap) { this._initCodeSurface(codeWrap); return; }

        const termInput = this.container.querySelector('[data-terminal-input]');
        if (termInput) { this._initTerminalSurface(termInput); return; }

        const presSlide = this.container.querySelector('[data-func-domain="presentation"]');
        if (presSlide) { this._initPresentationSurface(presSlide); return; }

        const phoneEl = this.container.querySelector('[data-func-domain="telephony"]');
        if (phoneEl) { this._initTelephonySurface(phoneEl); return; }

        const autoConnectEl = this.container.querySelector('[data-auto-connect]');
        if (autoConnectEl) {
            const svc = String(autoConnectEl.dataset.autoConnect || '').trim();
            if (svc) setTimeout(() => this._oauthConnectService(svc), 150);
        }
    },

    _initTelephonySurface(el) {
        const callSid = String(el.dataset.callSid || '').trim();
        if (!callSid) return;

        const badge = el.querySelector('.phone-state-badge');
        const timerEl = el.querySelector('.phone-timer');
        const hangupBtn = el.querySelector('.phone-btn-hangup');
        const holdBtn = el.querySelector('.phone-btn-hold');
        const muteBtn = el.querySelector('.phone-btn-mute');
        const dtmfBtn = el.querySelector('.phone-btn-dtmf');
        const dtmfPad = el.querySelector('.phone-dtmf-pad');

        // ── Timer ──────────────────────────────────────────────────────────────
        let timerInterval = null;
        const startTimer = (epochStart) => {
            clearInterval(timerInterval);
            timerEl.dataset.start = String(epochStart || Date.now());
            timerEl.dataset.active = '1';
            timerInterval = setInterval(() => {
                const elapsed = Math.floor((Date.now() - Number(timerEl.dataset.start)) / 1000);
                const m = String(Math.floor(elapsed / 60)).padStart(2, '0');
                const s = String(elapsed % 60).padStart(2, '0');
                timerEl.textContent = `${m}:${s}`;
            }, 1000);
        };
        const stopTimer = () => {
            clearInterval(timerInterval);
            timerEl.dataset.active = '0';
        };
        if (el.dataset.callState === 'active') startTimer(Date.now());

        // ── State sync ─────────────────────────────────────────────────────────
        const applyState = (state) => {
            if (badge) { badge.dataset.state = state; badge.textContent = state; }
            el.dataset.callState = state;
            if (state === 'active' && timerEl.dataset.active !== '1') startTimer(Date.now());
            if (state === 'held' || state === 'ended') stopTimer();
            if (holdBtn) {
                holdBtn.dataset.held = state === 'held' ? '1' : '0';
                holdBtn.title = state === 'held' ? 'Resume' : 'Hold';
            }
        };

        const onPhoneState = (evt) => {
            const d = evt.detail || {};
            if (d.callSid !== callSid) return;
            applyState(String(d.state || ''));
        };
        document.addEventListener('phoneStateUpdate', onPhoneState);

        // ── Controls ───────────────────────────────────────────────────────────
        const token = () => sessionStorage.getItem('genome_session') || '';
        const callApi = async (path, body) => {
            try {
                const r = await fetch(path, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Genome-Auth': token() },
                    body: JSON.stringify(body || {}),
                });
                return r.ok ? await r.json() : { ok: false };
            } catch { return { ok: false }; }
        };

        if (hangupBtn) {
            hangupBtn.addEventListener('click', async () => {
                hangupBtn.disabled = true;
                await callApi(`/api/telephony/hangup/${encodeURIComponent(callSid)}`);
                applyState('ended');
            });
        }

        if (holdBtn) {
            holdBtn.addEventListener('click', async () => {
                const isHeld = el.dataset.callState === 'held';
                const res = await callApi(
                    `/api/telephony/hold/${encodeURIComponent(callSid)}`,
                    { hold: !isHeld }
                );
                if (res.ok) applyState(res.state || (isHeld ? 'active' : 'held'));
            });
        }

        if (muteBtn) {
            // Mute is client-side only — MediaStream track mute (no Twilio API needed)
            muteBtn.addEventListener('click', () => {
                const muted = muteBtn.dataset.muted === '1';
                muteBtn.dataset.muted = muted ? '0' : '1';
                muteBtn.title = muted ? 'Mute' : 'Unmute';
                muteBtn.classList.toggle('active', !muted);
            });
        }

        if (dtmfBtn && dtmfPad) {
            dtmfBtn.addEventListener('click', () => {
                const hidden = dtmfPad.hidden;
                dtmfPad.hidden = !hidden;
                dtmfBtn.classList.toggle('active', hidden);
            });
            dtmfPad.addEventListener('click', async (e) => {
                const key = e.target.closest('.phone-dtmf-key');
                if (!key) return;
                const digit = String(key.dataset.digit || '');
                key.classList.add('pressed');
                setTimeout(() => key.classList.remove('pressed'), 150);
                await callApi(
                    `/api/telephony/dtmf/${encodeURIComponent(callSid)}`,
                    { digits: digit }
                );
            });
        }

        this.state.activeSurface = {
            domain: 'telephony',
            cleanup: () => {
                clearInterval(timerInterval);
                document.removeEventListener('phoneStateUpdate', onPhoneState);
            },
        };
    },

    async _contentLoad(domain, name) {
        if (!name) return null;
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const sessionId = encodeURIComponent(this.state.session.sessionId || '');
            const branch = encodeURIComponent(this._contentBranchFor(domain, name));
            const res = await fetch(`/api/content/${encodeURIComponent(domain)}/${encodeURIComponent(name)}?sessionId=${sessionId}&branch=${branch}`, {
                headers: { 'X-Genome-Auth': token }
            });
            if (!res.ok) return null;
            const json = await res.json();
            return json.ok ? (json.item?.data ?? null) : null;
        } catch { return null; }
    },

    async _contentOpenItem(domain, name) {
        if (!name) return null;
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const sessionId = encodeURIComponent(this.state.session.sessionId || '');
            const branch = encodeURIComponent(this._contentBranchFor(domain, name));
            const res = await fetch(`/api/content/${encodeURIComponent(domain)}/${encodeURIComponent(name)}?sessionId=${sessionId}&branch=${branch}`, {
                headers: { 'X-Genome-Auth': token }
            });
            if (!res.ok) return null;
            const json = await res.json();
            return json.ok ? (json.item || null) : null;
        } catch { return null; }
    },

    async _contentHistory(domain, name, limit = 20) {
        if (!name) return [];
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const branch = encodeURIComponent(this._contentBranchFor(domain, name));
            const res = await fetch(`/api/content/${encodeURIComponent(domain)}/${encodeURIComponent(name)}/history?branch=${branch}&limit=${Math.max(1, Math.min(100, Number(limit || 20)))}`, {
                headers: { 'X-Genome-Auth': token }
            });
            if (!res.ok) return [];
            const json = await res.json();
            return Array.isArray(json?.items) ? json.items : [];
        } catch { return []; }
    },

    async _contentBranches(domain, name) {
        if (!name) return [];
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const res = await fetch(`/api/content/${encodeURIComponent(domain)}/${encodeURIComponent(name)}/branches`, {
                headers: { 'X-Genome-Auth': token }
            });
            if (!res.ok) return [];
            const json = await res.json();
            return Array.isArray(json?.items) ? json.items : [];
        } catch { return []; }
    },

    async _fetchWorkspaceState() {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return this.state.session.workspace || null;
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/workspace`, {
                headers: { 'X-Genome-Auth': token }
            });
            if (!res.ok) return this.state.session.workspace || null;
            const json = await res.json();
            if (json?.workspace && typeof json.workspace === 'object') {
                this.state.session.workspace = json.workspace;
            }
            return this.state.session.workspace || null;
        } catch {
            return this.state.session.workspace || null;
        }
    },

    async _fetchWorkspaceWorktrees() {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return [];
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/workspace/worktrees`, {
                headers: { 'X-Genome-Auth': token }
            });
            if (!res.ok) return [];
            const json = await res.json();
            return Array.isArray(json?.items) ? json.items : [];
        } catch {
            return [];
        }
    },

    async _fetchNotificationsState() {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return Array.isArray(this.state.runtimeEvents) ? this.state.runtimeEvents : [];
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/notifications`);
            if (!res.ok) return Array.isArray(this.state.runtimeEvents) ? this.state.runtimeEvents : [];
            const json = await res.json();
            this.mergeRuntimeEvents(json?.notifications, { replace: true });
            return Array.isArray(this.state.runtimeEvents) ? this.state.runtimeEvents : [];
        } catch {
            return Array.isArray(this.state.runtimeEvents) ? this.state.runtimeEvents : [];
        }
    },

    async markNotificationsRead(app = '') {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        const target = String(app || '').trim().toLowerCase() === 'all' ? '' : String(app || '').trim();
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/notifications/read`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ app: target })
            });
            if (!res.ok) return false;
            const json = await res.json();
            this.mergeRuntimeEvents(json?.notifications, { replace: true });
            await this.showNotificationsInbox();
            return true;
        } catch {
            return false;
        }
    },

    async clearNotifications(app = '') {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        const target = String(app || '').trim().toLowerCase() === 'all' ? '' : String(app || '').trim();
        const suffix = target ? `?app=${encodeURIComponent(target)}` : '';
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/notifications${suffix}`, {
                method: 'DELETE'
            });
            if (!res.ok) return false;
            const json = await res.json();
            this.mergeRuntimeEvents(json?.notifications, { replace: true });
            await this.showNotificationsInbox();
            return true;
        } catch {
            return false;
        }
    },

    async setConnectorGrant(scope, enabled, ttlMs = 0) {
        const targetScope = String(scope || '').trim();
        if (!targetScope) return false;
        try {
            const res = await fetch('/api/connectors/grants', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ scope: targetScope, enabled: Boolean(enabled), ttlMs: Math.max(0, Number(ttlMs || 0)) })
            });
            if (!res.ok) return false;
            await this.showConnectionsPanel();
            return true;
        } catch {
            return false;
        }
    },

    async clearContinuityAlerts() {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/continuity/alerts/clear`, { method: 'POST' });
            if (!res.ok) return false;
            await this.showContinuitySurface();
            return true;
        } catch {
            return false;
        }
    },

    async drillContinuityAlert() {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/continuity/alerts/drill`, { method: 'POST' });
            if (!res.ok) return false;
            await this.showContinuitySurface();
            return true;
        } catch {
            return false;
        }
    },

    async prunePresence(all = false, maxAgeMs = 120000) {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/presence/prune`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ all: Boolean(all), maxAgeMs: Math.max(1000, Number(maxAgeMs || 120000)) })
            });
            if (!res.ok) return false;
            const json = await res.json();
            this.state.session.presence = json || this.state.session.presence;
            await this.showContinuitySurface();
            return true;
        } catch {
            return false;
        }
    },

    async setContinuityAutopilot(enabled) {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: Boolean(enabled) })
            });
            if (!res.ok) return false;
            await this.showContinuitySurface();
            return true;
        } catch {
            return false;
        }
    },

    async tickContinuityAutopilot() {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/tick`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ force: true })
            });
            if (!res.ok) return false;
            await this.showContinuitySurface();
            return true;
        } catch {
            return false;
        }
    },

    async applyRecommendedContinuityMode() {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/mode/apply-recommended`, {
                method: 'POST'
            });
            if (!res.ok) return false;
            await this.showContinuitySurface();
            return true;
        } catch {
            return false;
        }
    },

    async applyContinuityNextAction() {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/continuity/next/apply`, {
                method: 'POST'
            });
            if (!res.ok) return false;
            await this.showContinuitySurface();
            return true;
        } catch {
            return false;
        }
    },

    async configureContinuityAutopilot(options = {}) {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid || !options || typeof options !== 'object') return false;
        const body = {};
        if (options.mode != null) body.mode = String(options.mode || '').trim().toLowerCase();
        if (options.cooldownMs != null) body.cooldownMs = Math.max(1000, Number(options.cooldownMs || 0));
        if (options.maxAppliesPerHour != null) body.maxAppliesPerHour = Math.max(0, Number(options.maxAppliesPerHour || 0));
        if (options.autoAlignMode != null) body.autoAlignMode = Boolean(options.autoAlignMode);
        if (!Object.keys(body).length) return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/config`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            if (!res.ok) return false;
            await this.showContinuitySurface();
            return true;
        } catch {
            return false;
        }
    },

    async resetContinuityAutopilot(clearHistory = false) {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/reset`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ clearHistory: Boolean(clearHistory) })
            });
            if (!res.ok) return false;
            await this.showContinuitySurface();
            return true;
        } catch {
            return false;
        }
    },

    async applyContinuityPostureAction(index = 1) {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/posture/actions/apply?index=${Math.max(1, Number(index || 1))}`, {
                method: 'POST'
            });
            if (!res.ok) return false;
            await this.showContinuitySurface();
            return true;
        } catch {
            return false;
        }
    },

    async applyContinuityPostureBatch(limit = 3) {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/posture/actions/apply-batch?limit=${Math.max(1, Math.min(10, Number(limit || 3)))}`, {
                method: 'POST'
            });
            if (!res.ok) return false;
            await this.showContinuitySurface();
            return true;
        } catch {
            return false;
        }
    },

    async openShellObject(spec = {}) {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid || !spec || typeof spec !== 'object') return false;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/objects/open`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(spec)
            });
            if (!res.ok) return false;
            const json = await res.json();
            if (!json?.ok) return false;
            if (json?.data?.workspace && typeof json.data.workspace === 'object') {
                this.state.session.workspace = json.data.workspace;
            }
            if (json?.revision != null) {
                this.state.session.revision = Number(json.revision || this.state.session.revision);
            }
            const target = (json?.data?.target && typeof json.data.target === 'object') ? json.data.target : {};
            if (String(target.type || '') === 'repo') {
                return await this.openRepoObject(
                    String(target.domain || spec.domain || '').trim(),
                    String(target.name || spec.name || '').trim(),
                    String(target.branch || spec.branch || '').trim()
                );
            }
            if (String(target.type || '') === 'scene') {
                const scene = String(target.scene || spec.scene || '').trim().toLowerCase();
                const service = String(target.service || spec.service || '').trim().toLowerCase();
                if (scene === 'connectors' && service) return await this.showConnectionsPanel(service);
                return await this.switchToSceneDomain(scene);
            }
            return false;
        } catch {
            return false;
        }
    },

    async startSessionHandoff(targetSurfaceId = '') {
        const sid = String(this.state.session.sessionId || '').trim();
        const deviceId = String(this.state.session.deviceId || '').trim();
        if (!sid || !deviceId) return null;
        try {
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/handoff/start`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ deviceId, targetSurfaceId: String(targetSurfaceId || '').trim() || null })
            });
            if (!res.ok) return null;
            const json = await res.json();
            const backendUrl = this.resolveHandoffBackendUrl(json?.backendUrl || '');
            const bridgeUrl = String(json?.bridgeUrl || '').trim();
            if (json?.handoff && typeof json.handoff === 'object') {
                this.state.session.handoff = json.handoff;
                if (this.state.session.handoff?.pending && typeof this.state.session.handoff.pending === 'object') {
                    this.state.session.handoff.pending.backendUrl = backendUrl;
                    this.state.session.handoff.pending.bridgeUrl = bridgeUrl;
                }
            }
            if (json?.revision != null) {
                this.state.session.revision = Number(json.revision || this.state.session.revision);
            }
            if (json && typeof json === 'object') {
                json.backendUrl = backendUrl;
                json.bridgeUrl = bridgeUrl;
            }
            this.saveState();
            return json;
        } catch {
            return null;
        }
    },

    async preferPairedSurface(surfaceId = '') {
        const clean = String(surfaceId || '').trim();
        if (!clean) return false;
        try {
            const res = await fetch(`/api/surfaces/${encodeURIComponent(clean)}/prefer`, { method: 'POST' });
            return !!res.ok;
        } catch {
            return false;
        }
    },

    resolveHandoffBackendUrl(raw = '') {
        const value = String(raw || '').trim().replace(/\/+$/, '');
        if (value) return rewriteLoopbackOrigin(value, '8787');
        return rewriteLoopbackOrigin(`${window.location.protocol}//${window.location.hostname}:8787`, '8787');
    },

    resolvePhoneSurfaceUrl(backendUrl = '') {
        const backendBase = this.resolveHandoffBackendUrl(backendUrl);
        const frontendOrigin = rewriteLoopbackOrigin(window.location.origin, window.location.port || '5173');
        try {
            const fromBackend = new URL(backendBase);
            fromBackend.port = window.location.port || '5173';
            return fromBackend.origin;
        } catch {
            return frontendOrigin;
        }
    },

    buildHandoffShareUrl(token = '', backendUrl = '', bridgeUrl = '') {
        const cleanToken = String(token || '').trim();
        const sid = String(this.state.session.sessionId || '').trim();
        if (!cleanToken || !sid) return '';
        const phoneBase = this.resolvePhoneSurfaceUrl(backendUrl);
        const backendBase = this.resolveHandoffBackendUrl(backendUrl);
        return `${phoneBase}/?session=${encodeURIComponent(sid)}&handoff=${encodeURIComponent(cleanToken)}&backend=${encodeURIComponent(backendBase)}`;
    },

    async showHandoffQr(link = '', expiresAt = 0) {
        const cleanLink = String(link || '').trim();
        if (!cleanLink) return false;
        document.getElementById('genome-handoff-qr-overlay')?.remove();
        const overlay = document.createElement('div');
        overlay.className = 'handoff-qr-overlay';
        overlay.id = 'genome-handoff-qr-overlay';
        overlay.innerHTML = `
            <div class="handoff-qr-card">
                <div class="handoff-qr-title">Open Genome On Phone</div>
                <div class="handoff-qr-sub">Scan to open the phone PWA surface and claim this session.</div>
                <div class="handoff-qr-frame">
                    <img class="handoff-qr-image" id="genome-handoff-qr-image" alt="Nous handoff QR code" />
                </div>
                <div class="handoff-qr-meta">${escapeHtml(cleanLink)}</div>
                ${Number(expiresAt || 0) > 0 ? `<div class="handoff-qr-expiry">Expires ${escapeHtml(formatDate(Number(expiresAt)))}</div>` : ''}
                <div class="handoff-qr-actions">
                    <button class="confirm-cancel" id="handoff-qr-close-btn">Close</button>
                    <button class="confirm-approve" id="handoff-qr-copy-btn">Copy Link</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);

        const dismiss = () => document.getElementById('genome-handoff-qr-overlay')?.remove();
        document.getElementById('handoff-qr-close-btn')?.addEventListener('click', dismiss);
        overlay.addEventListener('click', (event) => {
            if (event.target === overlay) dismiss();
        });
        document.getElementById('handoff-qr-copy-btn')?.addEventListener('click', async () => {
            const ok = await this.copyTextToClipboard(cleanLink);
            this.showToast(ok ? 'Handoff link copied.' : 'Could not copy handoff link.', ok ? 'ok' : 'warn', 2200);
        });

        try {
            const dataUrl = await QRCode.toDataURL(cleanLink, {
                errorCorrectionLevel: 'H',
                margin: 3,
                width: 320,
                color: {
                    dark: '#000000',
                    light: '#ffffff'
                }
            });
            const img = document.getElementById('genome-handoff-qr-image');
            if (img) img.src = dataUrl;
            return true;
        } catch {
            dismiss();
            this.showToast('Could not generate handoff QR.', 'warn', 2400);
            return false;
        }
    },

    async copyTextToClipboard(text) {
        const value = String(text || '').trim();
        if (!value) return false;
        try {
            await navigator.clipboard.writeText(value);
            return true;
        } catch {
            const probe = document.createElement('textarea');
            probe.value = value;
            probe.setAttribute('readonly', 'readonly');
            probe.style.position = 'fixed';
            probe.style.opacity = '0';
            document.body.appendChild(probe);
            probe.select();
            const ok = document.execCommand('copy');
            probe.remove();
            return Boolean(ok);
        }
    },

    async runDirectShellCommand(rawCommand) {
        const command = String(rawCommand || '').trim();
        if (!command) return false;
        const lower = command.toLowerCase();

        if (lower === 'show my notifications' || lower === 'show notifications') {
            return await this.showNotificationsInbox();
        }
        if (lower === 'show content history' || lower === 'show content') {
            return await this.showContentRepository();
        }
        if (lower === 'show workspace files') {
            return await this.showWorkspaceFiles('.');
        }
        if (lower === 'show my connections' || lower === 'show connections') {
            return await this.showConnectionsPanel();
        }
        if (lower === 'show continuity') {
            return await this.showContinuitySurface();
        }
        if (lower === 'show runtime health' || lower === 'show runtime') {
            return await this.showRuntimeHealth();
        }
        if (lower === 'check my email' || lower === 'show email') {
            return await this.showEmailInbox();
        }
        if (lower === 'show messages' || lower === 'show messaging') {
            return await this.showMessagingInbox();
        }
        if (lower === 'show calendar') {
            return await this.showCalendarAgenda();
        }

        let match = lower.match(/^show connections for\s+([a-z0-9_-]+)$/i);
        if (match) {
            return await this.showConnectionsPanel(match[1]);
        }

        match = command.match(/^show history for\s+(document|spreadsheet|presentation)\s+(.+)$/i);
        if (match) {
            const [, domain, name] = match;
            return await this.showRepoHistory(domain, String(name || '').trim());
        }

        match = command.match(/^list files(?:\s+(.+))?$/i);
        if (match) {
            return await this.showWorkspaceFiles(String(match[1] || '.').trim() || '.');
        }

        match = command.match(/^search drive for\s+(.+)$/i);
        if (match) {
            return await this.showDriveFiles(String(match[1] || '').trim());
        }

        return false;
    },

    async _contentSave(domain, name, data) {
        if (!name) return;
        const statusEl = this.container.querySelector('[data-save-status]');
        if (statusEl) { statusEl.textContent = 'Saving…'; statusEl.className = 'func-save-status saving'; }
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const sessionId = encodeURIComponent(this.state.session.sessionId || '');
            const branch = encodeURIComponent(this._contentBranchFor(domain, name));
            const res = await fetch(`/api/content/${encodeURIComponent(domain)}/${encodeURIComponent(name)}?sessionId=${sessionId}&branch=${branch}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Genome-Auth': token },
                body: JSON.stringify({ data })
            });
            const json = res.ok ? await res.json() : null;
            const ok = Boolean(res.ok && json?.ok);
            if (ok && json?.item) {
                const item = json.item;
                this.state.session.workspace = this.state.session.workspace || { repoId: 'user-global', branch: 'main', worktrees: {}, activeContent: null };
                this.state.session.workspace.branch = String(item.branch || this.state.session.workspace.branch || 'main');
                this.state.session.workspace.activeContent = {
                    itemId: String(item.itemId || ''),
                    name: String(item.name || name),
                    domain: String(item.domain || domain),
                    branch: String(item.branch || 'main'),
                    hash: String(item.hash || ''),
                    updatedAt: Number(item.updated_at || Date.now())
                };
                this._renderSurfaceRepoMeta(domain, name);
            }
            if (statusEl) { statusEl.textContent = ok ? 'Saved' : 'Error'; statusEl.className = `func-save-status${ok ? '' : ' error'}`; }
        } catch {
            if (statusEl) { statusEl.textContent = 'Error'; statusEl.className = 'func-save-status error'; }
        }
    },

    _activeContentMeta(domain, fallbackName = '') {
        const workspace = this.state.session.workspace || {};
        const active = (workspace.activeContent && typeof workspace.activeContent === 'object') ? workspace.activeContent : null;
        if (active && String(active.domain || '').trim() === String(domain || '').trim()) {
            return {
                itemId: String(active.itemId || ''),
                name: String(active.name || fallbackName || ''),
                domain: String(active.domain || domain || ''),
                branch: String(active.branch || workspace.branch || 'main'),
                hash: String(active.hash || ''),
                updatedAt: Number(active.updatedAt || 0)
            };
        }
        return {
            itemId: '',
            name: String(fallbackName || ''),
            domain: String(domain || ''),
            branch: String(workspace.branch || 'main'),
            hash: '',
            updatedAt: 0
        };
    },

    _contentBranchFor(domain, name = '') {
        const meta = this._activeContentMeta(domain, name);
        return String(meta.branch || this.state.session.workspace?.branch || 'main');
    },

    _renderSurfaceRepoMeta(domain, fallbackName = '') {
        const meta = this._activeContentMeta(domain, fallbackName);
        const branchEls = this.container.querySelectorAll('.func-branch-badge');
        const hashEls = this.container.querySelectorAll('.func-hash-badge');
        for (const el of branchEls) el.textContent = meta.branch || 'main';
        for (const el of hashEls) el.textContent = meta.hash ? meta.hash.slice(0, 8) : 'new';
        return meta;
    },

    async createRepoBranch(domain, name, targetBranch) {
        const itemDomain = String(domain || '').trim().toLowerCase();
        const itemName = String(name || '').trim();
        const nextBranch = String(targetBranch || '').trim();
        if (!itemDomain || !itemName || !nextBranch) return null;
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const sessionId = encodeURIComponent(this.state.session.sessionId || '');
            const fromBranch = encodeURIComponent(this._contentBranchFor(itemDomain, itemName));
            const res = await fetch(`/api/content/${encodeURIComponent(itemDomain)}/${encodeURIComponent(itemName)}/branch?sessionId=${sessionId}&branch=${fromBranch}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Genome-Auth': token },
                body: JSON.stringify({ targetBranch: nextBranch })
            });
            if (!res.ok) return null;
            const json = await res.json();
            const item = json?.item || null;
            if (!item) return null;
            this.state.session.workspace = this.state.session.workspace || { repoId: 'user-global', branch: 'main', worktrees: {}, activeContent: null };
            this.state.session.workspace.branch = String(item.branch || nextBranch);
            if (this.state.session.workspace.activeContent && typeof this.state.session.workspace.activeContent === 'object') {
                this.state.session.workspace.activeContent.branch = String(item.branch || nextBranch);
                this.state.session.workspace.activeContent.hash = String(item.hash || this.state.session.workspace.activeContent.hash || '');
            }
            this.saveState();
            return item;
        } catch {
            return null;
        }
    },

    async revertRepoObject(domain, name, targetHash) {
        const itemDomain = String(domain || '').trim().toLowerCase();
        const itemName = String(name || '').trim();
        const hash = String(targetHash || '').trim();
        if (!itemDomain || !itemName || !hash) return null;
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const sessionId = encodeURIComponent(this.state.session.sessionId || '');
            const branch = encodeURIComponent(this._contentBranchFor(itemDomain, itemName));
            const res = await fetch(`/api/content/${encodeURIComponent(itemDomain)}/${encodeURIComponent(itemName)}/revert?sessionId=${sessionId}&branch=${branch}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Genome-Auth': token },
                body: JSON.stringify({ hash })
            });
            if (!res.ok) return null;
            const json = await res.json();
            return json?.item || null;
        } catch {
            return null;
        }
    },

    async mergeRepoBranch(domain, name, sourceBranch, targetBranch = '') {
        const itemDomain = String(domain || '').trim().toLowerCase();
        const itemName = String(name || '').trim();
        const source = String(sourceBranch || '').trim();
        const target = String(targetBranch || this._contentBranchFor(itemDomain, itemName)).trim() || 'main';
        if (!itemDomain || !itemName || !source || source === target) return null;
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const sessionId = encodeURIComponent(this.state.session.sessionId || '');
            const branch = encodeURIComponent(target);
            const res = await fetch(`/api/content/${encodeURIComponent(itemDomain)}/${encodeURIComponent(itemName)}/merge?sessionId=${sessionId}&branch=${branch}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Genome-Auth': token },
                body: JSON.stringify({ sourceBranch: source })
            });
            if (!res.ok) return null;
            const json = await res.json();
            return json?.item || null;
        } catch {
            return null;
        }
    },

    async showRepoHistory(domain, name) {
        const itemDomain = String(domain || '').trim().toLowerCase();
        const itemName = String(name || '').trim();
        if (!itemDomain || !itemName) return false;
        const history = await this._contentHistory(itemDomain, itemName, 30);
        const branches = await this._contentBranches(itemDomain, itemName);
        const branch = this._contentBranchFor(itemDomain, itemName);
        const intent = `history for ${itemDomain} ${itemName}`;
        const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
        const plan = UIPlanSchema.normalize({
            title: 'Repository history',
            subtitle: `${itemDomain} · ${itemName}`,
            suggestions: [],
            trace: {}
        });
        const execution = {
            ok: true,
            toolResults: [{
                ok: true,
                op: 'content_history',
                message: `History for ${itemName}`,
                data: {
                    op: 'content_history',
                    action: 'history',
                    name: itemName,
                    type: itemDomain,
                    branch,
                    branches,
                    history,
                    source: 'live',
                    connected: true,
                    authoritative: true,
                }
            }]
        };
        this.state.session.lastIntent = intent;
        this.state.session.lastExecution = execution;
        this.render(plan, envelope, this.state.session.lastKernelTrace);
        this.saveState();
        return true;
    },

    async showWorkspaceWorktrees() {
        const workspace = await this._fetchWorkspaceState();
        const worktrees = await this._fetchWorkspaceWorktrees();
        const branch = String(workspace?.branch || 'main');
        const runtimeFocus = (workspace?.activeContent && typeof workspace.activeContent === 'object') ? workspace.activeContent : null;
        const presence = this.state.session.presence || {};
        const handoff = this.state.session.handoff || {};
        const intent = 'show workspace worktrees';
        const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
        const plan = UIPlanSchema.normalize({
            title: 'Workspace worktrees',
            subtitle: `session · ${branch}`,
            suggestions: [],
            trace: {}
        });
        const execution = {
            ok: true,
            toolResults: [{
                ok: true,
                op: 'content_list',
                message: 'Workspace worktrees',
                data: {
                    op: 'content_list',
                    action: 'worktrees',
                    name: '',
                    type: '',
                    branch,
                    runtimeFocus,
                    presence,
                    handoff,
                    items: [],
                    worktrees,
                    source: 'live',
                    connected: true,
                    authoritative: true,
                }
            }]
        };
        this.state.session.lastIntent = intent;
        this.state.session.lastExecution = execution;
        this.render(plan, envelope, this.state.session.lastKernelTrace);
        this.saveState();
        return true;
    },

    async showNotificationsInbox() {
        const items = await this._fetchNotificationsState();
        const unread = items.filter((item) => !item.read).length;
        const intent = 'show my notifications';
        const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
        const plan = UIPlanSchema.normalize({
            title: 'Notification center',
            subtitle: unread > 0 ? `${unread} unread` : 'Runtime inbox',
            suggestions: [],
            trace: {}
        });
        const execution = {
            ok: true,
            toolResults: [{
                ok: true,
                op: 'notifications_view',
                message: unread > 0 ? `${unread} unread notifications` : 'Notifications',
                data: {
                    op: 'notifications_view',
                    notifications: items,
                    source: 'live',
                    authoritative: true,
                }
            }]
        };
        this.state.session.lastIntent = intent;
        this.state.session.lastExecution = execution;
        this.render(plan, envelope, this.state.session.lastKernelTrace);
        this.saveState();
        return true;
    },

    async showConnectionsPanel(targetService = '') {
        try {
            const suffix = targetService ? `?targetService=${encodeURIComponent(String(targetService).trim())}` : '';
            const [statusRes, providersRes, contractsRes] = await Promise.all([
                fetch(`/api/connectors/status${suffix}`),
                fetch('/api/connectors/providers'),
                fetch('/api/connectors/contracts'),
            ]);
            if (!statusRes.ok || !providersRes.ok || !contractsRes.ok) return false;
            const [payload, providersPayload, contractsPayload] = await Promise.all([
                statusRes.json(),
                providersRes.json(),
                contractsRes.json(),
            ]);
            if (payload?.data && typeof payload.data === 'object') {
                payload.data.providers = (providersPayload && typeof providersPayload === 'object') ? (providersPayload.providers || {}) : {};
                payload.data.contracts = (contractsPayload && typeof contractsPayload === 'object') ? (contractsPayload.contracts || {}) : {};
            }
            const focusedService = String(targetService || '').trim().toLowerCase();
            if (focusedService) {
                try {
                    const diagRes = await fetch(`/api/connectors/status/${encodeURIComponent(focusedService)}`);
                    if (diagRes.ok) {
                        const diagPayload = await diagRes.json();
                        if (payload?.data && typeof payload.data === 'object') {
                            payload.data.serviceDiagnostics = diagPayload?.data || null;
                        }
                    }
                } catch {
                    // Keep connections shell operable even if focused diagnostics fail.
                }
            }
            const execution = {
                ok: true,
                toolResults: [payload],
            };
            const intent = targetService ? `show connections for ${targetService}` : 'show my connections';
            const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
            const plan = UIPlanSchema.normalize({
                title: 'Connections',
                subtitle: 'Live connector state',
                suggestions: [],
                trace: {}
            });
            this.state.session.lastIntent = intent;
            this.state.session.lastExecution = execution;
            this.render(plan, envelope, this.state.session.lastKernelTrace);
            this.saveState();
            return true;
        } catch {
            return false;
        }
    },

    async showEmailInbox(limit = 10) {
        try {
            const res = await fetch(`/api/connectors/gmail/messages?limit=${Math.max(1, Math.min(25, Number(limit || 10)))}`);
            if (!res.ok) return false;
            const json = await res.json();
            const item = (json?.item && typeof json.item === 'object') ? json.item : {};
            const execution = {
                ok: true,
                toolResults: [{
                    ok: Boolean(item.ok !== false),
                    op: 'email_read',
                    message: item.ok === false ? String(item.error || 'Email unavailable') : 'Inbox',
                    data: {
                        ...item,
                        op: 'email_read',
                        action: 'read',
                    }
                }]
            };
            const plan = UIPlanSchema.normalize({
                title: 'Email',
                subtitle: item.connected === false ? 'Connect your inbox' : 'Live mailbox',
                suggestions: [],
                trace: {}
            });
            const intent = 'check my email';
            const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
            this.state.session.lastIntent = intent;
            this.state.session.lastExecution = execution;
            this.render(plan, envelope, this.state.session.lastKernelTrace);
            this.saveState();
            return true;
        } catch {
            return false;
        }
    },

    async showMessagingInbox() {
        try {
            const res = await fetch('/api/connectors/slack/channels');
            if (!res.ok) return false;
            const json = await res.json();
            const item = (json?.item && typeof json.item === 'object') ? json.item : {};
            const execution = {
                ok: true,
                toolResults: [{
                    ok: Boolean(item.ok !== false),
                    op: 'slack_read',
                    message: item.ok === false ? String(item.error || 'Messaging unavailable') : 'Messages',
                    data: {
                        ...item,
                        op: 'slack_read',
                        action: 'read',
                    }
                }]
            };
            const plan = UIPlanSchema.normalize({
                title: 'Messages',
                subtitle: item.connected === false ? 'Connect messaging' : 'Live channels',
                suggestions: [],
                trace: {}
            });
            const intent = 'show my messages';
            const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
            this.state.session.lastIntent = intent;
            this.state.session.lastExecution = execution;
            this.render(plan, envelope, this.state.session.lastKernelTrace);
            this.saveState();
            return true;
        } catch {
            return false;
        }
    },

    async showCalendarAgenda(days = 7) {
        try {
            const res = await fetch(`/api/connectors/gcal/events?days=${Math.max(1, Math.min(30, Number(days || 7)))}`);
            if (!res.ok) return false;
            const json = await res.json();
            const item = (json?.item && typeof json.item === 'object') ? json.item : {};
            const execution = {
                ok: true,
                toolResults: [{
                    ok: Boolean(item.ok !== false),
                    op: 'calendar_list',
                    message: item.ok === false ? String(item.error || 'Calendar unavailable') : 'Agenda',
                    data: {
                        ...item,
                        op: 'calendar_list',
                        action: 'read',
                    }
                }]
            };
            const plan = UIPlanSchema.normalize({
                title: 'Calendar',
                subtitle: item.connected === false ? 'Connect calendar' : 'Live agenda',
                suggestions: [],
                trace: {}
            });
            const intent = 'show my calendar';
            const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
            this.state.session.lastIntent = intent;
            this.state.session.lastExecution = execution;
            this.render(plan, envelope, this.state.session.lastKernelTrace);
            this.saveState();
            return true;
        } catch {
            return false;
        }
    },

    async showDriveFiles(query = '') {
        try {
            const suffix = query ? `?query=${encodeURIComponent(String(query).trim())}` : '';
            const res = await fetch(`/api/connectors/gdrive/files${suffix}`);
            if (!res.ok) return false;
            const json = await res.json();
            const item = (json?.item && typeof json.item === 'object') ? json.item : {};
            const execution = {
                ok: true,
                toolResults: [{
                    ok: Boolean(item.ok !== false),
                    op: 'gdrive.list',
                    message: item.ok === false ? String(item.error || 'Drive unavailable') : 'Drive files',
                    data: {
                        ...item,
                        op: 'gdrive.list',
                    }
                }]
            };
            const plan = UIPlanSchema.normalize({
                title: 'Drive',
                subtitle: item.connected === false ? 'Connect file storage' : 'Connector storage',
                suggestions: [],
                trace: {}
            });
            const intent = query ? `search drive for ${query}` : 'show my drive files';
            const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
            this.state.session.lastIntent = intent;
            this.state.session.lastExecution = execution;
            this.render(plan, envelope, this.state.session.lastKernelTrace);
            this.saveState();
            return true;
        } catch {
            return false;
        }
    },

    async showRuntimeHealth(limit = 120) {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const [healthRes, selfCheckRes, profileRes, servicesRes, diagnosticsRes] = await Promise.all([
                fetch(`/api/session/${encodeURIComponent(sid)}/runtime`),
                fetch(`/api/session/${encodeURIComponent(sid)}/runtime/self-check`),
                fetch(`/api/session/${encodeURIComponent(sid)}/runtime/profile?limit=${Math.max(10, Math.min(500, Number(limit || 120)))}`),
                fetch('/api/runtime/services'),
                fetch(`/api/session/${encodeURIComponent(sid)}/diagnostics`),
            ]);
            if (!healthRes.ok || !selfCheckRes.ok || !profileRes.ok || !servicesRes.ok || !diagnosticsRes.ok) return false;
            const [health, selfCheck, profile, services, diagnostics] = await Promise.all([
                healthRes.json(),
                selfCheckRes.json(),
                profileRes.json(),
                servicesRes.json(),
                diagnosticsRes.json(),
            ]);
            const execution = {
                ok: true,
                toolResults: [{
                    ok: true,
                    op: 'runtime_profile',
                    message: 'Runtime health',
                    data: {
                        op: 'runtime_profile',
                        health,
                        selfCheck,
                        profile,
                        services,
                        diagnostics,
                    }
                }]
            };
            const plan = UIPlanSchema.normalize({
                title: 'Runtime',
                subtitle: 'Shell health and latency',
                suggestions: [],
                trace: {}
            });
            const intent = 'show runtime health';
            const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
            this.state.session.lastIntent = intent;
            this.state.session.lastExecution = execution;
            this.render(plan, envelope, this.state.session.lastKernelTrace);
            this.saveState();
            return true;
        } catch {
            return false;
        }
    },

    async showContinuitySurface() {
        const sid = String(this.state.session.sessionId || '').trim();
        if (!sid) return false;
        try {
            const [
                continuityRes,
                alertsRes,
                handoffRes,
                presenceRes,
                incidentsRes,
                nextRes,
                diagnosticsRes,
                continuityHistoryRes,
                continuityAnomaliesRes,
                autopilotHistoryRes,
                autopilotPreviewRes,
                autopilotMetricsRes,
                autopilotGuardrailsRes,
                autopilotModeRes,
                autopilotDriftRes,
                autopilotAlignmentRes,
                autopilotPolicyMatrixRes,
                postureHistoryRes,
                postureAnomaliesRes,
                postureActionsRes,
                postureActionMetricsRes,
                posturePolicyMatrixRes,
                pairedSurfacesRes,
            ] = await Promise.all([
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/alerts`),
                fetch(`/api/session/${encodeURIComponent(sid)}/handoff/stats`),
                fetch(`/api/session/${encodeURIComponent(sid)}/presence`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/incidents?limit=8`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/next?limit=5`),
                fetch(`/api/session/${encodeURIComponent(sid)}/diagnostics`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/history?limit=8`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/anomalies?limit=8`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/history?limit=8`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/preview`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/metrics?window_ms=3600000`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/guardrails`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/mode-recommendation`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/mode-drift`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/mode-alignment?limit=8`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/mode-policy/matrix`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/posture/history?limit=8`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/posture/anomalies?limit=8`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/posture/actions?limit=5`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/posture/actions/metrics?window_ms=3600000`),
                fetch(`/api/session/${encodeURIComponent(sid)}/continuity/autopilot/posture/actions/policy-matrix?limit=5`),
                fetch('/api/surfaces?limit=12'),
            ]);
            if (
                !continuityRes.ok ||
                !alertsRes.ok ||
                !handoffRes.ok ||
                !presenceRes.ok ||
                !incidentsRes.ok ||
                !nextRes.ok ||
                !diagnosticsRes.ok ||
                !continuityHistoryRes.ok ||
                !continuityAnomaliesRes.ok ||
                !autopilotHistoryRes.ok ||
                !autopilotPreviewRes.ok ||
                !autopilotMetricsRes.ok ||
                !autopilotGuardrailsRes.ok ||
                !autopilotModeRes.ok ||
                !autopilotDriftRes.ok ||
                !autopilotAlignmentRes.ok ||
                !autopilotPolicyMatrixRes.ok ||
                !postureHistoryRes.ok ||
                !postureAnomaliesRes.ok ||
                !postureActionsRes.ok ||
                !postureActionMetricsRes.ok ||
                !posturePolicyMatrixRes.ok ||
                !pairedSurfacesRes.ok
            ) return false;
            const [
                continuity,
                alerts,
                handoff,
                presence,
                incidents,
                nextActions,
                diagnostics,
                continuityHistory,
                continuityAnomalies,
                autopilotHistory,
                autopilotPreview,
                autopilotMetrics,
                autopilotGuardrails,
                autopilotMode,
                autopilotDrift,
                autopilotAlignment,
                autopilotPolicyMatrix,
                postureHistory,
                postureAnomalies,
                postureActions,
                postureActionMetrics,
                posturePolicyMatrix,
                pairedSurfaces,
            ] = await Promise.all([
                continuityRes.json(),
                alertsRes.json(),
                handoffRes.json(),
                presenceRes.json(),
                incidentsRes.json(),
                nextRes.json(),
                diagnosticsRes.json(),
                continuityHistoryRes.json(),
                continuityAnomaliesRes.json(),
                autopilotHistoryRes.json(),
                autopilotPreviewRes.json(),
                autopilotMetricsRes.json(),
                autopilotGuardrailsRes.json(),
                autopilotModeRes.json(),
                autopilotDriftRes.json(),
                autopilotAlignmentRes.json(),
                autopilotPolicyMatrixRes.json(),
                postureHistoryRes.json(),
                postureAnomaliesRes.json(),
                postureActionsRes.json(),
                postureActionMetricsRes.json(),
                posturePolicyMatrixRes.json(),
                pairedSurfacesRes.json(),
            ]);
            const execution = {
                ok: true,
                toolResults: [{
                    ok: true,
                    op: 'continuity_alerts',
                    message: 'Continuity',
                    data: {
                        op: 'continuity_alerts',
                        continuity,
                        alerts,
                        handoff,
                        presence,
                        incidents,
                        nextActions,
                        diagnostics,
                        continuityHistory,
                        continuityAnomalies,
                        autopilotHistory,
                        autopilotPreview,
                        autopilotMetrics,
                        autopilotGuardrails,
                        autopilotMode,
                        autopilotDrift,
                        autopilotAlignment,
                        autopilotPolicyMatrix,
                        postureHistory,
                        postureAnomalies,
                        postureActions,
                        postureActionMetrics,
                        posturePolicyMatrix,
                        pairedSurfaces,
                    }
                }]
            };
            const plan = UIPlanSchema.normalize({
                title: 'Continuity',
                subtitle: 'Shared runtime and handoff',
                suggestions: [],
                trace: {}
            });
            const intent = 'show continuity';
            const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
            this.state.session.lastIntent = intent;
            this.state.session.lastExecution = execution;
            this.render(plan, envelope, this.state.session.lastKernelTrace);
            this.saveState();
            return true;
        } catch {
            return false;
        }
    },

    async showContentRepository(query = '') {
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const headers = token ? { 'X-Genome-Auth': token } : {};
            const suffix = query ? `?query=${encodeURIComponent(String(query).trim())}` : '';
            const domains = ['document', 'spreadsheet', 'presentation'];
            const responses = await Promise.all(domains.map(async (domain) => {
                const res = await fetch(`/api/content/${encodeURIComponent(domain)}${suffix}`, { headers });
                if (!res.ok) return [];
                const json = await res.json();
                return Array.isArray(json?.items) ? json.items.map((item) => ({ ...item, domain })) : [];
            }));
            const items = responses.flat().sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0));
            const workspace = await this._fetchWorkspaceState();
            const worktrees = await this._fetchWorkspaceWorktrees();
            const plan = UIPlanSchema.normalize({
                title: 'Content repository',
                subtitle: query ? `results for ${query}` : 'Repo objects',
                suggestions: [],
                trace: {}
            });
            const intent = query ? `find content ${query}` : 'show content history';
            const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
            const execution = {
                ok: true,
                toolResults: [{
                    ok: true,
                    op: 'content_list',
                    message: items.length ? `${items.length} repo objects` : 'No repo objects',
                    data: {
                        op: 'content_list',
                        action: query ? 'find' : 'list',
                        query: String(query || '').trim(),
                        items,
                        branch: String(workspace?.branch || 'main'),
                        worktrees,
                        source: 'live',
                        connected: true,
                        authoritative: true,
                    }
                }]
            };
            this.state.session.lastIntent = intent;
            this.state.session.lastExecution = execution;
            this.render(plan, envelope, this.state.session.lastKernelTrace);
            this.saveState();
            return true;
        } catch {
            return false;
        }
    },

    async showWorkspaceFiles(path = '.') {
        try {
            const execution = await RemoteTurnService.process(
                `list files ${String(path || '.').trim() || '.'}`,
                this.state.session.sessionId,
                this.state.session.revision,
                this.state.session.deviceId,
                'rebase_if_commutative',
                `files:${String(path || '.').trim() || '.'}:${Date.now()}`
            );
            this.state.session.revision = Number(execution.revision || this.state.session.revision);
            this.state.memory = execution.memory || this.state.memory;
            this.state.session.handoff = execution.handoff || this.state.session.handoff;
            this.state.session.presence = execution.presence || this.state.session.presence;
            this.state.session.workspace = execution.workspace || this.state.session.workspace;
            const plan = UIPlanSchema.normalize(execution.plan);
            const envelope = execution.envelope || IntentLayerCompiler.compile(`list files ${String(path || '.').trim() || '.'}`, this.state.memory);
            this.state.session.lastIntent = String(path || '.').trim() && String(path || '.').trim() !== '.'
                ? `list files ${String(path || '.').trim()}`
                : 'show workspace files';
            this.state.session.lastExecution = execution.execution || this.state.session.lastExecution;
            this.state.session.lastKernelTrace = execution.kernelTrace || this.state.session.lastKernelTrace;
            this.render(plan, envelope, this.state.session.lastKernelTrace);
            this.saveState();
            return true;
        } catch {
            return false;
        }
    },

    async activateWorkspaceWorktree(itemId) {
        const sid = String(this.state.session.sessionId || '').trim();
        const target = String(itemId || '').trim();
        if (!sid || !target) return null;
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/workspace/worktrees/${encodeURIComponent(target)}/activate`, {
                method: 'POST',
                headers: { 'X-Genome-Auth': token }
            });
            if (!res.ok) return null;
            const json = await res.json();
            if (json?.workspace && typeof json.workspace === 'object') {
                this.state.session.workspace = json.workspace;
            }
            return json?.item || null;
        } catch {
            return null;
        }
    },

    async detachWorkspaceWorktree(itemId) {
        const sid = String(this.state.session.sessionId || '').trim();
        const target = String(itemId || '').trim();
        if (!sid || !target) return false;
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/workspace/worktrees/${encodeURIComponent(target)}`, {
                method: 'DELETE',
                headers: { 'X-Genome-Auth': token }
            });
            if (!res.ok) return false;
            const json = await res.json();
            if (json?.workspace && typeof json.workspace === 'object') {
                this.state.session.workspace = json.workspace;
            }
            return Boolean(json?.ok);
        } catch {
            return false;
        }
    },

    async attachWorkspaceWorktree(domain, name, branch = '') {
        const sid = String(this.state.session.sessionId || '').trim();
        const itemDomain = String(domain || '').trim().toLowerCase();
        const itemName = String(name || '').trim();
        const itemBranch = String(branch || this._contentBranchFor(itemDomain, itemName)).trim() || 'main';
        if (!sid || !itemDomain || !itemName) return null;
        try {
            const token = sessionStorage.getItem('genome_session') || '';
            const res = await fetch(`/api/session/${encodeURIComponent(sid)}/workspace/attach`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-Genome-Auth': token },
                body: JSON.stringify({ domain: itemDomain, name: itemName, branch: itemBranch })
            });
            if (!res.ok) return null;
            const json = await res.json();
            if (json?.workspace && typeof json.workspace === 'object') {
                this.state.session.workspace = json.workspace;
            }
            return json?.item || null;
        } catch {
            return null;
        }
    },

    async openRepoObject(domain, name, branch = '') {
        const itemDomain = String(domain || '').trim().toLowerCase();
        const itemName = String(name || '').trim();
        if (!itemDomain || !itemName) return false;

        if (branch) {
            this.state.session.workspace = this.state.session.workspace || { repoId: 'user-global', branch: 'main', worktrees: {}, activeContent: null };
            this.state.session.workspace.branch = String(branch).trim() || 'main';
            if (this.state.session.workspace.activeContent && typeof this.state.session.workspace.activeContent === 'object') {
                this.state.session.workspace.activeContent.branch = this.state.session.workspace.branch;
            }
        }

        const item = await this._contentOpenItem(itemDomain, itemName);
        if (!item) {
            this.showToast(`Could not open ${itemName}.`, 'warn', 2400);
            return false;
        }

        this.state.session.workspace = this.state.session.workspace || { repoId: 'user-global', branch: 'main', worktrees: {}, activeContent: null };
        const worktrees = (this.state.session.workspace.worktrees && typeof this.state.session.workspace.worktrees === 'object')
            ? this.state.session.workspace.worktrees
            : {};
        worktrees[String(item.itemId || itemName)] = {
            itemId: String(item.itemId || ''),
            name: String(item.name || itemName),
            domain: String(item.domain || itemDomain),
            branch: String(item.branch || 'main'),
            updatedAt: Number(item.updated_at || Date.now())
        };
        this.state.session.workspace.worktrees = worktrees;
        this.state.session.workspace.branch = String(item.branch || this.state.session.workspace.branch || 'main');
        this.state.session.workspace.activeContent = {
            itemId: String(item.itemId || ''),
            name: String(item.name || itemName),
            domain: String(item.domain || itemDomain),
            branch: String(item.branch || 'main'),
            hash: String(item.hash || ''),
            updatedAt: Number(item.updated_at || Date.now())
        };

        const intent = `open ${itemDomain} ${itemName}`;
        const envelope = IntentLayerCompiler.compile(intent, this.state.memory);
        const plan = UIPlanSchema.normalize({
            title: 'Open repository object',
            subtitle: `${itemDomain} · ${itemName}`,
            suggestions: [],
            trace: {}
        });
        const execution = {
            ok: true,
            toolResults: [{
                ok: true,
                op: `${itemDomain}_open`,
                message: `Opened ${itemName}`,
                data: {
                    op: `${itemDomain}_open`,
                    action: 'open',
                    name: itemName,
                    domain: itemDomain,
                    ...item,
                }
            }]
        };
        this.state.session.lastIntent = intent;
        this.state.session.lastExecution = execution;
        this.render(plan, envelope, this.state.session.lastKernelTrace);
        this.saveState();
        return true;
    },

    _makeAutoSave(domain, name, getDataFn, delayMs = 30000) {
        let timer = null;
        const schedule = () => {
            clearTimeout(timer);
            timer = setTimeout(() => this._contentSave(domain, name, getDataFn()), delayMs);
        };
        return { schedule, cleanup: () => clearTimeout(timer) };
    },

    _initDocumentSurface(bodyEl) {
        const domain = 'document';
        const name = bodyEl.dataset.funcName || '';
        this._renderSurfaceRepoMeta(domain, name);

        // Toolbar: execCommand bindings
        const toolbar = this.container.querySelector('[data-func-toolbar="document"]');
        if (toolbar) {
            toolbar.addEventListener('mousedown', (e) => {
                const btn = e.target.closest('[data-cmd]');
                if (!btn) return;
                e.preventDefault(); // keep editor focus
                const cmd = btn.dataset.cmd;
                if (cmd.startsWith('formatBlock:')) {
                    document.execCommand('formatBlock', false, cmd.split(':')[1]);
                } else {
                    document.execCommand(cmd, false, null);
                }
                bodyEl.focus();
            });
        }

        const getData = () => bodyEl.innerHTML;
        const saver = this._makeAutoSave(domain, name, getData);

        bodyEl.addEventListener('input', () => saver.schedule());
        bodyEl.addEventListener('blur', () => {
            saver.cleanup();
            this._contentSave(domain, name, getData());
        });

        this.state.activeSurface = { domain, name, getData, cleanup: saver.cleanup, getMeta: () => this._activeContentMeta(domain, name) };

        // Load saved content
        this._contentLoad(domain, name).then((saved) => {
            if (saved && typeof saved === 'string' && saved.trim()) {
                bodyEl.innerHTML = saved;
            } else {
                // Scaffold: agent topic/name as default heading
                const h = escapeHtml(name || 'Untitled');
                bodyEl.innerHTML = `<h1>${h}</h1><p></p>`;
            }
            bodyEl.focus();
            // Place cursor at end
            const range = document.createRange();
            range.selectNodeContents(bodyEl);
            range.collapse(false);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        });
    },

    _initSpreadsheetSurface(tableEl) {
        const domain = 'spreadsheet';
        const name = tableEl.dataset.funcName || '';
        this._renderSurfaceRepoMeta(domain, name);

        // Formula bar + cell ref sync
        const formulaBar = this.container.querySelector('[data-formula-bar]');
        const cellRef = this.container.querySelector('[data-cell-ref]');

        // cells: { "A1": { raw: "=SUM(A1:A3)", display: "6" } }
        const cells = {};

        const cellCoords = (cellId) => {
            const m = cellId.match(/^([A-Z]+)(\d+)$/);
            if (!m) return null;
            return { col: m[1], row: parseInt(m[2], 10) };
        };

        const colIndex = (col) => col.charCodeAt(0) - 65; // A=0

        const getCellValue = (cellId) => {
            const c = cells[cellId];
            if (!c) return '';
            return c.display !== undefined ? c.display : c.raw;
        };

        const evalFormula = (formula) => {
            try {
                const f = formula.slice(1).trim(); // strip '='
                // Range resolver: A1:A5 → array of values
                const resolved = f.replace(/([A-Z]+\d+):([A-Z]+\d+)/g, (_, from, to) => {
                    const fc = cellCoords(from), tc = cellCoords(to);
                    if (!fc || !tc) return '0';
                    const vals = [];
                    for (let r = fc.row; r <= tc.row; r++) {
                        for (let ci = colIndex(fc.col); ci <= colIndex(tc.col); ci++) {
                            const id = String.fromCharCode(65 + ci) + r;
                            vals.push(Number(getCellValue(id)) || 0);
                        }
                    }
                    return `[${vals.join(',')}]`;
                });
                // Function rewrites
                const expr = resolved
                    .replace(/\bSUM\(\[([^\]]*)\]\)/g, (_, v) => `(${v.split(',').map(Number).reduce((a, b) => a + b, 0)})`)
                    .replace(/\bAVERAGE\(\[([^\]]*)\]\)/g, (_, v) => {
                        const a = v.split(',').map(Number).filter((n) => Number.isFinite(n));
                        return a.length ? (a.reduce((x, y) => x + y, 0) / a.length) : 0;
                    })
                    .replace(/\bAVG\(\[([^\]]*)\]\)/g, (_, v) => { const a = v.split(',').map(Number); return a.reduce((x, y) => x + y, 0) / a.length; })
                    .replace(/\bIF\((.+),(.+),(.+)\)/g, (_, cond, t, f2) => `((${cond}) ? (${t}) : (${f2}))`)
                    // Single-cell references
                    .replace(/\b([A-Z]+\d+)\b/g, (id) => String(Number(getCellValue(id)) || 0));
                // eslint-disable-next-line no-new-func
                const result = Function(`"use strict"; return (${expr})`)();
                return typeof result === 'number' ? (Number.isFinite(result) ? String(Math.round(result * 1000) / 1000) : '#ERR') : String(result);
            } catch { return '#ERR'; }
        };

        const commitCell = (td) => {
            const id = td.dataset.cell;
            if (!id) return;
            const raw = td.textContent;
            const display = raw.startsWith('=') ? evalFormula(raw) : raw;
            cells[id] = { raw, display };
            td.textContent = display;
        };

        // Focus cell: show raw in formula bar
        tableEl.addEventListener('focusin', (e) => {
            const td = e.target.closest('td[data-cell]');
            if (!td) return;
            const id = td.dataset.cell;
            if (cellRef) cellRef.textContent = id;
            if (formulaBar) formulaBar.value = cells[id]?.raw ?? td.textContent;
            td.textContent = cells[id]?.raw ?? td.textContent;
        });

        tableEl.addEventListener('focusout', (e) => {
            const td = e.target.closest('td[data-cell]');
            if (td) { commitCell(td); saver.schedule(); }
        });

        tableEl.addEventListener('keydown', (e) => {
            const td = e.target.closest('td[data-cell]');
            if (!td) return;
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                commitCell(td);
                // Move to next row
                const coords = cellCoords(td.dataset.cell);
                if (coords) {
                    const next = tableEl.querySelector(`td[data-cell="${coords.col}${coords.row + 1}"]`);
                    if (next) next.focus();
                }
            }
            if (e.key === 'Tab') {
                e.preventDefault();
                commitCell(td);
                const coords = cellCoords(td.dataset.cell);
                if (coords) {
                    const nextCol = String.fromCharCode(coords.col.charCodeAt(0) + (e.shiftKey ? -1 : 1));
                    const next = tableEl.querySelector(`td[data-cell="${nextCol}${coords.row}"]`);
                    if (next) next.focus();
                }
            }
        });

        if (formulaBar) {
            formulaBar.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    const id = cellRef?.textContent;
                    const td = id ? tableEl.querySelector(`td[data-cell="${id}"]`) : null;
                    if (td) { td.textContent = formulaBar.value; commitCell(td); td.focus(); }
                }
            });
        }

        const getData = () => {
            const out = {};
            for (const [id, c] of Object.entries(cells)) out[id] = c.raw;
            return out;
        };
        const saver = this._makeAutoSave(domain, name, getData);

        this.state.activeSurface = { domain, name, getData, cleanup: saver.cleanup, getMeta: () => this._activeContentMeta(domain, name) };

        // Load saved content
        this._contentLoad(domain, name).then((saved) => {
            if (saved && typeof saved === 'object') {
                for (const [id, raw] of Object.entries(saved)) {
                    const td = tableEl.querySelector(`td[data-cell="${id}"]`);
                    if (td) { td.textContent = raw; commitCell(td); }
                }
            }
            // Focus A1
            const a1 = tableEl.querySelector('td[data-cell="A1"]');
            if (a1) a1.focus();
        });
    },

    _initCodeSurface(wrapEl) {
        const domain = 'code';
        const name = wrapEl.dataset.funcName || '';
        const textarea = wrapEl.querySelector('textarea');
        if (!textarea) return;

        // Tab key inserts spaces
        textarea.addEventListener('keydown', (e) => {
            if (e.key === 'Tab') {
                e.preventDefault();
                const start = textarea.selectionStart;
                const end = textarea.selectionEnd;
                textarea.value = textarea.value.slice(0, start) + '  ' + textarea.value.slice(end);
                textarea.selectionStart = textarea.selectionEnd = start + 2;
                saver.schedule();
            }
        });

        textarea.addEventListener('input', () => saver.schedule());
        textarea.addEventListener('blur', () => {
            saver.cleanup();
            this._contentSave(domain, name, textarea.value);
        });

        const getData = () => textarea.value;
        const saver = this._makeAutoSave(domain, name, getData);

        this.state.activeSurface = { domain, name, getData, cleanup: saver.cleanup };

        // Load saved content
        this._contentLoad(domain, name).then((saved) => {
            textarea.value = (typeof saved === 'string' && saved) ? saved : '';
            textarea.focus();
        });
    },

    _initTerminalSurface(inputEl) {
        const outputEl = this.container.querySelector('[data-terminal-output]');
        const history = [];
        let histIdx = -1;

        const appendLine = (text, cls = 't-out') => {
            if (!outputEl) return;
            const span = document.createElement('span');
            span.className = `t-line ${cls}`;
            span.textContent = text;
            outputEl.appendChild(span);
            outputEl.scrollTop = outputEl.scrollHeight;
        };

        appendLine('GenomeUI Terminal — type a command', 't-out');

        inputEl.addEventListener('keydown', async (e) => {
            if (e.key === 'Enter') {
                const cmd = inputEl.value.trim();
                if (!cmd) return;
                inputEl.value = '';
                history.unshift(cmd);
                histIdx = -1;

                appendLine(`$ ${cmd}`, 't-cmd');

                // Dispatch to backend terminal_exec op
                try {
                    const token = sessionStorage.getItem('genome_session') || '';
                    const res = await fetch('/api/turn', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-Genome-Auth': token },
                        body: JSON.stringify({
                            intent: cmd,
                            sessionId: this.state.session.sessionId,
                            baseRevision: this.state.session.revision,
                            deviceId: this.state.session.deviceId,
                            activeContent: { domain: 'terminal', op: 'terminal_run', command: cmd }
                        })
                    });
                    if (res.ok) {
                        const data = await res.json();
                        const output = data?.execution?.data?.output || data?.message || '(no output)';
                        const lines = String(output).split('\n');
                        for (const line of lines) appendLine(line, 't-out');
                    } else {
                        appendLine(`Error: ${res.status}`, 't-err');
                    }
                } catch (err) {
                    appendLine(`Error: ${err.message}`, 't-err');
                }
            }
            if (e.key === 'ArrowUp') {
                histIdx = Math.min(histIdx + 1, history.length - 1);
                if (history[histIdx] !== undefined) inputEl.value = history[histIdx];
                e.preventDefault();
            }
            if (e.key === 'ArrowDown') {
                histIdx = Math.max(histIdx - 1, -1);
                inputEl.value = histIdx >= 0 ? history[histIdx] : '';
                e.preventDefault();
            }
        });

        // Terminal doesn't use activeSurface content — no saves
        this.state.activeSurface = { domain: 'terminal', name: '', getData: () => null, cleanup: () => {} };
        inputEl.focus();
    },

    _initPresentationSurface(stageSlideEl) {
        const domain = 'presentation';
        const name = stageSlideEl.dataset.funcName || '';
        this._renderSurfaceRepoMeta(domain, name);
        const panel = this.container.querySelector('[data-pres-panel]');
        const toolbar = this.container.querySelector('[data-func-toolbar="presentation"]');

        // slides: array of HTML strings
        const slides = [stageSlideEl.innerHTML];
        let activeIdx = 0;

        const syncPanel = () => {
            if (!panel) return;
            panel.innerHTML = slides.map((_, i) => `
                <div class="func-pres-thumb-wrap">
                    <div class="func-pres-thumb${i === activeIdx ? ' active' : ''}" data-slide-index="${i}"></div>
                    <div class="func-pres-thumb-num">${i + 1}</div>
                </div>`).join('');
        };

        const switchSlide = (idx) => {
            // Save current
            slides[activeIdx] = stageSlideEl.innerHTML;
            activeIdx = idx;
            stageSlideEl.innerHTML = slides[activeIdx];
            stageSlideEl.dataset.slideIndex = String(activeIdx);
            syncPanel();
            stageSlideEl.focus();
        };

        panel?.addEventListener('click', (e) => {
            const thumb = e.target.closest('[data-slide-index]');
            if (thumb) switchSlide(Number(thumb.dataset.slideIndex));
        });

        toolbar?.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-slide-cmd]');
            if (!btn) return;
            const cmd = btn.dataset.slideCmd;
            if (cmd === 'add') {
                slides.splice(activeIdx + 1, 0, '<h1>New Slide</h1><p></p>');
                switchSlide(activeIdx + 1);
                saver.schedule();
            } else if (cmd === 'delete' && slides.length > 1) {
                slides.splice(activeIdx, 1);
                switchSlide(Math.min(activeIdx, slides.length - 1));
                saver.schedule();
            }
        });

        stageSlideEl.addEventListener('input', () => {
            slides[activeIdx] = stageSlideEl.innerHTML;
            saver.schedule();
        });

        stageSlideEl.addEventListener('blur', () => {
            slides[activeIdx] = stageSlideEl.innerHTML;
        });

        const getData = () => slides.slice();
        const saver = this._makeAutoSave(domain, name, getData);

        this.state.activeSurface = { domain, name, getData, cleanup: saver.cleanup, getMeta: () => this._activeContentMeta(domain, name) };

        // Load saved content
        this._contentLoad(domain, name).then((saved) => {
            if (Array.isArray(saved) && saved.length) {
                slides.length = 0;
                slides.push(...saved);
                stageSlideEl.innerHTML = slides[0];
                syncPanel();
            }
            stageSlideEl.focus();
        });

        syncPanel();
    },
};

const IntentLayerCompiler = {
    compile(rawText, memory) {
        const text = rawText.trim();
        const lower = text.toLowerCase();

        const surfaceIntent = {
            raw: text,
            normalized: lower,
            kind: inferSurfaceKind(lower),
            timestamp: Date.now()
        };

        const taskIntent = {
            goal: 'inspect',
            operation: 'read',
            targetDomains: [],
            constraints: inferConstraints(lower)
        };

        const stateIntent = {
            readDomains: [],
            writeOperations: [],
            summary: 'read all domains'
        };

        const uiIntent = {
            mode: inferUIMode(lower),
            density: inferDensity(lower),
            interactionPattern: 'prompt + suggestions',
            emphasis: 'balanced'
        };

        const explicit = parseExplicitCommands(text);
        if (explicit.length) {
            stateIntent.writeOperations = explicit;
            taskIntent.goal = 'mutate';
            taskIntent.operation = 'write';
            taskIntent.targetDomains = [...new Set(explicit.map((op) => op.domain))];
            stateIntent.readDomains = [...taskIntent.targetDomains];
            stateIntent.summary = summarizeWrites(explicit);

            if (taskIntent.targetDomains.includes('expenses')) uiIntent.emphasis = 'numbers';
            if (taskIntent.targetDomains.includes('tasks')) uiIntent.emphasis = 'execution';
            if (taskIntent.targetDomains.includes('notes')) uiIntent.emphasis = 'capture';

            return {
                surfaceIntent,
                taskIntent,
                stateIntent,
                uiIntent,
                confidence: 0.96
            };
        }

        const scores = {
            tasks: countSignals(lower, ['task', 'todo', 'ship', 'backlog', 'priority', 'deadline']),
            expenses: countSignals(lower, ['expense', 'money', 'budget', 'spend', 'cost']),
            notes: countSignals(lower, ['note', 'research', 'idea', 'nca', 'model'])
        };

        const ranked = Object.entries(scores).sort((a, b) => b[1] - a[1]);
        const topScore = ranked[0][1];

        stateIntent.readDomains = topScore === 0
            ? ['tasks', 'expenses', 'notes']
            : ranked.filter(([, value]) => value === topScore).map(([domain]) => domain);

        taskIntent.targetDomains = [...stateIntent.readDomains];
        taskIntent.goal = topScore === 0 ? 'overview' : 'inspect';
        taskIntent.operation = 'read';
        stateIntent.summary = `read ${stateIntent.readDomains.join('+')}`;

        if (stateIntent.readDomains.length === 1) {
            uiIntent.emphasis = stateIntent.readDomains[0] === 'expenses' ? 'numbers' : 'details';
        }

        return {
            surfaceIntent,
            taskIntent,
            stateIntent,
            uiIntent,
            confidence: topScore === 0 ? 0.58 : 0.78
        };
    }
};

const ToolRegistry = {
    tools: {
        add_task(memory, payload) {
            const title = (payload.title || '').trim();
            if (!title) return { ok: false, message: 'Task title is empty. Use: add task <title>' };
            memory.tasks.unshift({ id: safeRandomId(), title, done: false, createdAt: Date.now() });
            return { ok: true, message: `Added task: ${title}` };
        },

        toggle_task(memory, payload) {
            const task = findTask(memory.tasks, payload.selector);
            if (!task) return { ok: false, message: 'Task not found. Use: complete task 1' };
            task.done = !task.done;
            return { ok: true, message: task.done ? `Completed: ${task.title}` : `Reopened: ${task.title}` };
        },

        delete_task(memory, payload) {
            const task = findTask(memory.tasks, payload.selector);
            if (!task) return { ok: false, message: 'Task not found.' };
            memory.tasks = memory.tasks.filter((x) => x.id !== task.id);
            return { ok: true, message: `Deleted task: ${task.title}` };
        },

        clear_completed(memory) {
            const before = memory.tasks.length;
            memory.tasks = memory.tasks.filter((task) => !task.done);
            const removed = before - memory.tasks.length;
            return { ok: true, message: removed ? `Cleared ${removed} completed task(s)` : 'No completed tasks found' };
        },

        add_expense(memory, payload) {
            const amount = Number(payload.amount);
            if (Number.isNaN(amount) || amount <= 0) return { ok: false, message: 'Invalid expense amount.' };
            const category = (payload.category || 'general').toLowerCase();
            memory.expenses.unshift({
                id: safeRandomId(),
                amount,
                category,
                note: payload.note || '',
                createdAt: Date.now()
            });
            return { ok: true, message: `Added expense: ${formatCurrency(amount)} (${category})` };
        },

        add_note(memory, payload) {
            const text = (payload.text || '').trim();
            if (!text) return { ok: false, message: 'Note is empty.' };
            memory.notes.unshift({ id: safeRandomId(), text, createdAt: Date.now() });
            return { ok: true, message: 'Note captured.' };
        },

        reset_memory(memory) {
            memory.tasks = safeStructuredClone(DEFAULT_MEMORY.tasks);
            memory.expenses = safeStructuredClone(DEFAULT_MEMORY.expenses);
            memory.notes = safeStructuredClone(DEFAULT_MEMORY.notes);
            return { ok: true, message: 'Memory reset to defaults.' };
        }
    },

    execute(memory, operation) {
        const tool = this.tools[operation.type];
        if (!tool) return { ok: false, message: `Unknown operation: ${operation.type}` };
        return tool(memory, operation.payload || {});
    }
};

const ActionExecutor = {
    run(writeOperations, memory) {
        if (!Array.isArray(writeOperations) || writeOperations.length === 0) {
            return { ok: true, message: 'No state changes requested.', toolResults: [] };
        }

        const toolResults = [];
        let lastMessage = '';
        let allOk = true;

        for (const op of writeOperations) {
            const result = ToolRegistry.execute(memory, op);
            toolResults.push({ op: op.type, ok: result.ok, message: result.message });
            lastMessage = result.message;
            if (!result.ok) allOk = false;
        }

        return {
            ok: allOk,
            message: lastMessage,
            toolResults
        };
    }
};

const UIPlanner = {
    build(envelope, memory, execution) {
        const blocks = [];
        const domains = envelope.stateIntent.readDomains;
        const primary = domains[0] || 'tasks';

        blocks.push({
            id: 'system',
            type: 'narrative',
            label: 'Workspace',
            text: execution.message || `Intent: ${envelope.surfaceIntent.raw}`
        });

        if (primary === 'tasks') {
            blocks.push({
                id: 'tasks-list',
                type: 'list',
                label: 'Task Queue',
                span: 2,
                items: memory.tasks.slice(0, 10).map((task, index) => `${index + 1}. ${task.done ? '[done]' : '[open]'} ${task.title}`)
            });
        }

        if (primary === 'expenses') {
            blocks.push({
                id: 'expenses-table',
                type: 'table',
                label: 'Expense Stream',
                headers: ['Category', 'Amount', 'Note'],
                rows: memory.expenses.slice(0, 10).map((entry) => [
                    entry.category,
                    formatCurrency(entry.amount),
                    entry.note || '-'
                ])
            });
        }

        if (primary === 'notes') {
            blocks.push({
                id: 'notes-list',
                type: 'list',
                label: 'Notes',
                span: 2,
                items: memory.notes.slice(0, 10).map((note) => `${formatDate(note.createdAt)} - ${note.text}`)
            });
        }

        blocks.push({
            id: 'operation',
            type: 'metric',
            label: 'Operation',
            value: envelope.taskIntent.operation.toUpperCase(),
            meta: envelope.stateIntent.summary
        });

        blocks.push({
            id: 'objects',
            type: 'metric',
            label: 'Objects',
            value: String(memory.tasks.length + memory.expenses.length + memory.notes.length)
        });

        blocks.push({
            id: 'tasks-open',
            type: 'metric',
            label: 'Open Tasks',
            value: String(memory.tasks.filter((task) => !task.done).length)
        });

        blocks.push({
            id: 'spend',
            type: 'metric',
            label: 'Spend',
            value: formatCurrency(memory.expenses.reduce((sum, item) => sum + item.amount, 0)),
            color: 'var(--danger)'
        });

        const suggestions = buildSuggestions(envelope, memory);

        return {
            version: '1.0.0',
            title: `Generated Surface: ${capitalize(primary)}`,
            subtitle: execution.ok ? `Intent: "${envelope.surfaceIntent.raw}"` : execution.message,
            layout: {
                columns: 2,
                density: envelope.uiIntent.density
            },
            suggestions,
            blocks,
            trace: {
                planVersion: 'intent-layer-v1',
                focusDomains: domains,
                mode: envelope.uiIntent.mode
            }
        };
    }
};

const UIPlanSchema = {
    normalize(plan) {
        const fallback = {
            version: '1.0.0',
            title: 'Generated Surface',
            subtitle: 'Unable to parse generated plan.',
            layout: { columns: 2, density: 'normal' },
            suggestions: ['show tasks and expenses'],
            blocks: [{ id: 'fallback', type: 'narrative', label: 'Fallback', text: 'Plan validation fallback.' }],
            trace: { planVersion: 'fallback', focusDomains: ['tasks', 'expenses', 'notes'], mode: 'default' }
        };

        if (!plan || typeof plan !== 'object') return fallback;

        const trace = plan?.trace && typeof plan.trace === 'object' ? { ...plan.trace } : {};
        const safe = {
            version: typeof plan.version === 'string' ? plan.version : fallback.version,
            title: typeof plan.title === 'string' ? plan.title : fallback.title,
            subtitle: typeof plan.subtitle === 'string' ? plan.subtitle : fallback.subtitle,
            layout: {
                columns: Number.isInteger(plan?.layout?.columns) ? plan.layout.columns : 2,
                density: typeof plan?.layout?.density === 'string' ? plan.layout.density : 'normal'
            },
            suggestions: Array.isArray(plan.suggestions)
                ? plan.suggestions.filter((s) => typeof s === 'string').slice(0, 6)
                : fallback.suggestions,
            blocks: Array.isArray(plan.blocks) ? plan.blocks.filter((b) => isValidBlock(b)).slice(0, 12) : fallback.blocks,
            trace: {
                ...trace,
                planVersion: typeof trace.planVersion === 'string' ? trace.planVersion : 'unknown',
                focusDomains: Array.isArray(trace.focusDomains) ? trace.focusDomains : [],
                mode: typeof trace.mode === 'string' ? trace.mode : 'default'
            }
        };

        if (!safe.blocks.length) safe.blocks = fallback.blocks;
        if (!safe.suggestions.length) safe.suggestions = fallback.suggestions;

        return safe;
    }
};

function isValidBlock(block) {
    if (!block || typeof block !== 'object' || typeof block.type !== 'string') return false;
    const allowed = ['metric', 'list', 'table', 'narrative'];
    return allowed.includes(block.type);
}

function parseExplicitCommands(text) {
    const commands = [];

    const addTask = text.match(/^add task\s+(.+)$/i);
    if (addTask) {
        commands.push({ type: 'add_task', domain: 'tasks', payload: { title: addTask[1] } });
        return commands;
    }

    const completeTask = text.match(/^(complete|done|finish)\s+task\s+(\S+)$/i);
    if (completeTask) {
        commands.push({ type: 'toggle_task', domain: 'tasks', payload: { selector: completeTask[2] } });
        return commands;
    }

    const deleteTask = text.match(/^(delete|remove)\s+task\s+(\S+)$/i);
    if (deleteTask) {
        commands.push({ type: 'delete_task', domain: 'tasks', payload: { selector: deleteTask[2] } });
        return commands;
    }

    const clearDone = text.match(/^(clear completed|clear done)$/i);
    if (clearDone) {
        commands.push({ type: 'clear_completed', domain: 'tasks', payload: {} });
        return commands;
    }

    const addExpense = text.match(/^add expense\s+\$?([0-9]+(?:\.[0-9]{1,2})?)\s+([a-zA-Z_-]+)\s*(.*)$/i);
    if (addExpense) {
        commands.push({
            type: 'add_expense',
            domain: 'expenses',
            payload: {
                amount: Number(addExpense[1]),
                category: addExpense[2],
                note: addExpense[3]
            }
        });
        return commands;
    }

    const addNote = text.match(/^(add note|note)\s+(.+)$/i);
    if (addNote) {
        commands.push({ type: 'add_note', domain: 'notes', payload: { text: addNote[2] } });
        return commands;
    }

    const reset = text.match(/^(reset demo|reset memory)$/i);
    if (reset) {
        commands.push({ type: 'reset_memory', domain: 'system', payload: {} });
        return commands;
    }

    return commands;
}

function summarizeWrites(writeOperations) {
    if (!writeOperations.length) return 'read only';
    return writeOperations.map((op) => op.type).join(' + ');
}

function inferSurfaceKind(lowerText) {
    if (lowerText.includes('?')) return 'question';
    if (lowerText.startsWith('add ') || lowerText.startsWith('delete ') || lowerText.startsWith('complete ')) return 'command';
    return 'statement';
}

function inferUIMode(lowerText) {
    if (lowerText.includes('compare') || lowerText.includes('analyze')) return 'analytical';
    if (lowerText.includes('quick') || lowerText.includes('fast')) return 'compact';
    return 'default';
}

function inferDensity(lowerText) {
    if (lowerText.includes('detailed') || lowerText.includes('deep')) return 'dense';
    if (lowerText.includes('quick') || lowerText.includes('summary')) return 'compact';
    return 'normal';
}

function inferConstraints(lowerText) {
    const constraints = [];
    if (lowerText.includes('today')) constraints.push('temporal:today');
    if (lowerText.includes('top')) constraints.push('ranked');
    return constraints;
}

function buildSuggestions(envelope, memory) {
    const suggestions = [];
    const domains = envelope.stateIntent.readDomains;

    if (domains.includes('tasks')) {
        suggestions.push('add task Draft onboarding checklist');
        if (memory.tasks.length) suggestions.push('complete task 1');
        suggestions.push('clear completed');
    }

    if (domains.includes('expenses')) {
        suggestions.push('add expense 16.4 transport train');
    }

    if (domains.includes('notes')) {
        suggestions.push('add note Users ask for direct outputs');
    }

    if (!suggestions.length) {
        suggestions.push('show tasks and expenses');
        suggestions.push('add task Ship first usable flow');
        suggestions.push('add expense 20 tools');
    }

    return [...new Set(suggestions)].slice(0, 6);
}

function findTask(tasks, selector) {
    if (!selector) return null;
    if (/^\d+$/.test(selector)) return tasks[Number(selector) - 1] || null;
    return tasks.find((task) => task.id === selector || task.id.startsWith(selector)) || null;
}

function countSignals(text, words) {
    return words.reduce((acc, word) => acc + (text.includes(word) ? 1 : 0), 0);
}

function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
}

function formatCurrency(value) {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value || 0);
}

function formatDate(timestamp) {
    return new Date(timestamp).toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    });
}

function capitalize(value) {
    if (!value) return '';
    return value.charAt(0).toUpperCase() + value.slice(1);
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function escapeAttr(value) {
    return escapeHtml(value);
}

// Sanitize a CSS color value before interpolating into a style attribute.
// Only allows CSS variables, hex colors, rgb/rgba, and safe keywords.
// Returns 'inherit' for anything that doesn't match — never raw user strings.
// Validate URLs before use in href/src attributes, then HTML-escape the result.
// escapeAttr() only escapes HTML entities — it cannot prevent javascript: URLs.
// This function whitelists http/https/mailto/tel and returns '#' for everything
// else so no code is executed on click.  It also HTML-escapes so it can be used
// directly in template literals without a second escapeAttr() call.
function safeUrl(value) {
    const v = String(value || '').trim();
    if (!v) return '#';
    let safe;
    try {
        const u = new URL(v, location.href);
        safe = (u.protocol === 'https:' || u.protocol === 'http:' ||
                u.protocol === 'mailto:' || u.protocol === 'tel:') ? v : '#';
    } catch (_) {
        // relative path (no scheme) — allow; anything with an unknown scheme — block
        safe = /^[a-z][a-z0-9+.-]*:/i.test(v) ? '#' : v;
    }
    return escapeAttr(safe);
}

function safeCssColor(value) {
    const v = String(value || '').trim();
    if (!v || v === 'inherit' || v === 'transparent' || v === 'currentColor') return v || 'inherit';
    if (/^var\(--[a-z0-9-]+\)$/.test(v)) return v;
    if (/^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$/.test(v)) return v;
    if (/^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}(?:\s*,\s*[\d.]+)?\s*\)$/.test(v)) return v;
    return 'inherit';
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

if (typeof window !== 'undefined') {
    window.__GENOME_UI_ENGINE__ = UIEngine;
}

UIEngine.init();
