# Solana Sentinel

A read-only, Phantom-style dashboard for the Sentinel **paper-trading** bot (pump.fun tokens, simulated trades, no real money).
It's a static Progressive Web App hosted on GitHub Pages. Install it on iPhone from Safari: **Share → Add to Home Screen**.

- No wallet, no keys, no trade or deposit buttons. The app only displays data.
- Live data is pushed by the bot's own computer to a public relay (MQTT over secure WebSocket, with an ntfy.sh backup),
  **end-to-end encrypted** (AES-256-GCM). The private key lives only in the owner's link (`#k=…`), which browsers never
  send to a server. Nothing secret is stored in this repository.
- Without the private link the app shows an "open your private link" screen.

Files: `index.html` (whole app), `manifest.webmanifest`, `sw.js` (offline app shell), `icons/`, `.nojekyll`,
`.github/workflows/pages.yml` (deploys the repo root to GitHub Pages on every push to `main`).
