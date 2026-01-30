/**
 * GENERATIVE UI ENGINE (The 'No-App' OS)
 * Core logic for the Latent Surface
 */

const UIEngine = {
    container: document.getElementById('ui-container'),
    input: document.getElementById('intent-input'),
    status: document.getElementById('status'),

    // Global State (The "Meaning" that survives UI changes)
    state: {
        lastIntent: null,
        data: {},
        history: [], // History of Shards
        metrics: {
            entropy: 0.02,
            latency: 0
        }
    },

    init() {
        this.loadState();
        this.input.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                this.handleIntent(this.input.value);
                this.input.value = '';
            }
        });

        // Initial entrance
        setTimeout(() => this.container.classList.add('visible'), 500);
    },

    async handleIntent(text) {
        if (!text.trim()) return;

        const startTime = performance.now();
        this.status.innerText = "CAPTURING INTENT...";
        this.container.classList.add('refracting');
        document.querySelector('.input-container').classList.add('active-intent');

        // Step 1: Reasoning Overlay
        await this.showReasoning([
            "Analyzing semantic vectors...",
            "Mapping intent to capability space...",
            "Synthesizing optimal component tree...",
            "Resolving global state dependencies..."
        ]);

        // Process Intent
        const projection = await IntentProcessor.process(text, this.state);

        // Simulated Synthesis Delay
        setTimeout(() => {
            this.container.classList.remove('refracting');
            document.querySelector('.input-container').classList.remove('active-intent');
            this.render(projection);

            // Add to history
            this.state.history.push({ intent: text, projection });
            this.updateHistoryReel();

            const endTime = performance.now();
            this.state.metrics.latency = Math.round(endTime - startTime);
            this.updateStatus();
        }, 300);
    },

    async showReasoning(steps) {
        const overlay = document.getElementById('reasoning-overlay');
        overlay.style.display = 'flex';
        overlay.innerHTML = '';

        for (const step of steps) {
            const div = document.createElement('div');
            div.className = 'reasoning-step';
            div.innerText = step;
            overlay.appendChild(div);

            // Trigger animation
            setTimeout(() => div.classList.add('visible'), 50);
            await new Promise(r => setTimeout(r, 450));
        }

        await new Promise(r => setTimeout(r, 300));
        overlay.style.display = 'none';
    },

    render(projection) {
        // Update persistent state data if provided
        if (projection.stateUpdates) {
            this.state.data = { ...this.state.data, ...projection.stateUpdates };
            this.saveState();
        }

        // Build HTML from projection
        let html = `
            <div class="h1">${projection.title}</div>
            <div class="desc">${projection.description}</div>
            <div class="grid">
                ${projection.components.map(c => this.renderComponent(c)).join('')}
            </div>
            <div class="latent-meta">
                <div class="label" style="margin-bottom: 12px; opacity: 0.5;">Active Semantic Map</div>
                <div class="state-chip-container">
                    ${Object.entries(this.state.data).map(([k, v]) => `
                        <div class="state-chip">
                            <span class="key">${k}:</span>
                            <span class="val">${v}</span>
                        </div>
                    `).join('')}
                </div>
            </div>
            ${projection.controls ? `
                <div class="controls" style="margin-top: 24px; display: flex; gap: 12px;">
                    ${projection.controls.map(ctrl => `<button class="button" onclick="${ctrl.action}">${ctrl.label}</button>`).join('')}
                </div>
            ` : ''}
        `;

        this.container.innerHTML = html;
        this.container.classList.add('visible');
    },

    loadState() {
        const saved = localStorage.getItem('genui_state');
        if (saved) {
            try {
                const parsed = JSON.parse(saved);
                this.state.data = parsed.data || {};
            } catch (e) { }
        }
    },

    saveState() {
        localStorage.setItem('genui_state', JSON.stringify({ data: this.state.data }));
    },

    renderComponent(comp) {
        switch (comp.type) {
            case 'metric':
                return `
                    <div class="card">
                        <div class="label">${comp.label}</div>
                        <div class="value" style="color: ${comp.color || 'inherit'}">${comp.value}</div>
                    </div>
                `;
            case 'chart':
                // Simple CSS-based bar chart for mock
                return `
                    <div class="card" style="grid-column: span 2;">
                        <div class="label">${comp.label}</div>
                        <div style="display: flex; align-items: flex-end; height: 100px; gap: 8px; margin-top: 16px;">
                            ${comp.data.map(d => `
                                <div style="flex: 1; height: ${d}%; background: var(--accent); opacity: 0.8; border-radius: 4px;"></div>
                            `).join('')}
                        </div>
                    </div>
                `;
            case 'list':
                return `
                    <div class="card" style="grid-column: span 1;">
                        <div class="label">${comp.label}</div>
                        <div style="margin-top: 12px;">
                            ${comp.items.map(i => `
                                <div style="padding: 8px 0; border-bottom: 1px solid var(--border-dim); font-size: 14px;">
                                    ${i}
                                </div>
                            `).join('')}
                        </div>
                    </div>
                `;
            case 'table':
                return `
                    <div class="card" style="grid-column: span 2;">
                        <div class="label" style="margin-bottom: 16px;">${comp.label}</div>
                        <div class="table-container">
                            <table>
                                <thead>
                                    <tr>${comp.headers.map(h => `<th>${h}</th>`).join('')}</tr>
                                </thead>
                                <tbody>
                                    ${comp.rows.map(row => `
                                        <tr>${row.map(cell => `<td>${cell}</td>`).join('')}</tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        </div>
                    </div>
                `;
            case 'code':
                return `
                    <div class="card" style="grid-column: span 2;">
                        <div class="label" style="margin-bottom: 12px;">${comp.label}</div>
                        <div class="code-surface">
                            <pre><code class="language-${comp.lang || 'javascript'}">${comp.content}</code></pre>
                        </div>
                    </div>
                `;
            case 'logs':
                return `
                    <div class="card" style="grid-column: span 2;">
                        <div class="label" style="margin-bottom: 16px;">${comp.label}</div>
                        <div class="log-surface">
                            ${comp.entries.map(e => `
                                <div class="log-entry">
                                    <span class="timestamp">${e.time || '15:34:02'}</span>
                                    <span class="source">[${e.source || 'SYSTEM'}]</span>
                                    <span class="msg">${e.msg}</span>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                `;
            default:
                return '';
        }
    },

    updateHistoryReel() {
        const reel = document.getElementById('history-reel');
        reel.innerHTML = this.state.history.map((h, i) => `
            <div class="history-node ${i === this.state.history.length - 1 ? 'active' : ''}" 
                 data-label="${h.intent}" 
                 onclick="UIEngine.restoreFromHistory(${i})">
            </div>
        `).join('');
    },

    restoreFromHistory(index) {
        const item = this.state.history[index];
        if (!item) return;

        this.container.classList.add('refracting');
        setTimeout(() => {
            this.render(item.projection);
            this.container.classList.remove('refracting');

            // Mark as active in reel
            const nodes = document.querySelectorAll('.history-node');
            nodes.forEach((n, i) => n.classList.toggle('active', i === index));

            this.status.innerText = `RESTORED FROM INTENT: ${item.intent}`;
        }, 300);
    },

    updateStatus() {
        this.status.innerText = `LATENCY: ${this.state.metrics.latency}ms | ENTROPY: ${(Math.random() * 0.05).toFixed(3)} | STATE: STABLE`;
    }
};

const IntentProcessor = {
    async process(text, state) {
        const query = text.toLowerCase();

        // 1. Financial Inquiry
        if (query.includes('money') || query.includes('expense') || query.includes('spend')) {
            return {
                title: "Financial Matrix",
                description: "Synthesized overview of your current liquidity and spend vectors.",
                stateUpdates: { last_context: 'finance', currency: 'USD', risk_level: 'low' },
                components: [
                    { type: 'metric', label: 'Net Liquidity', value: '$12,450.00', color: 'var(--success)' },
                    { type: 'metric', label: 'Monthly Burn', value: '$3,200', color: 'var(--danger)' },
                    { type: 'chart', label: 'Spend Velocity (7d)', data: [20, 45, 30, 80, 50, 90, 40] }
                ],
                controls: [
                    { label: 'Forecast Next 30 Days', action: "alert('Calculating projections...')" },
                    { label: 'Export Ledger', action: "alert('Preparing semantic CSV...')" }
                ]
            };
        }

        // 2. Technical / Research (NCA/ML)
        if (query.includes('nca') || query.includes('model') || query.includes('train')) {
            return {
                title: "Latent Model Observer",
                description: "Real-time projection of parameters for 'Genome-V9'.",
                stateUpdates: { last_context: 'research', active_model: 'Genome-V9', epoch: 142 },
                components: [
                    { type: 'metric', label: 'Entropy Stability', value: '0.992' },
                    { type: 'metric', label: 'Loss Converge', value: '0.0042', color: 'var(--accent)' },
                    {
                        type: 'table',
                        label: 'Layer Activation Statistics',
                        headers: ['Layer', 'Mean Act.', 'Peak Act.', 'Sparsity'],
                        rows: [
                            ['Convolution_1', '0.42', '0.88', '12%'],
                            ['Hidden_Gate_A', '0.15', '0.92', '45%'],
                            ['Fire_Logic_V9', '0.67', '0.99', '2%']
                        ]
                    },
                    {
                        type: 'code',
                        label: 'Active Fire Rate Logic',
                        lang: 'javascript',
                        content: `function calibrate(signal) {\n  const entropy = Math.random() * 0.05;\n  return signal.map(s => s * (1 - entropy));\n}`
                    },
                    {
                        type: 'logs',
                        label: 'System Telemetry',
                        entries: [
                            { source: 'GPU', msg: 'Kernel initialization successful.' },
                            { source: 'NCA', msg: 'Equilibrium detected at step 14,200.' },
                            { source: 'PERSISTENCE', msg: 'Syncing latent weights to local buffer...' },
                            { source: 'ERROR', msg: 'Voltage jitter detected in Layer 4 (Ignored).' }
                        ]
                    }
                ],
                controls: [
                    { label: 'Stabilize Fire Rate', action: "alert('NCA Stabilized.')" }
                ]
            };
        }

        // 3. Productivity / Task Management
        if (query.includes('todo') || query.includes('task') || query.includes('do')) {
            return {
                title: "Action Surface",
                description: "High-priority nodes requiring immediate cognitive processing.",
                stateUpdates: { last_context: 'productivity', focus_mode: true, nodes: 14 },
                components: [
                    { type: 'metric', label: 'Open Loops', value: '14' },
                    { type: 'metric', label: 'Focus Score', value: '82%', color: 'var(--warning)' },
                    {
                        type: 'table',
                        label: 'Cognitive Load Distribution',
                        headers: ['Project', 'Cycles', 'Deadline'],
                        rows: [
                            ['GenUI Core', '8.5h', 'Today'],
                            ['Diffusion Logic', '4.2h', 'Tomorrow'],
                            ['Semantic Graph', '1.1h', 'Friday']
                        ]
                    },
                    {
                        type: 'list', label: 'Primary Objectives', items: [
                            'Finalize GenUI Research Plan',
                            'Refactor Diffusion Gate logic',
                            'Calibrate Latent Field transitions'
                        ]
                    }
                ],
                controls: [
                    { label: 'Optimize Focus', action: "alert('Suppressing notifications.')" }
                ]
            };
        }

        // 4. Visual / OCR Intent
        if (query.includes('image') || query.includes('photo') || query.includes('ocr')) {
            return {
                title: "Optical Parse Surface",
                description: "Extracting high-precision technical data from visual buffer.",
                stateUpdates: { last_context: 'vision', ocr_engine: 'tesseract_optimized', psm: 6 },
                components: [
                    { type: 'metric', label: 'Character Precision', value: '99.4%', color: 'var(--success)' },
                    { type: 'metric', label: 'Nodes Detected', value: '42' },
                    { type: 'list', label: 'Extracted Schema', items: ['Product_ID (String)', 'Timestamp (ISO)', 'Unit_Cost (Float)'] }
                ],
                controls: [
                    { label: 'Map to Global State', action: "alert('Mapping fields...')" }
                ]
            };
        }

        // Default: General Projection
        return {
            title: "General Projection",
            description: `A generalized interface synthesized for: "${text}"`,
            stateUpdates: { last_context: 'unknown', raw_intent: text },
            components: [
                { type: 'metric', label: 'Intent Clarity', value: 'High' },
                {
                    type: 'list', label: 'Estimated Capabilities', items: [
                        'Semantic Search',
                        'Logical Reasoning',
                        'Interface Synthesis'
                    ]
                }
            ]
        };
    }
};

// Start the OS
UIEngine.init();
