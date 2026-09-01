# 🤖 Yaazhi — Your Personal Jarvis, Built on ₹0

<div align="center">

![Python](https://img.shields.io/badge/Python-3.11-blue?style=flat-square&logo=python)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)
![Status](https://img.shields.io/badge/Status-Active%20Development-orange?style=flat-square)
![Made in India](https://img.shields.io/badge/Made%20in-India%20🇮🇳-orange?style=flat-square)
![LangGraph](https://img.shields.io/badge/LangGraph-0.2+-purple?style=flat-square)

**A self-hosted, multi-agent AI operating system that speaks Telugu, remembers your ECE notes, browses the internet, writes and runs code, and works while you sleep. Built by a 2nd-year student at VIT-AP for exactly ₹0.**

</div>

---

## 🧠 What is Yaazhi?

Yaazhi is not a chatbot. It is a **personal AI operating system** — a stateful, multi-agent system that:

- 🔁 **Runs 24/7** on a free cloud VPS (DigitalOcean via GitHub Student Pack)
- 🗣️ **Speaks Telugu, Hindi, English** natively via Bhashini government API
- 🧠 **Remembers everything** — your B.Tech notes, past conversations, IEEE papers, projects
- 🌐 **Browses the internet** autonomously, fills forms, extracts data
- 💻 **Writes and runs code** in a sandboxed loop until it works
- 📲 **Sends WhatsApp/email alerts** when tasks complete
- 💰 **Costs exactly ₹0** using student benefits

> *ChatGPT answers questions. Yaazhi takes actions, remembers your life, and works while you sleep.*

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     YOU (Santhosh)                              │
│              Voice / Chat / WhatsApp / Dashboard                │
└─────────────────────┬──────────────────────────────────────────┘
                      │
┌─────────────────────▼──────────────────────────────────────────┐
│                  BLOCK 1 — BRAIN                                │
│         LangGraph Orchestrator (core/orchestrator.py)           │
│  Planner → Router → [Agents in parallel] → Reviewer → Finalize │
└────┬──────────┬─────────┬──────────┬────────────┬──────────────┘
     │          │         │          │            │
┌────▼──┐ ┌────▼──┐ ┌────▼──┐ ┌────▼──┐   ┌────▼──────────┐
│Groq   │ │Claude │ │Gemini │ │Ollama │   │BLOCK 2 ACTIONS│
│Fast   │ │Coder  │ │Reader │ │Local  │   │Browser/Notify │
│Search │ │Agent  │ │Agent  │ │Agent  │   │n8n Workflows  │
└────┬──┘ └────┬──┘ └────┬──┘ └────┬──┘   └──────┬────────┘
     └──────────┴─────────┴──────────┴────────────┘
                          │
┌─────────────────────────▼──────────────────────────────────────┐
│                  BLOCK 3 — MEMORY                               │
│   ChromaDB (primary) ←→ pgvector (fallback) ←→ Redis (cache)   │
│   Your B.Tech Notes · IEEE Papers · Conversation History        │
└─────────────────────────┬──────────────────────────────────────┘
                          │
┌─────────────────────────▼──────────────────────────────────────┐
│                  BLOCK 4 — INTERFACE                            │
│   FastAPI (port 8000) · Streamlit Dashboard (port 8501)         │
│   Whisper STT · Coqui/Bhashini TTS · OpenWakeWord              │
│   Prometheus · Grafana · LangSmith · Uptime Kuma               │
└─────────────────────────────────────────────────────────────────┘
```

---

## ⚡ Capabilities vs Movie Jarvis

| Capability | Movie Jarvis | Yaazhi |
|:-----------|:------------:|:-------:|
| Voice conversation | ✅ 95% | ✅ 85% |
| Web browsing & research | ✅ 90% | ✅ 95% |
| Write & run code | ✅ 100% | ✅ 95% |
| Read entire textbooks | ✅ 90% | ✅ **98%** (Gemini 2M tokens) |
| Persistent memory | ✅ 100% | ✅ 90% |
| Multi-agent teamwork | ✅ 80% | ✅ **95%** (4+ parallel agents) |
| Telugu/Hindi support | ❌ 0% | ✅ **95%** (Bhashini) |
| IoT hardware control | ✅ 100% | ⚡ 50% (ESP32 coming) |
| **Cost** | Fictional | **₹0 real** |
| Holographic projection | ✅ | ❌ (hardware doesn't exist) |

---

## 🚀 Quick Start (5 Steps)

### Prerequisites
- Ubuntu 22.04 VPS (DigitalOcean via GitHub Student Pack — free)
- GitHub Student Pack activated (education.github.com/pack)

```bash
# Step 1: Clone the repository
git clone https://github.com/santhosh-vitap/yaazhi.git
cd yaazhi

# Step 2: Fill in your API keys
cp config/.env.example config/.env
nano config/.env   # Fill in at minimum: GROQ_API_KEY, POSTGRES_URL

# Step 3: Run the automated VPS setup script (Ubuntu 22.04 only)
chmod +x scripts/setup.sh
bash scripts/setup.sh

# Step 4: Add your B.Tech notes to the knowledge base
cp /path/to/your/notes/*.pdf knowledge/btech_notes/
python scripts/ingest_docs.py --folder knowledge/btech_notes/

# Step 5: Start talking to Yaazhi
# Dashboard: http://YOUR_VPS_IP:8501
# API: http://YOUR_VPS_IP:8000/health
```

---

## 🎓 Student Benefits Used

| Service | What Yaazhi uses it for | Value |
|:--------|:------------------------|------:|
| **GitHub Copilot** (VIT email) | Writes all Yaazhi code | ₹8,000/yr |
| **DigitalOcean $200** (via Pack) | Primary VPS for 12 months | ₹16,000 |
| **Azure $100 credit** (VIT email) | GPT-4o for final review | ₹8,000 |
| **JetBrains IDEs** (via Pack) | PyCharm development | ₹12,000/yr |
| **Groq API** (personal Gmail) | World's fastest free LLM inference | Free |
| **Google Cloud Free Tier** | Secondary VPS + Vertex AI | Permanent |
| **Bhashini API** (VIT email) | Telugu/Hindi voice I/O | Free (Indian student) |
| **Cloudflare Free** (personal Gmail) | Secure tunnel + DNS | Permanent |
| **Supabase Free** (personal Gmail) | PostgreSQL + vector DB | Free |
| | **Total annual value** | **~₹50,000** |

---

## 📁 Project Structure

```
yaazhi/
├── core/               # Brain — LangGraph orchestrator
│   ├── orchestrator.py # Master LangGraph control loop
│   ├── planner.py      # Task decomposition (GPT-4o)
│   ├── reviewer.py     # Quality gating (Groq fast review)
│   ├── state.py        # All TypedDict and Pydantic models
│   └── guardrails.py   # Input/output safety validation
├── agents/             # Agent swarm
│   ├── researcher.py   # DuckDuckGo + Groq fast research
│   ├── coder.py        # Claude code writer + sandbox executor
│   ├── reader.py       # Gemini 2M context PDF/URL reader
│   ├── browser.py      # Playwright autonomous web browsing
│   └── notifier.py     # WhatsApp / Email / Telegram alerts
├── memory/             # The Vault
│   ├── vector_store.py # ChromaDB primary, pgvector fallback
│   ├── episodic.py     # Conversation threads (Mem0 + Redis)
│   ├── ingestion.py    # PDF/DOCX/PPTX document ingestion
│   ├── retriever.py    # Semantic + BM25 hybrid search
│   └── indexer.py      # Watchdog auto-ingest on file drop
├── voice/              # The Senses
│   ├── stt.py          # Whisper speech-to-text
│   ├── tts.py          # Coqui XTTS + Bhashini TTS
│   ├── bhashini.py     # Bhashini government API client
│   └── wakeword.py     # "Hey Yaazhi" always-on listener
├── api/                # FastAPI backbone
│   ├── main.py         # App entry point + lifespan
│   ├── middleware.py   # Auth, rate limit, logging, language detect
│   └── routes/         # chat.py · memory.py · voice.py
├── dashboard/          # Streamlit UI
│   ├── app.py          # 5-page Streamlit dashboard
│   └── components/     # chat_ui.py
├── workflows/          # n8n automations (import directly into n8n)
├── knowledge/          # Drop your PDFs here — auto-ingested
├── infra/              # Docker + Nginx + PostgreSQL init
├── config/             # .env.example · settings.py · models.yaml
├── scripts/            # setup.sh · health_check.py · migrate.py
└── requirements.txt
```

---

## 💰 Monetization Potential

Once Yaazhi is running, you have a portfolio to monetize:

| Revenue Stream | Realistic Income |
|:---------------|:----------------|
| Custom WhatsApp bots for local businesses | ₹5k–30k per bot |
| n8n automation retainers | ₹8k–25k/month |
| AI agent development (Upwork/Fiverr) | ₹1.2L–4L per project |
| Telugu/Hindi AI tools (blue ocean) | ₹5k–50k/month SaaS |
| Teaching: "Build your Jarvis" (YouTube/Gumroad) | ₹20k–2L per launch |
| Hackathons (SIH, MLH, IEEE) | ₹50k–5L prizes |

---

## 🗺️ Roadmap

**v1.0 — Core System (Now)**
- [x] LangGraph multi-agent orchestration
- [x] ChromaDB + pgvector dual memory
- [x] Whisper STT + Coqui/Bhashini TTS
- [x] FastAPI backend + Streamlit dashboard
- [x] n8n notification workflows
- [x] Docker + Cloudflare tunnel

**v2.0 — Enhancement**
- [ ] Custom "Hey Yaazhi" wake word (your voice)
- [ ] ESP32 IoT sensor integration via MQTT
- [ ] Mobile app (React Native)
- [ ] AutoGen multi-agent debate for complex decisions
- [ ] Qdrant upgrade for faster vector search

**v3.0 — Scale**
- [ ] Multi-user support for VIT-AP ECE batch
- [ ] Curriculum-aware study assistant
- [ ] IEEE paper research assistant
- [ ] Lab experiment automation

---

## 🤝 Contributing

This is a personal project but contributions are welcome!

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Commit your changes: `git commit -m 'Add some feature'`
4. Push: `git push origin feature/your-feature`
5. Open a Pull Request

Please follow the existing code style (ruff + mypy strict).

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

<div align="center">

**Built by Santhosh · VIT-AP University · ECE · 2nd Year · 2026**

*"The only thing movie Jarvis does that Yaazhi cannot is holographic projection."*

⭐ **Star this repo if Yaazhi inspires you to build your own!**

</div>
