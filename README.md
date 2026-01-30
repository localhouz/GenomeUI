# Generative UI (The "No-App" OS)

This is a functional prototype of a **Latent Surface**—a paradigm shift where the OS vanishes into a blank canvas and "hallucinates" perfectly optimized interfaces on demand.

## 🚀 Getting Started

1. **Install Dependencies**:
   ```bash
   npm install
   ```

2. **Launch the Latent Surface**:
   ```bash
   npm run dev
   ```

3. **Navigate to**: `http://localhost:5173`

## 🧩 Architectural Paradigms

### 1. The Latent Surface (`index.html` / `index.css`)
A high-fidelity obsidian workspace designed for "Perceptual Instancy." It uses glassmorphism and fluid typography to create a premium, "living" background that awaits observer intent.

### 2. The UI Engine (`app.js` -> `UIEngine`)
Responsible for **Refraction**. When an intent is received, the engine desynthesizes the current interface and synthesizes a new "Shard" (the projected UI) with <100ms latency targets.

### 3. Intent Processing (`app.js` -> `IntentProcessor`)
In this prototype, we use a semantic keyword mapper that simulates an LLM's ability to project high-dimensional intent onto functional UI requirements. It supports:
- **Finance**: Query "Check my expenses"
- **Research**: Query "NCA stability" 
- **Productivity**: Query "My tasks"

### 4. Semantic Persistence (`app.js` -> `StateStore`)
Data meaning is preserved across UI transitions. Even as the interface "melts" away and reformulates, the underlying state (Active Semantic Map) is tracked and displayed, ensuring a continuous cognitive thread.

## 🛠 Features
- **Atomic Elements**: High-performance metrics, reactive sparklines, and list nodes.
- **Glassmorphism Design System**: Built with Vanilla CSS for maximum performance and flexible styling.
- **Holographic Transitions**: Smooth scaling and opacity shifts that mimic the feeling of a "projected" interface.
- **State Visualization**: Real-time view into the "meaning" the OS has extracted from your session.

---

*Inspired by the [Generative UI Research Plan](./Generative%20UI%20Research%20Plan.md)*
