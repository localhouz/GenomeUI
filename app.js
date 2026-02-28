const STORAGE_KEY = 'genui_memory_v4';
const SESSION_STORAGE_KEY = 'genui_session_v1';
const DEVICE_STORAGE_KEY = 'genui_device_v1';
const HISTORY_LIMIT = 40;
const FALLBACK_POLL_MS = 2500;
const PRESENCE_HEARTBEAT_MS = 30000;
const WS_RECONNECT_MIN_MS = 1200;
const WS_RECONNECT_MAX_MS = 20000;
const WS_RECONNECT_FACTOR = 1.8;

if (typeof window !== 'undefined' && 'serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => { });
    });
}

const DEFAULT_MEMORY = {
    tasks: [
        { id: crypto.randomUUID(), title: 'Replace mode-based UI with layered synthesis', done: false, createdAt: Date.now() - 7_200_000 },
        { id: crypto.randomUUID(), title: 'Add schema validator for UI plans', done: false, createdAt: Date.now() - 5_400_000 }
    ],
    expenses: [
        { id: crypto.randomUUID(), amount: 28.5, category: 'food', note: 'lunch', createdAt: Date.now() - 86_400_000 },
        { id: crypto.randomUUID(), amount: 96, category: 'cloud', note: 'gpu runtime', createdAt: Date.now() - 43_200_000 }
    ],
    notes: [
        { id: crypto.randomUUID(), text: 'Generative UI should synthesize from intent layers every turn.', createdAt: Date.now() - 3_000_000 }
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

    async process(intent, sessionId, baseRevision, deviceId, onConflict = 'rebase_if_commutative', idempotencyKey = null) {
        const authToken = sessionStorage.getItem('genome_session') || '';
        const response = await fetch(`/api/turn`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Genome-Auth': authToken,
            },
            body: JSON.stringify({ intent, sessionId, baseRevision, deviceId, onConflict, idempotencyKey })
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

const UIEngine = {
    container: document.getElementById('ui-container'),
    input: document.getElementById('intent-input'),
    status: document.getElementById('status'),
    historyReel: document.getElementById('history-reel'),
    inputContainer: document.querySelector('.input-container'),

    state: {
        memory: structuredClone(DEFAULT_MEMORY),
        session: {
            lastIntent: '',
            lastEnvelope: null,
            lastExecution: null,
            lastPlan: null,
            lastKernelTrace: null,
            handoff: { activeDeviceId: null, pending: null, lastClaimAt: null },
            presence: { activeCount: 0, count: 0, items: [], timeoutMs: 120000, updatedAt: 0 },
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
        runtimeEvents: []
    },

    async init() {
        this.loadState();
        this.primeRelativeLocationContext().catch(() => { });
        this.setupElectronChrome();
        this.setupUXChrome();
        this.setConnectivity(typeof navigator?.onLine === 'boolean' ? navigator.onLine : true);
        this.bindEvents();
        this.updateShortcutHint();
        await this.ensureAuth();
        await this.runBootSequence();
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

        this.setBootState('Compiling startup intent layers', 52);
        const hasHistory = Array.isArray(this.state.history) && this.state.history.length > 0;
        const hasIntent = Boolean(String(this.state.session.lastIntent || '').trim());
        if (!hasHistory && !hasIntent) {
            this.renderWelcome();
        } else {
            this.refreshSurface();
        }
        await sleep(110);

        this.setBootState('Synchronizing surface runtime', 78);
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

    renderWelcome() {
        const welcomeIntents = [
            'show weather in my city',
            'show me running shoes',
            'show my tasks',
            'search web local-first app design'
        ];
        this.container.innerHTML = `
            <div class="welcome-surface">
                <div class="welcome-title">Genome Surface OS</div>
                <div class="welcome-sub">Intent-driven. No apps. Just surface.</div>
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

    async startHandoff() {
        const sid = this.state.session.sessionId;
        if (!sid || !this.state.session.deviceId) return;
        const out = await RemoteTurnService.handoffStart(sid, this.state.session.deviceId);
        this.state.session.revision = Number(out.revision || this.state.session.revision);
        this.state.session.handoff = out.handoff || {
            activeDeviceId: this.state.session.deviceId,
            pending: { token: out.token, fromDeviceId: this.state.session.deviceId, expiresAt: out.expiresAt },
            lastClaimAt: this.state.session.handoff?.lastClaimAt || null
        };
        const claimUrl = new URL(window.location.href);
        claimUrl.searchParams.set('session', sid);
        claimUrl.searchParams.set('handoff', out.token);
        const link = String(claimUrl);
        if (navigator?.clipboard?.writeText) {
            try { await navigator.clipboard.writeText(link); } catch { }
        }
        this.showToast('Handoff token issued. Claim link copied.', 'ok', 3200);
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
        if (/^start handoff$/i.test(value)) {
            return { mode: 'start' };
        }
        const claim = value.match(/^claim handoff\s+([a-z0-9-]+)$/i);
        if (claim) {
            return { mode: 'claim', token: claim[1] };
        }
        return null;
    },

    resolveSessionIdFromUrl() {
        const params = new URLSearchParams(window.location.search);
        const value = String(params.get('session') || '').trim().toLowerCase();
        return value ? value.replace(/[^a-z0-9_-]/g, '').slice(0, 32) : '';
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

        const authToken = sessionStorage.getItem('genome_session') || '';
        const wsUrl = `ws://${location.host}/ws?sessionId=${encodeURIComponent(sessionId)}&authToken=${encodeURIComponent(authToken)}`;
        const ws = new WebSocket(wsUrl);
        this._ws = ws;

        ws.onopen = () => {
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
        if (snapshot.lastTurn?.envelope && snapshot.lastTurn?.plan) {
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
        const prior = Array.isArray(this.state.runtimeEvents) ? this.state.runtimeEvents : [];
        const seen = new Set(prior.map((item) => `${item.id}:${item.ts}`));
        const next = [...prior];
        for (const item of incoming) {
            if (!item || typeof item !== 'object') continue;
            const jobId = String(item.jobId || '');
            const createdAt = Number(item.createdAt || Date.now());
            const key = `${jobId}:${createdAt}`;
            if (seen.has(key)) continue;
            seen.add(key);
            const title = String(item.title || 'Background event').trim();
            const message = String(item.message || '').trim();
            next.push({
                id: jobId || key,
                type: String(item.type || 'event'),
                ts: createdAt,
                title,
                message
            });
            const eventType = String(item.type || 'event');
            if (eventType === 'continuity_alert') {
                this.showToast(`${message || title}`, 'warn', 5000);
                if (this.status) {
                    this.status.dataset.alert = 'continuity';
                }
            } else {
                this.showToast(`${title}: ${message || 'updated'}`, 'info', 2600);
            }
        }
        this.state.runtimeEvents = next.slice(-8);
    },

    applyRemoteSync(payload) {
        if (!payload || this.state.session.isApplyingLocalTurn) return;
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
        this.applyBackgroundEvents(payload.backgroundEvents);
        if (payload.lastTurn?.envelope && payload.lastTurn?.plan) {
            const plan = UIPlanSchema.normalize(payload.lastTurn.plan);
            this.state.session.lastExecution = payload.lastTurn.execution || this.state.session.lastExecution;
            this.state.session.lastKernelTrace = payload.lastTurn.kernelTrace || this.deriveKernelTrace(payload.lastTurn.execution, payload.lastTurn.route);
            this.render(plan, payload.lastTurn.envelope, this.state.session.lastKernelTrace);
            this.updateStatus(`SYNCED:${payload.lastTurn.planner || 'REMOTE'}`);
        } else if (priorHandoff !== JSON.stringify(this.state.session.handoff || {})) {
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

        this.container.addEventListener('click', (event) => {
            const webdeckModeBtn = event.target.closest('[data-webdeck-mode-toggle]');
            if (webdeckModeBtn) {
                this.toggleWebdeckMode();
                return;
            }

            const sceneButton = event.target.closest('[data-scene-domain]');
            if (sceneButton) {
                const domain = String(sceneButton.dataset.sceneDomain || '').trim().toLowerCase();
                if (domain) this.switchToSceneDomain(domain);
                return;
            }

            const suggestion = event.target.closest('[data-command]');
            if (!suggestion) return;
            const command = suggestion.dataset.command;
            if (!command) return;
            this.input.value = command;
            this.input.focus();
            this.handleIntent(command);
        });

        this.historyReel.addEventListener('click', (event) => {
            const node = event.target.closest('[data-history-index]');
            if (!node) return;
            this.restoreFromHistory(Number(node.dataset.historyIndex));
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

    showToast(message, type = 'info', duration = 1800) {
        if (!this.toast) return;
        this.toast.className = `visible tone-${type}`;
        this.toast.textContent = String(message || 'Updated.');
        if (this._toastTimer) clearTimeout(this._toastTimer);
        this._toastTimer = setTimeout(() => {
            this.toast.className = '';
            this.toast.textContent = '';
        }, duration);
    },

    async handleIntent(text) {
        if (!text) return;
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

        const startTime = performance.now();
        this.state.session.lastIntent = text;
        this.status.innerText = 'INTERPRETING INTENT...';
        this.inputContainer.classList.add('active-intent');
        this.container.classList.add('refracting');

        await this.showReasoning([
            'Parsing layered intent envelope...',
            'Executing state/tool operations...',
            'Generating schema-validated UI plan...'
        ]);

        let envelope;
        let execution;
        let kernelTrace;
        let safePlan;
        let mergeInfo = null;
        let plannerSource = 'local';

        try {
            this.state.session.isApplyingLocalTurn = true;
            const remote = await RemoteTurnService.process(
                text,
                this.state.session.sessionId,
                this.state.session.revision,
                this.state.session.deviceId,
                'rebase_if_commutative',
                `${this.state.session.deviceId}:${Date.now().toString(36)}:${Math.random().toString(36).slice(2, 8)}`
            );
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
                this.container.classList.remove('refracting');
                this.inputContainer.classList.remove('active-intent');
                return;
            }
            this.handleTransportFailure('turn', error);
            envelope = IntentLayerCompiler.compile(text, this.state.memory);
            execution = ActionExecutor.run(envelope.stateIntent.writeOperations, this.state.memory);
            kernelTrace = this.deriveKernelTrace(execution, { target: 'local-fallback', reason: 'backend unavailable', model: null });
            const uiPlan = UIPlanner.build(envelope, this.state.memory, execution);
            safePlan = UIPlanSchema.normalize(uiPlan);
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
        if (mergeInfo?.rebased) {
            this.showToast('Merged with newer session revision.', 'info', 2200);
        }
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
        const showCoreCopy = !['shopping', 'webdeck', 'social', 'banking', 'contacts', 'telephony', 'tasks', 'files', 'expenses', 'notes', 'sports', 'sports_manage', 'weather', 'weather_7day', 'weather_tomorrow'].includes(core.kind);
        const hud = this.buildImmersiveHud(core, plan, envelope, kernelTrace, this.state.session.lastExecution);
        const railBlocks = this.buildImmersiveRailBlocks(blocks);
        this.container.innerHTML = `
            <div class="workspace immersive">
                <section class="workspace-main">
                    <div class="surface-core immersive ${escapeAttr(core.kind || 'generic')} ${escapeAttr(core.theme || '')}">
                        ${sceneDock}
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
            { id: 'files', label: 'files' },
            { id: 'weather', label: 'weather' },
            { id: 'location', label: 'location' },
            { id: 'shopping', label: 'shopping' },
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
            files: 'show files',
            weather: weatherHint ? `show weather in ${weatherHint}` : "what's the weather where i am",
            location: 'where am i',
            shopping: 'show me running shoes',
            webdeck: 'open example.com',
            social: 'show my social feed',
            banking: 'show account balances',
            contacts: 'show contacts',
            telephony: 'show call status',
            generic: 'show me what i can do'
        };
        return byDomain[String(domain || 'generic')] || byDomain.generic;
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
        if (!latest) {
            if (domain === 'tasks') return { headline: 'Task Flow', summary: 'Generated task workspace', variant: 'result', kind: 'tasks', theme: 'theme-tasks' };
            if (domain === 'expenses') return { headline: 'Spend Pulse', summary: 'Generated expense workspace', variant: 'result', kind: 'expenses', theme: 'theme-expenses' };
            if (domain === 'notes') return { headline: 'Knowledge Stream', summary: 'Generated notes workspace', variant: 'result', kind: 'notes', theme: 'theme-notes' };
            if (domain === 'graph') return { headline: 'System Graph', summary: 'Generated relation workspace', variant: 'result', kind: 'graph', theme: 'theme-graph' };
            if (domain === 'files') return { headline: 'File Surface', summary: 'Generated file workspace', variant: 'result', kind: 'files', theme: 'theme-files' };
            return { headline: fallbackHeadline, summary: fallbackSummary, variant: 'intent', kind: 'generic', theme: 'theme-neutral' };
        }
        if (!latest.ok) {
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
        if (domain === 'files') {
            return { headline: 'File Surface', summary: 'Generated file workspace', variant: 'result', kind: 'files', theme: 'theme-files' };
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
        // ── Computer layer ops ─────────────────────────────────────────────────
        {
            const d = (latest.data && typeof latest.data === 'object') ? latest.data : {};
            const action = String(d.action || '').trim();
            const name   = String(d.name   || '').trim();
            const topic  = String(d.topic  || '').trim();
            if (['document_create', 'document_edit'].includes(latest.op)) {
                return { headline: name || topic || (action === 'edit' ? 'Edit document' : 'New document'), summary: action || 'document', variant: 'result', kind: 'document', theme: 'theme-document', info: d };
            }
            if (['spreadsheet_create', 'spreadsheet_edit'].includes(latest.op)) {
                return { headline: name || (action === 'edit' ? 'Edit spreadsheet' : 'New spreadsheet'), summary: action || 'spreadsheet', variant: 'result', kind: 'spreadsheet', theme: 'theme-spreadsheet', info: d };
            }
            if (['presentation_create', 'presentation_edit'].includes(latest.op)) {
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
            if (['content_find', 'content_list', 'content_history', 'content_branch', 'content_revert', 'content_share'].includes(latest.op)) {
                const type = String(d.type || '').trim();
                return { headline: name || action || 'content', summary: [type, action].filter(Boolean).join(' · ') || 'content', variant: 'result', kind: 'content', theme: 'theme-content', info: d };
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
        return { headline: summary, summary: fallbackSummary, variant: 'result', kind: 'generic', theme: 'theme-neutral' };
    },

    buildPrimaryVisual(core, execution, envelope, plan) {
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
                        <a class="shop-stage-tile" href="${escapeAttr(itemUrl)}" target="_blank" rel="noopener noreferrer">
                            ${imgSrc ? `<img src="${escapeAttr(imgSrc)}" alt="${escapeAttr(ttl)}" loading="lazy" />` : '<div class="shop-stage-tile-img-fallback"></div>'}
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
                            <a class="scene-chip scene-chip-link shop-brand-cta" href="${escapeAttr(brandLink)}" target="_blank" rel="noopener noreferrer">
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
                                ${liveFrameUrl ? `<iframe class="shop-stage-live-frame" src="${escapeAttr(liveFrameUrl)}" title="${escapeAttr(`${brandName || 'brand'} live source`)}" loading="eager" referrerpolicy="no-referrer" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation"></iframe>` : ''}
                                ${heroImage ? `<img class="shop-stage-hero-fallback-image" src="${escapeAttr(heroImage)}" alt="${escapeAttr(heroTitle)}" loading="lazy" />` : '<div class="shop-stage-hero-fallback"></div>'}
                                <div class="shop-stage-hero-tint"></div>
                                <div class="shop-stage-hero-meta">
                                    <div class="shop-stage-hero-title">${escapeHtml(heroTitle)}</div>
                                    ${heroPrice ? `<div class="shop-stage-hero-price">${escapeHtml(heroPrice)}</div>` : ''}
                                    <a class="shop-stage-open-live" href="${escapeAttr(heroUrl)}" target="_blank" rel="noopener noreferrer">open product</a>
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
                    <a class="shop-card${sizeMod}" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">
                        <div class="shop-image-wrap">
                            ${imageUrl ? `<img class="shop-image" src="${escapeAttr(imageUrl)}" alt="${escapeAttr(title)}" loading="${idx < 3 ? 'eager' : 'lazy'}" />` : '<div class="shop-image-placeholder"></div>'}
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
                        <a class="scene-chip scene-chip-link" href="${escapeAttr(brandLink)}" target="_blank" rel="noopener noreferrer">
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
            const items = Array.isArray(info.items) ? info.items.slice(0, 10) : [];
            const available = Number(info.available || 0);
            const ledger = Number(info.ledger || 0);
            const source = String(info.source || 'scaffold').trim();
            const currency = String(info.currency || 'USD').trim();
            const asOf = String(info.asOf || '').trim();
            const txRows = items.map((item) => {
                const date = String(item?.date || '-').trim();
                const merchant = String(item?.merchant || item?.name || '-').trim();
                const amount = Number(item?.amount || 0);
                const direction = amount < 0 ? 'debit' : 'credit';
                return `<div class="bank-row ${direction}">
                    <div class="bank-row-merchant">${escapeHtml(merchant)}</div>
                    <div class="bank-row-date">${escapeHtml(date)}</div>
                    <div class="bank-row-amount">${escapeHtml(formatCurrency(amount))}</div>
                </div>`;
            }).join('');
            return `
                <div class="scene scene-banking interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                    <div class="scene-orb orb-b"></div>
                    <div class="scene-orb orb-c"></div>
                    <div class="scene-grid"></div>
                    <div class="bank-head">
                        <div class="bank-balance">${escapeHtml(formatCurrency(available))}</div>
                        <div class="bank-meta">${escapeHtml(currency)} | ledger ${escapeHtml(formatCurrency(ledger))}${asOf ? ` | ${escapeHtml(asOf)}` : ''}</div>
                    </div>
                    <div class="bank-source">source ${escapeHtml(source)}</div>
                    <div class="bank-transactions">
                        ${txRows || '<div class="bank-row"><div class="bank-row-merchant">No transactions loaded</div><div class="bank-row-date">-</div><div class="bank-row-amount">$0.00</div></div>'}
                    </div>
                </div>
            `;
        }
        if (core.kind === 'contacts') {
            const info = core.info || {};
            const items = Array.isArray(info.items) ? info.items.slice(0, 16) : [];
            const source = String(info.source || 'scaffold').trim();
            const cards = items.map((item) => {
                const name = String(item?.name || 'Unknown').trim();
                const phone = String(item?.phone || '').trim();
                const label = String(item?.label || '').trim();
                return `<article class="contacts-card">
                    <div class="contacts-name">${escapeHtml(name)}</div>
                    <div class="contacts-meta">${escapeHtml([label, phone].filter(Boolean).join(' | '))}</div>
                    ${phone ? `<button class="contacts-call-btn" data-command="${escapeAttr(`confirm call ${phone}`)}" type="button">prepare call</button>` : ''}
                </article>`;
            }).join('');
            return `
                <div class="scene scene-contacts interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                    <div class="scene-orb orb-a"></div>
                    <div class="scene-grid"></div>
                    <div class="contacts-head">
                        <div class="contacts-title">contacts stream</div>
                        <div class="contacts-source">source ${escapeHtml(source)}</div>
                    </div>
                    <div class="contacts-grid">
                        ${cards || '<article class="contacts-card"><div class="contacts-name">No contacts</div><div class="contacts-meta">Try: show contacts</div></article>'}
                    </div>
                </div>
            `;
        }
        if (core.kind === 'telephony') {
            const info = core.info || {};
            const target = String(info.target || '').trim();
            const mode = String(info.mode || 'bridge_prepare').trim();
            const contactName = String(info.contactName || '').trim();
            const lines = Array.isArray(info.steps) ? info.steps : [];
            const safeLines = lines.length ? lines.slice(0, 6) : [
                'grant connector scope telephony.call.start',
                'confirm call <target>',
                'open mobile and claim handoff token',
            ];
            const items = safeLines.map((line) => `<li>${escapeHtml(String(line))}</li>`).join('');
            return `
                <div class="scene scene-telephony interactive">
                    <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                    <div class="scene-orb orb-b"></div>
                    <div class="scene-grid"></div>
                    <div class="telephony-head">
                        <div class="telephony-title">${escapeHtml(target || 'telephony control')}</div>
                        <div class="telephony-meta">${escapeHtml([mode, contactName].filter(Boolean).join(' | ') || 'handoff mode')}</div>
                    </div>
                    <div class="telephony-panel">
                        <div class="telephony-label">next actions</div>
                        <ol class="telephony-steps">${items}</ol>
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
                            ${weatherTargetUrl ? `<a class="wh-link" href="${escapeAttr(weatherTargetUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(weatherTargetLabel)}</a>` : ''}
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
                            ${weatherTargetUrl ? `<a class="wh-link" href="${escapeAttr(weatherTargetUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(weatherTargetLabel)}</a>` : ''}
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
                        ${weatherTargetUrl ? `<a class="wh-link" href="${escapeAttr(weatherTargetUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(weatherTargetLabel)}</a>` : ''}
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
                        ? `<img class="sp-bs-logo" src="${escapeAttr(logo)}" alt="${abbr}" onerror="this.style.display='none'">`
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
                        ? `<img class="sp-duel-logo" src="${escapeAttr(logoUrl)}" alt="${abbr}" onerror="this.style.display='none'">`
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
                        ${logoUrl ? `<img class="sp-compact-logo" src="${escapeAttr(logoUrl)}" alt="${abbr}" onerror="this.style.display='none'">` : ''}
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
            const tasks = (this.state.memory?.tasks || []).slice(0, 6);
            const openCount = tasks.filter((t) => !t.done).length;
            const doneCount = tasks.filter((t) => t.done).length;
            const progress = tasks.length ? Math.round((doneCount / Math.max(1, tasks.length)) * 100) : 0;
            const topOpen = tasks.filter((t) => !t.done).slice(0, 5);
            const topDone = tasks.filter((t) => t.done).slice(0, 3);
            const relationItems = (() => {
                const snapshot = this.state.session.graphSnapshot || {};
                const relations = Array.isArray(snapshot.relations) ? snapshot.relations : [];
                return relations
                    .filter((r) => String(r?.relation || r?.kind || '').toLowerCase() === 'depends_on')
                    .slice(-5)
                    .map((r) => `${String(r?.sourceId || '').slice(0, 8)} -> ${String(r?.targetId || '').slice(0, 8)}`);
            })();
            const openHtml = (topOpen.length ? topOpen : [{ title: 'No open tasks' }]).map((item, idx) => `
                <div class="tasks-row">
                    <span class="tasks-row-index">${escapeHtml(String(idx + 1))}</span>
                    <span class="tasks-row-title">${escapeHtml(String(item.title || 'Task'))}</span>
                    ${topOpen.length ? `<button class="tasks-row-action" type="button" data-command="${escapeAttr(`complete task ${idx + 1}`)}">complete</button>` : ''}
                </div>
            `).join('');
            const doneHtml = (topDone.length ? topDone : [{ title: 'No completed tasks' }]).map((item) => `
                <div class="tasks-done-pill">${escapeHtml(String(item.title || 'Task'))}</div>
            `).join('');
            const relHtml = (relationItems.length ? relationItems : ['No dependencies yet']).map((line) => `
                <div class="tasks-rel-row">${escapeHtml(line)}</div>
            `).join('');
            return `<div class="scene scene-domain scene-tasks interactive">
                <canvas class="scene-canvas domain-canvas" data-scene="tasks" data-open="${escapeAttr(String(openCount))}" data-done="${escapeAttr(String(doneCount))}"></canvas>
                <div class="tasks-shell">
                    <div class="tasks-kpi">
                        <div class="tasks-kpi-label">completion</div>
                        <div class="tasks-kpi-value">${escapeHtml(String(progress))}%</div>
                        <div class="tasks-kpi-sub">${escapeHtml(String(doneCount))} done / ${escapeHtml(String(openCount))} open</div>
                    </div>
                    <div class="tasks-open-list">${openHtml}</div>
                    <div class="tasks-done-list">${doneHtml}</div>
                    <div class="tasks-rel-list">${relHtml}</div>
                </div>
            </div>`;
        }
        if (core.kind === 'files') {
            const info = core.info || {};
            const items = Array.isArray(info.items) ? info.items.slice(0, 28) : [];
            const op = String(info.op || '').trim();
            const path = String(info.path || '').trim();
            const excerpt = String(info.excerpt || '').trim();
            const lineCount = Number(info.lineCount || 0);
            const treeHtml = items.map((item) => {
                const name = String(item?.name || item || '').trim();
                const type = String(item?.type || (name.endsWith('/') ? 'dir' : 'file')).trim();
                const cleanBase = path && path !== '.' ? String(path).replace(/\/+$/g, '') : '';
                const cleanName = name.replace(/\/+$/g, '');
                const nodePath = cleanBase ? `${cleanBase}/${cleanName}` : cleanName;
                const command = type === 'dir' ? `list files ${nodePath}` : `read file ${nodePath}`;
                return `<button class="files-node ${escapeAttr(type)}" type="button" data-command="${escapeAttr(command)}">${escapeHtml(name || '(item)')}</button>`;
            }).join('');
            return `<div class="scene scene-files interactive">
                <canvas class="scene-canvas generic-canvas" data-scene="generic"></canvas>
                <div class="scene-grid"></div>
                <div class="files-head">
                    <div class="files-title">${escapeHtml(path || 'workspace')}</div>
                    <div class="files-meta">${escapeHtml(op || 'files')} | ${escapeHtml(String(items.length || lineCount))}</div>
                </div>
                <div class="files-body">
                    <div class="files-tree">${treeHtml || '<div class="files-node">No entries</div>'}</div>
                    <pre class="files-preview">${escapeHtml(excerpt || 'No file preview loaded.')}</pre>
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
            const topCats = Array.from(byCategory.entries())
                .sort((a, b) => b[1] - a[1])
                .slice(0, 5);
            const bars = topCats.map(([category, amount]) => {
                const pct = Math.max(6, Math.min(100, (Number(amount || 0) / Math.max(1, total)) * 100));
                return `<div class="expenses-cat-row">
                    <div class="expenses-cat-label">${escapeHtml(category)}</div>
                    <div class="expenses-cat-bar"><span style="width:${pct}%"></span></div>
                    <div class="expenses-cat-amt">${escapeHtml(formatCurrency(Number(amount || 0)))}</div>
                </div>`;
            }).join('');
            const ledger = expenses.slice(0, 8).map((e) => {
                const category = String(e?.category || 'misc').trim();
                const note = String(e?.note || '').trim();
                const amount = Number(e?.amount || 0);
                return `<div class="expenses-ledger-row">
                    <div class="expenses-ledger-main">${escapeHtml(category)}</div>
                    <div class="expenses-ledger-note">${escapeHtml(note || '-')}</div>
                    <div class="expenses-ledger-amt">${escapeHtml(formatCurrency(amount))}</div>
                </div>`;
            }).join('');
            const avg = expenses.length ? total / expenses.length : 0;
            return `<div class="scene scene-domain scene-expenses interactive">
                <canvas class="scene-canvas domain-canvas" data-scene="expenses" data-total="${escapeAttr(String(total.toFixed(2)))}" data-items="${escapeAttr(String(expenses.length))}"></canvas>
                <div class="expenses-shell">
                    <div class="expenses-kpi-card">
                        <div class="expenses-kpi-label">total spend</div>
                        <div class="expenses-kpi-value">${escapeHtml(formatCurrency(total))}</div>
                        <div class="expenses-kpi-sub">${escapeHtml(String(expenses.length))} entries | avg ${escapeHtml(formatCurrency(avg))}</div>
                    </div>
                    <div class="expenses-cats-card">${bars || '<div class="expenses-cat-row"><div class="expenses-cat-label">no spend</div><div class="expenses-cat-bar"><span style="width:12%"></span></div><div class="expenses-cat-amt">$0.00</div></div>'}</div>
                    <div class="expenses-ledger-card">${ledger || '<div class="expenses-ledger-row"><div class="expenses-ledger-main">No expenses yet</div><div class="expenses-ledger-note">-</div><div class="expenses-ledger-amt">$0.00</div></div>'}</div>
                </div>
            </div>`;
        }
        if (core.kind === 'notes') {
            const notes = (this.state.memory?.notes || []).slice(0, 16);
            const tiles = notes.map((n, idx) => {
                const text = String(n.text || '').trim();
                const stamp = Number(n.createdAt || 0);
                const shortDate = stamp ? new Date(stamp).toLocaleDateString() : 'recent';
                const lines = text.split(/\s+/).slice(0, 20).join(' ');
                return `<article class="notes-card ${idx % 5 === 0 ? 'feature' : ''}">
                    <div class="notes-card-meta">${escapeHtml(shortDate)}</div>
                    <div class="notes-card-text">${escapeHtml(lines || 'note')}</div>
                </article>`;
            }).join('');
            const summary = notes.length
                ? `${notes.length} notes active`
                : 'No notes yet';
            return `<div class="scene scene-domain scene-notes interactive">
                <canvas class="scene-canvas domain-canvas" data-scene="notes" data-count="${escapeAttr(String(notes.length))}"></canvas>
                <div class="notes-shell">
                    <div class="notes-headline">
                        <div class="notes-title">knowledge stream</div>
                        <div class="notes-sub">${escapeHtml(summary)}</div>
                    </div>
                    <div class="notes-wall">${tiles || '<article class="notes-card"><div class="notes-card-text">No notes yet.</div></article>'}</div>
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
                `<img class="mcp-image" src="${escapeAttr(String(img.url || ''))}" alt="MCP image" loading="lazy" />`
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
                ? `<img class="webdeck-favicon" src="${escapeAttr(favicon)}" alt="" loading="lazy" onerror="this.style.display='none'">`
                : '<div class="webdeck-favicon-fallback"></div>';
            const resultsHtml = isSearch
                ? items.map((item) => {
                    const itemTitle = String(item.title || '').trim().slice(0, 90);
                    const itemUrl = String(item.url || '').trim();
                    const itemSnippet = String(item.snippet || '').trim().slice(0, 160);
                    const itemHost = String(item.host || '').trim() || (itemUrl ? safeHostname(itemUrl) : '');
                    const itemThumb = String(item.thumbnail || '').trim();
                    const itemFav = String(item.favicon || '').trim();
                    return `<a class="webdeck-result-card" href="${escapeAttr(itemUrl)}" target="_blank" rel="noopener noreferrer">
                        <div class="webdeck-result-media">
                            ${itemThumb ? `<img class="webdeck-result-thumb" src="${escapeAttr(itemThumb)}" alt="${escapeAttr(itemTitle)}" loading="lazy" onerror="this.style.display='none'">` : `<div class="webdeck-result-thumb-fallback">${itemFav ? `<img class="webdeck-result-favicon" src="${escapeAttr(itemFav)}" alt="" loading="lazy" onerror="this.style.display='none'">` : ''}</div>`}
                        </div>
                        <div class="webdeck-result-title">${escapeHtml(itemTitle)}</div>
                        <div class="webdeck-result-host">${escapeHtml(itemHost)}</div>
                        ${itemSnippet ? `<div class="webdeck-result-snippet">${escapeHtml(itemSnippet)}</div>` : ''}
                    </a>`;
                }).join('')
                : (excerpt
                    ? `<div class="webdeck-page-preview">
                        ${thumbnail ? `<img class="webdeck-page-hero" src="${escapeAttr(thumbnail)}" alt="${escapeAttr(title || 'Page preview')}" loading="lazy" onerror="this.style.display='none'">` : ''}
                        <div class="webdeck-page-text">${escapeHtml(excerpt.slice(0, 400))}</div>
                    </div>`
                    : '');
            const directBtn = siteTarget
                ? `<a class="webdeck-direct-btn scene-chip scene-chip-link" href="${escapeAttr(String(siteTarget.url || ''))}" target="_blank" rel="noopener noreferrer">${escapeHtml(String(siteTarget.label || 'Open source'))}</a>`
                : (url ? `<a class="webdeck-direct-btn scene-chip scene-chip-link" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">Open page</a>` : '');
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
                                    <webview class="webdeck-live-frame webdeck-webview" src="${escapeAttr(fullViewUrl)}" allowpopups partition="persist:genome-browser"></webview>
                                    <div class="webdeck-live-meta">
                                        <span>live source: ${escapeHtml(safeHostname(fullViewUrl))}</span>
                                        <a href="${escapeAttr(fullViewUrl)}" target="_blank" rel="noopener noreferrer">open in tab</a>
                                    </div>
                                </div>
                            `
                    : `
                                <div class="webdeck-live-surface">
                                    <iframe class="webdeck-live-frame" src="${escapeAttr(fullViewUrl)}" title="${escapeAttr(title || 'Web live surface')}" loading="eager" referrerpolicy="no-referrer" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-top-navigation-by-user-activation"></iframe>
                                    <div class="webdeck-live-meta">
                                        <span>live source: ${escapeHtml(safeHostname(fullViewUrl))}</span>
                                        <a href="${escapeAttr(fullViewUrl)}" target="_blank" rel="noopener noreferrer">open in tab</a>
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
                                ${sourceUrl ? `<a class="webdeck-inspector-link" href="${escapeAttr(sourceUrl)}" target="_blank" rel="noopener noreferrer">open source</a>` : ''}
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
            const lineSizes = [0.85, 0.62, 0.78, 0, 0.70, 0.88, 0.45, 0, 0.75, 0.60];
            const docLines  = lineSizes.map((w) => w === 0
                ? '<div class="doc-line gap"></div>'
                : `<div class="doc-line"><span style="width:${(w * 100).toFixed(0)}%"></span></div>`).join('');
            return `<div class="scene scene-computer scene-document interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="document"></canvas>
                <div class="doc-shell">
                    <div class="doc-header">
                        <div class="doc-action-badge">${escapeHtml(action)}</div>
                        <div class="doc-title">${escapeHtml(title)}</div>
                        ${topic && name ? `<div class="doc-topic">${escapeHtml(topic)}</div>` : ''}
                    </div>
                    <div class="doc-page">
                        <div class="doc-page-title">${escapeHtml(title)}</div>
                        <div class="doc-page-lines">${docLines}</div>
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'spreadsheet') {
            const info   = core.info || {};
            const action = String(info.action || 'create').trim();
            const name   = String(info.name   || 'New Spreadsheet').trim();
            const cols   = ['A', 'B', 'C', 'D', 'E'];
            const rows   = [['Category','Q1','Q2','Q3','Q4'],['Revenue','—','—','—','—'],['Expenses','—','—','—','—'],['Profit','—','—','—','—']];
            const hdrHtml = ['', ...cols].map((c) => `<div class="ss-cell ss-head">${escapeHtml(c)}</div>`).join('');
            const rowsHtml = rows.map((row, ri) => `<div class="ss-row"><div class="ss-cell ss-rownum">${ri + 1}</div>${row.map((cell, ci) => `<div class="ss-cell ${ri === 0 || ci === 0 ? 'ss-label' : ''}">${escapeHtml(cell)}</div>`).join('')}</div>`).join('');
            return `<div class="scene scene-computer scene-spreadsheet interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="spreadsheet"></canvas>
                <div class="ss-shell">
                    <div class="ss-header">
                        <div class="ss-action-badge">${escapeHtml(action)}</div>
                        <div class="ss-title">${escapeHtml(name)}</div>
                    </div>
                    <div class="ss-table">
                        <div class="ss-col-headers">${hdrHtml}</div>
                        ${rowsHtml}
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'presentation') {
            const info   = core.info || {};
            const action = String(info.action || 'create').trim();
            const name   = String(info.name   || '').trim();
            const topic  = String(info.topic  || '').trim();
            const slides = Number(info.slides || 8);
            const title  = name || topic || 'New Presentation';
            const thumbs = Array.from({ length: Math.min(slides, 6) }, (_, i) => `
                <div class="pres-thumb ${i === 0 ? 'active' : ''}">
                    <div class="pres-thumb-inner"><div class="pres-thumb-line l1"></div><div class="pres-thumb-line l2"></div><div class="pres-thumb-line l3"></div></div>
                    <div class="pres-thumb-num">${i + 1}</div>
                </div>`).join('');
            return `<div class="scene scene-computer scene-presentation interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="presentation"></canvas>
                <div class="pres-shell">
                    <div class="pres-filmstrip">${thumbs}</div>
                    <div class="pres-stage">
                        <div class="pres-slide">
                            <div class="pres-slide-eyebrow">${escapeHtml(action)} · ${escapeHtml(String(slides))} slides</div>
                            <div class="pres-slide-title">${escapeHtml(title)}</div>
                            <div class="pres-slide-sub">${escapeHtml(topic || 'Generated presentation')}</div>
                            <div class="pres-slide-lines"><div class="pres-slide-line"></div><div class="pres-slide-line short"></div><div class="pres-slide-line"></div></div>
                        </div>
                    </div>
                    <div class="pres-controls">
                        <div class="pres-action-badge">${escapeHtml(action)}</div>
                        <div class="pres-count">${escapeHtml(String(slides))} slides</div>
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'code') {
            const info     = core.info || {};
            const action   = String(info.action   || 'create').trim();
            const language = String(info.language || '').trim();
            const topic    = String(info.topic    || '').trim();
            const name     = String(info.name     || '').trim();
            const fname    = name || topic || 'untitled';
            const fakeCode = [
                `<span class="ck">function</span> <span class="cf">${escapeHtml(name || 'main')}</span>() {`,
                `  <span class="ck">const</span> result = <span class="cf">initialize</span>()`,
                `  <span class="ck">if</span> (result.<span class="cp">ok</span>) {`,
                `    <span class="cf">process</span>(result.<span class="cp">data</span>)`,
                `    <span class="ck">return</span> <span class="cs">'success'</span>`,
                `  }`,
                `  <span class="ck">return</span> <span class="cs">'error'</span>`,
                `}`,
            ].map((src, i) => `<div class="code-line"><span class="code-lnum">${i + 1}</span><span class="code-src">${src}</span></div>`).join('');
            const actionLabel = action === 'explain' ? '// explaining' : action === 'debug' ? '// debugging' : action === 'run' ? '// running' : `// ${action}`;
            return `<div class="scene scene-computer scene-code interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="code"></canvas>
                <div class="code-shell">
                    <div class="code-header">
                        <div class="code-dots"><span></span><span></span><span></span></div>
                        ${language ? `<div class="code-lang-badge">${escapeHtml(language)}</div>` : ''}
                        <div class="code-action-badge">${escapeHtml(action)}</div>
                        <div class="code-filename">${escapeHtml(fname.slice(0, 40))}</div>
                    </div>
                    <div class="code-editor">
                        ${fakeCode}
                        <div class="code-status-line">
                            <span class="code-status-action">${escapeHtml(actionLabel)}</span>
                            ${topic ? `<span class="code-status-topic">${escapeHtml(topic.slice(0, 60))}</span>` : ''}
                        </div>
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'terminal') {
            const info    = core.info || {};
            const command = String(info.command || '').trim();
            const outLines = ['Initializing session...', 'Environment ready'].map((l) => `<div class="term-output-line">${escapeHtml(l)}</div>`).join('');
            return `<div class="scene scene-computer scene-terminal interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="terminal"></canvas>
                <div class="term-shell">
                    <div class="term-bar">
                        <div class="term-dots"><span class="term-dot red"></span><span class="term-dot yellow"></span><span class="term-dot green"></span></div>
                        <div class="term-title">terminal</div>
                    </div>
                    <div class="term-body">
                        ${outLines}
                        <div class="term-prompt-line">
                            <span class="term-prompt">$ </span>
                            <span class="term-cmd">${escapeHtml(command)}</span>
                            <span class="term-cursor"></span>
                        </div>
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'calendar') {
            const info   = core.info || {};
            const action = String(info.action || 'list').trim();
            const title  = String(info.title  || '').trim();
            const date   = String(info.date   || 'today').trim();
            const isCreate = action === 'create';
            const isCancel = action === 'cancel';
            const agendaItems = [
                { time: '9:00 AM',  label: 'Team standup',  dot: 'blue'   },
                { time: '11:00 AM', label: 'Design review',  dot: 'purple' },
                { time: '2:00 PM',  label: title || 'New event', dot: 'green'  },
                { time: '4:30 PM',  label: 'Wrap-up sync',  dot: 'blue'   },
            ];
            const agendaHtml = agendaItems.map((item, i) => `
                <div class="cal-item ${i === 2 ? 'cal-item-focus' : ''}">
                    <div class="cal-item-time">${escapeHtml(item.time)}</div>
                    <div class="cal-item-dot ${escapeAttr(item.dot)}"></div>
                    <div class="cal-item-title">${escapeHtml(item.label)}</div>
                </div>`).join('');
            return `<div class="scene scene-computer scene-calendar interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="calendar"></canvas>
                <div class="cal-shell">
                    <div class="cal-header">
                        <div class="cal-action-badge ${isCancel ? 'cancel' : ''}">${escapeHtml(action)}</div>
                        <div class="cal-date">${escapeHtml(date)}</div>
                    </div>
                    ${isCreate ? `<div class="cal-event-card">
                        <div class="cal-event-title">${escapeHtml(title || 'New Event')}</div>
                        <div class="cal-event-meta">${escapeHtml(date)}</div>
                        <div class="cal-event-status">generating event...</div>
                    </div>` : ''}
                    <div class="cal-agenda">${agendaHtml}</div>
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
            const fakeInbox = [
                { from: 'Sarah M.',          subject: 'Re: Project update', preview: "Looks great, let's sync...", time: '10:42 AM', unread: true  },
                { from: 'Team Alerts',       subject: 'Daily digest',        preview: 'Here is your summary...',  time: '8:01 AM',  unread: false },
                { from: from || 'Mike T.',   subject: subject || query || 'Meeting notes', preview: 'Attached are the notes...', time: 'Yesterday', unread: action === 'reply' },
            ];
            const inboxHtml = fakeInbox.map((msg, i) => `
                <div class="email-msg ${msg.unread ? 'unread' : ''} ${i === 2 ? 'focused' : ''}">
                    <div class="email-msg-from">${escapeHtml(msg.from)}</div>
                    <div class="email-msg-subject">${escapeHtml(msg.subject)}</div>
                    <div class="email-msg-preview">${escapeHtml(msg.preview)}</div>
                    <div class="email-msg-time">${escapeHtml(msg.time)}</div>
                </div>`).join('');
            const composeHtml = `<div class="email-compose">
                <div class="email-field"><span class="email-field-label">To</span><span class="email-field-value">${escapeHtml(to || '...')}</span></div>
                <div class="email-field"><span class="email-field-label">Subject</span><span class="email-field-value">${escapeHtml(subject || '...')}</span></div>
                <div class="email-divider"></div>
                <div class="email-body-lines">
                    <div class="email-body-line long"></div><div class="email-body-line mid"></div>
                    <div class="email-body-line long"></div><div class="email-body-line short"></div>
                </div></div>`;
            return `<div class="scene scene-computer scene-email interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="email"></canvas>
                <div class="email-shell">
                    <div class="email-sidebar">
                        <div class="email-folder ${!isCompose ? 'active' : ''}">Inbox</div>
                        <div class="email-folder">Sent</div>
                        <div class="email-folder ${isCompose ? 'active' : ''}">Drafts</div>
                    </div>
                    <div class="email-main">
                        <div class="email-header">
                            <div class="email-action-badge">${escapeHtml(action)}</div>
                            ${query ? `<div class="email-query">"${escapeHtml(query)}"</div>` : ''}
                        </div>
                        ${isCompose ? composeHtml : `<div class="email-inbox">${inboxHtml}</div>`}
                    </div>
                </div>
            </div>`;
        }
        if (core.kind === 'content') {
            const info     = core.info || {};
            const action   = String(info.action || 'find').trim();
            const name     = String(info.name   || '').trim();
            const type     = String(info.type   || '').trim();
            const isHistory = ['history', 'branch', 'revert'].includes(action);
            const listItems = [
                { name: name || 'Q4 Report',       type: type || 'document',     ver: 'v3', time: '2h ago'   },
                { name: 'Budget 2026',             type: 'spreadsheet',           ver: 'v7', time: 'yesterday'},
                { name: 'Product Roadmap',         type: 'presentation',          ver: 'v2', time: '3d ago'  },
            ];
            const histItems = [
                { ver: 'HEAD', msg: 'Latest revision',         time: 'now',       current: true  },
                { ver: 'v3',   msg: 'Updated section 2',       time: '2h ago',    current: false },
                { ver: 'v2',   msg: 'Added executive summary', time: 'yesterday', current: false },
                { ver: 'v1',   msg: 'Initial draft',           time: '3d ago',    current: false },
            ];
            const listHtml = listItems.map((item) => `
                <div class="cnt-item">
                    <div class="cnt-item-icon ${escapeAttr(item.type)}"></div>
                    <div class="cnt-item-name">${escapeHtml(item.name)}</div>
                    <div class="cnt-item-type">${escapeHtml(item.type)}</div>
                    <div class="cnt-item-ver">${escapeHtml(item.ver)}</div>
                    <div class="cnt-item-time">${escapeHtml(item.time)}</div>
                </div>`).join('');
            const histHtml = histItems.map((item) => `
                <div class="cnt-hist-item ${item.current ? 'current' : ''}">
                    <div class="cnt-hist-ver">${escapeHtml(item.ver)}</div>
                    <div class="cnt-hist-msg">${escapeHtml(item.msg)}</div>
                    <div class="cnt-hist-time">${escapeHtml(item.time)}</div>
                </div>`).join('');
            return `<div class="scene scene-computer scene-content interactive">
                <canvas class="scene-canvas computer-canvas" data-scene="content"></canvas>
                <div class="cnt-shell">
                    <div class="cnt-header">
                        <div class="cnt-action-badge">${escapeHtml(action)}</div>
                        ${type ? `<div class="cnt-type-badge">${escapeHtml(type)}</div>` : ''}
                        ${name ? `<div class="cnt-name">${escapeHtml(name)}</div>` : ''}
                    </div>
                    ${isHistory ? `<div class="cnt-history">${histHtml}</div>` : `<div class="cnt-list">${listHtml}</div>`}
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
            const items = Array.isArray(data.items) ? data.items : [];
            return [
                { label: 'Source', value: String(data.source || 'scaffold'), tone: 'accent' },
                { label: 'Mode', value: latest.op === 'social_message_send' ? 'send' : 'feed', tone: 'neutral' },
                { label: 'Items', value: String(items.length), tone: 'cool' },
                { label: 'Delivery', value: String(data.delivery || (latest.op === 'social_message_send' ? 'queued' : 'n/a')), tone: 'warm' },
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
        } else if (scene === 'graph') {
            this._sceneRenderer = this.makeGraphRenderer(canvas);
        } else if (scene === 'webdeck') {
            this._sceneRenderer = this.makeWebdeckRenderer(canvas);
        } else if (scene === 'mcp') {
            this._sceneRenderer = this.makeMcpRenderer(canvas);
        } else if (scene === 'sports') {
            this._sceneRenderer = this.makeSportsRenderer(canvas);
        } else if (['document','spreadsheet','presentation','code','terminal','calendar','email','content'].includes(scene)) {
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

    makeTasksRenderer(canvas) {
        const open = Math.max(0, Number(canvas.dataset.open || 0));
        const done = Math.max(0, Number(canvas.dataset.done || 0));
        const total = Math.max(1, open + done);
        const lanes = 10;
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.013;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);
            const bg = ctx.createLinearGradient(0, 0, 0, h);
            bg.addColorStop(0, 'rgba(24, 55, 87, 0.22)');
            bg.addColorStop(1, 'rgba(24, 55, 87, 0.03)');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            ctx.strokeStyle = 'rgba(24, 55, 87, 0.12)';
            ctx.lineWidth = 1;
            for (let i = 0; i <= lanes; i += 1) {
                const y = Math.round((i / lanes) * h);
                ctx.beginPath();
                ctx.moveTo(0, y);
                ctx.lineTo(w, y);
                ctx.stroke();
            }
            const completion = done / total;
            const sweep = Math.max(0.08, completion) * w;
            const pulse = 0.2 + Math.sin(t * 2) * 0.08;
            const grad = ctx.createLinearGradient(0, 0, sweep, 0);
            grad.addColorStop(0, `rgba(20, 184, 166, ${0.18 + pulse})`);
            grad.addColorStop(1, 'rgba(20, 184, 166, 0)');
            ctx.fillStyle = grad;
            ctx.fillRect(0, 0, sweep, h);
        };
    },

    makeExpensesRenderer(canvas) {
        const total = Math.max(0, Number(canvas.dataset.total || 0));
        const entries = Math.max(1, Number(canvas.dataset.items || 1));
        const bars = Math.max(8, Math.min(22, entries * 2));
        const amplitude = Math.min(1, total / 1500);
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.02;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);
            const bg = ctx.createLinearGradient(0, 0, 0, h);
            bg.addColorStop(0, 'rgba(9, 94, 76, 0.18)');
            bg.addColorStop(1, 'rgba(9, 94, 76, 0.03)');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            const bw = w / (bars * 1.4);
            for (let i = 0; i < bars; i += 1) {
                const x = i * bw * 1.4 + 8;
                const signal = (Math.sin(t + i * 0.38) + 1) / 2;
                const hPct = 0.16 + signal * (0.42 + amplitude * 0.3);
                const bh = Math.max(10, h * hPct);
                const y = h - bh;
                const alpha = 0.22 + signal * 0.3;
                ctx.fillStyle = `rgba(16, 158, 129, ${alpha})`;
                ctx.fillRect(x, y, bw, bh);
            }
        };
    },

    makeNotesRenderer(canvas) {
        const count = Math.max(1, Number(canvas.dataset.count || 1));
        const particles = Array.from({ length: Math.min(36, 10 + count * 3) }, (_, i) => ({
            x: ((i * 37) % 997) / 997,
            y: ((i * 71) % 991) / 991,
            s: 0.4 + ((i * 13) % 10) / 10,
            p: i * 0.27,
        }));
        let t = 0;
        return () => {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;
            const { dpr, w, h } = this.fitCanvas(canvas);
            t += 0.01;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);
            const bg = ctx.createLinearGradient(0, 0, w, h);
            bg.addColorStop(0, 'rgba(64, 82, 181, 0.16)');
            bg.addColorStop(1, 'rgba(64, 82, 181, 0.02)');
            ctx.fillStyle = bg;
            ctx.fillRect(0, 0, w, h);
            particles.forEach((p, idx) => {
                const x = (p.x * w + Math.sin(t + p.p) * 16) % (w + 20);
                const y = (p.y * h + Math.cos(t * 1.1 + p.p) * 12) % (h + 20);
                const alpha = 0.12 + ((Math.sin(t * 1.7 + idx) + 1) / 2) * 0.2;
                ctx.fillStyle = `rgba(84, 106, 223, ${alpha})`;
                ctx.beginPath();
                ctx.arc(x, y, 2 + p.s * 3, 0, Math.PI * 2);
                ctx.fill();
            });
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
        this.state.history.push({
            intent,
            summary: execution.message || plan.subtitle,
            timestamp: Date.now(),
            envelope,
            executionSnapshot: execution ? JSON.parse(JSON.stringify(execution)) : null,
            plan,
            kernelTrace,
            merge,
            plannerSource,
            memorySnapshot: JSON.parse(JSON.stringify(this.state.memory))
        });

        if (this.state.history.length > HISTORY_LIMIT) this.state.history.shift();
        this.state.session.activeHistoryIndex = this.state.history.length - 1;
        this.updateHistoryReel();
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

    updateHistoryReel() {
        this.historyReel.innerHTML = this.state.history.map((entry, index) => `
            <button class="history-node ${index === this.state.session.activeHistoryIndex ? 'active' : ''}" data-history-index="${index}" data-label="${escapeAttr(entry.intent)}" title="${escapeAttr(entry.summary)}"></button>
        `).join('');
    },

    restoreFromHistory(index) {
        const entry = this.state.history[index];
        if (!entry) return;

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
            this.state.memory = parsed.memory || structuredClone(DEFAULT_MEMORY);
            this.state.history = parsed.history || [];
            this.state.intentHistory = Array.isArray(parsed.intentHistory)
                ? parsed.intentHistory.filter((x) => typeof x === 'string').slice(-60)
                : [];
            this.state.intentHistoryIndex = this.state.intentHistory.length;
            this.state.session.sessionId = parsed.sessionId || localStorage.getItem(SESSION_STORAGE_KEY) || '';
            this.state.session.deviceId = parsed.deviceId || localStorage.getItem(DEVICE_STORAGE_KEY) || '';
            this.state.session.handoff = parsed.handoff || this.state.session.handoff;
            this.state.session.presence = parsed.presence || this.state.session.presence;
            this.state.session.revision = Number(parsed.revision || 0);
            this.state.session.locationHint = String(parsed.locationHint || '').trim();
            this.state.runtimeEvents = Array.isArray(parsed.runtimeEvents)
                ? parsed.runtimeEvents.filter((item) => item && typeof item === 'object').slice(-8)
                : [];
            const mode = String(parsed?.uiPrefs?.webdeckMode || '').toLowerCase();
            if (mode === 'surface' || mode === 'full') {
                this.state.webdeck.mode = mode;
            }
        } catch {
            this.state.memory = structuredClone(DEFAULT_MEMORY);
            this.state.history = [];
            this.state.intentHistory = [];
            this.state.intentHistoryIndex = -1;
        }
    },

    saveState() {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({
            memory: this.state.memory,
            history: this.state.history,
            intentHistory: this.state.intentHistory,
            sessionId: this.state.session.sessionId,
            deviceId: this.state.session.deviceId,
            handoff: this.state.session.handoff,
            presence: this.state.session.presence,
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
            memory.tasks.unshift({ id: crypto.randomUUID(), title, done: false, createdAt: Date.now() });
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
                id: crypto.randomUUID(),
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
            memory.notes.unshift({ id: crypto.randomUUID(), text, createdAt: Date.now() });
            return { ok: true, message: 'Note captured.' };
        },

        reset_memory(memory) {
            memory.tasks = structuredClone(DEFAULT_MEMORY.tasks);
            memory.expenses = structuredClone(DEFAULT_MEMORY.expenses);
            memory.notes = structuredClone(DEFAULT_MEMORY.notes);
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
