const STORAGE_KEY = 'genui_memory_v4';
const SESSION_STORAGE_KEY = 'genui_session_v1';
const DEVICE_STORAGE_KEY = 'genui_device_v1';
const HISTORY_LIMIT = 40;
const FALLBACK_POLL_MS = 2500;
const PRESENCE_HEARTBEAT_MS = 30000;

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
        const response = await fetch('/api/turn', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
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
            lastKernelTrace: null,
            handoff: { activeDeviceId: null, pending: null, lastClaimAt: null },
            presence: { activeCount: 0, count: 0, items: [], timeoutMs: 120000, updatedAt: 0 },
            activeHistoryIndex: -1,
            sessionId: '',
            deviceId: '',
            revision: 0,
            isApplyingLocalTurn: false,
            syncTransport: 'idle'
        },
        history: [],
        intentHistory: [],
        intentHistoryIndex: -1,
        metrics: {
            latency: 0,
            entropy: 0.02
        }
    },

    async init() {
        this.loadState();
        this.setupUXChrome();
        this.bindEvents();
        await this.runBootSequence();
        this.showToast('Surface ready. Press ? for command help.', 'info', 3000);
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
                <div class="help-line"><span>?</span><span>Open or close this guide</span></div>
                <div class="help-line"><span>Esc</span><span>Close guide</span></div>
                <div class="help-line"><span>Up or Down</span><span>Recall prior intents</span></div>
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
    },

    async runBootSequence() {
        this.input.disabled = true;
        this.setBootState('Initializing shell context', 8);
        await sleep(120);

        this.setBootState('Wiring session transport', 26);
        await this.bootstrapSession();
        await sleep(90);

        this.setBootState('Compiling startup intent layers', 52);
        const bootstrapText = 'Show me what I can do';
        const envelope = IntentLayerCompiler.compile(bootstrapText, this.state.memory);
        const uiPlan = UIPlanner.build(envelope, this.state.memory, { ok: true, message: 'Surface online.' });
        this.render(UIPlanSchema.normalize(uiPlan), envelope);
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

    startRealtimeSync() {
        this.openWebSocketSync();
        this.startPresenceHeartbeat();
        if (this._syncTimer) clearInterval(this._syncTimer);
        this._syncTimer = setInterval(() => {
            if (this.state.session.syncTransport === 'ws' || this.state.session.syncTransport === 'sse') return;
            this.pollSession().catch(() => { });
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

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws?sessionId=${encodeURIComponent(sessionId)}`;
        const ws = new WebSocket(wsUrl);
        this._ws = ws;

        ws.onopen = () => {
            this.state.session.syncTransport = 'ws';
        };

        ws.onmessage = (event) => {
            try {
                const payload = JSON.parse(event.data);
                this.applyRemoteSync(payload);
            } catch {
                this.state.session.syncTransport = 'poll';
            }
        };

        ws.onerror = () => {
            this.state.session.syncTransport = 'sse';
            this.openSessionStream();
        };

        ws.onclose = () => {
            if (this._ws === ws) this._ws = null;
            if (this.state.session.syncTransport !== 'ws') return;
            this.state.session.syncTransport = 'sse';
            this.openSessionStream();
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
        };

        es.onmessage = (event) => {
            try {
                const payload = JSON.parse(event.data);
                this.applyRemoteSync(payload);
            } catch {
                this.state.session.syncTransport = 'poll';
            }
        };

        es.onerror = () => {
            if (this.state.session.syncTransport !== 'ws') {
                this.state.session.syncTransport = 'poll';
            }
            if (this._eventSource) {
                this._eventSource.close();
                this._eventSource = null;
            }
        };
    },

    async pollSession() {
        const sessionId = this.state.session.sessionId;
        if (!sessionId || this.state.session.isApplyingLocalTurn) return;

        const snapshot = await RemoteTurnService.getSession(sessionId);
        const serverRevision = Number(snapshot.revision || 0);
        if (serverRevision <= this.state.session.revision) return;

        this.state.session.revision = serverRevision;
        this.state.memory = snapshot.memory || this.state.memory;
        this.state.session.handoff = snapshot.handoff || this.state.session.handoff;
        this.state.session.presence = snapshot.presence || this.state.session.presence;
        if (snapshot.lastTurn?.envelope && snapshot.lastTurn?.plan) {
            const plan = UIPlanSchema.normalize(snapshot.lastTurn.plan);
            this.state.session.lastKernelTrace = snapshot.lastTurn.kernelTrace || this.deriveKernelTrace(snapshot.lastTurn.execution, snapshot.lastTurn.route);
            this.render(plan, snapshot.lastTurn.envelope, this.state.session.lastKernelTrace);
            this.updateStatus(`SYNCED:${snapshot.lastTurn.planner || 'REMOTE'}`);
        }
        this.saveState();
    },

    applyRemoteSync(payload) {
        if (!payload || this.state.session.isApplyingLocalTurn) return;
        const revision = Number(payload.revision || 0);
        if (revision <= this.state.session.revision) return;

        const priorHandoff = JSON.stringify(this.state.session.handoff || {});
        this.state.session.revision = revision;
        this.state.memory = payload.memory || this.state.memory;
        this.state.session.handoff = payload.handoff || this.state.session.handoff;
        this.state.session.presence = payload.presence || this.state.session.presence;
        if (payload.lastTurn?.envelope && payload.lastTurn?.plan) {
            const plan = UIPlanSchema.normalize(payload.lastTurn.plan);
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
            if (event.key === 'Escape') {
                this.toggleHelp(false);
                return;
            }

            if (event.key === '?' || event.key === 'F1') {
                event.preventDefault();
                this.toggleHelp();
                return;
            }

            if (event.key !== '/') return;
            const active = document.activeElement;
            const isEditable = active && (
                active.tagName === 'INPUT'
                || active.tagName === 'TEXTAREA'
                || active.isContentEditable
            );
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

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                this.sendPresenceHeartbeat(true).catch(() => { });
            }
        });

        window.addEventListener('beforeunload', () => {
            this.stopPresenceHeartbeat();
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
        const hint = plan.suggestions?.[0] || 'add task Ship onboarding';
        this.input.placeholder = `Try: "${hint}"`;
        const blocks = this.composeFeedBlocks(plan, envelope, kernelTrace);
        const intentText = (envelope?.raw || this.state.session.lastIntent || '').trim() || 'State intent to synthesize your workspace.';
        this.container.innerHTML = `
            <div class="workspace">
                <section class="workspace-main">
                    <div class="surface-core">
                        <div class="surface-label">Workspace</div>
                        <div class="core-intent">${escapeHtml(intentText)}</div>
                        <div class="core-summary">${escapeHtml(plan.subtitle || 'Surface online.')}</div>
                    </div>
                </section>
                <aside class="workspace-side">
                    <div class="feed-head">Activity Feed</div>
                    ${blocks.length ? blocks.map((block) => this.renderFeedBlock(block)).join('') : '<div class="feed-empty">No active signals.</div>'}
                </aside>
            </div>
        `;
    },

    composeFeedBlocks(plan, envelope, kernelTrace) {
        const trace = kernelTrace || this.deriveKernelTrace(this.state.session.lastExecution, null);
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

        const extra = [
            {
                id: 'trace-system',
                type: 'list',
                label: 'System',
                items: systemItems
            },
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

        return [...extra].slice(0, 10);
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
                    <div class="feed-value" style="color:${block.color || 'inherit'}">${escapeHtml(String(block.value ?? ''))}</div>
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
                    <div class="value compact-value" style="color:${block.color || 'inherit'}">${escapeHtml(String(block.value ?? ''))}</div>
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
                    <div class="value" style="color:${block.color || 'inherit'}">${escapeHtml(String(block.value ?? ''))}</div>
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
            revision: this.state.session.revision
        }));
        if (this.state.session.sessionId) {
            localStorage.setItem(SESSION_STORAGE_KEY, this.state.session.sessionId);
        }
    },

    updateStatus(mode) {
        const m = this.state.memory;
        const objectCount = m.tasks.length + m.expenses.length + m.notes.length;
        const sid = this.state.session.sessionId ? this.state.session.sessionId.slice(0, 8) : '-';
        const sync = this.state.session.syncTransport.toUpperCase();
        this.status.innerText = `MODE: ${mode} | SYNC: ${sync} | SESSION: ${sid} | OBJECTS: ${objectCount} | LATENCY: ${this.state.metrics.latency}ms | ENTROPY: ${this.state.metrics.entropy.toFixed(3)}`;
    }
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
                planVersion: typeof plan?.trace?.planVersion === 'string' ? plan.trace.planVersion : 'unknown',
                focusDomains: Array.isArray(plan?.trace?.focusDomains) ? plan.trace.focusDomains : [],
                mode: typeof plan?.trace?.mode === 'string' ? plan.trace.mode : 'default'
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

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

UIEngine.init();
