Market analysis — Foundation models & agents (Apr 2026)

Opportunities
- Vertical RAG agents: domain-specific ingest → vector DB → QA with citations (legal, infra, clinical). Fast to demo, high enterprise interest.
- Tool-enabled agent orchestration: planner→executor with sandboxed tools and safety/policy hooks.
- Hybrid storage: Postgres for metadata/transactions + vector DB for embeddings for consistency and audit.
- Efficient on-device LLMs and quantized inference: latency and cost wins for edge deployments.
- Observability & provenance: hallucination tracing, cost telemetry, and provenance to build trust.

Recommended projects
1) Vertical RAG agent POC — ingest pipeline, vector store, QA UI with citations, evaluation scripts, and demo.
2) Sandboxed tool-executor API — secure tool registry, policy hooks, tests, and a small integration (web fetch + runner).
3) Quantized 7B deploy — quantize, serve with simple API, produce latency/cost benchmarks and ci-driven reproducibility.

12-week roadmap (high level)
- Month 1: repo & env, ingestion + vector DB demo, basic RAG QA with citations, evaluation.
- Month 2: planner→executor agent, sandboxed tool integration, safety tests, LoRA/PEFT experiments.
- Month 3: quantization/compression, containerized deploy & CI, telemetry/provenance, writeup and demo video.

Stop criteria
- Core supervisor, agent registry, memory, sandboxed tool-executor, and minimal UI shipped. Treat other features as plugins.

How to show work
- Public repo with reproducible demos, benchmark report, blog walkthrough, and a short demo video.

Contact/Next steps
- Assign POC owner, pick stack (Hugging Face + LangChain + FAISS recommended), and schedule first sprint.
