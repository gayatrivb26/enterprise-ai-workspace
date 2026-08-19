"""
Central configuration for the AI service. Everything is read from environment
variables so the same image runs unchanged across docker-compose, CI, and prod.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"

    postgres_dsn: str = "postgresql://postgres:postgres@postgres:5432/workspace"
    redis_url: str = "redis://redis:6379/0"
    chroma_host: str = "chroma"
    chroma_port: int = 8000

    # --- Chunking / retrieval -------------------------------------------
    chunk_size_tokens: int = 400
    chunk_overlap_tokens: int = 60
    retrieval_top_k: int = 10
    rerank_candidate_k: int = 20
    # Maximal-marginal-relevance tradeoff: 1.0 = pure similarity, 0.0 = pure
    # diversity. Weighted well toward relevance: at 0.65 the diversity term was
    # strong enough to push the single best-matching chunk out of the window,
    # which is worse than showing two related passages from one section.
    mmr_lambda: float = 0.82
    # How far past the closest hit a chunk may sit and still be included.
    #
    # ADDITIVE, not multiplicative. A multiple of the best distance looks
    # adaptive but scales the wrong way: when the best hit is itself mediocre
    # (say 1.15) a 1.9x band admits everything out to 2.18 — effectively the
    # whole corpus, which is how an unrelated novel ended up cited in a
    # question about taxation. A fixed margin means "close to the best answer"
    # regardless of how good the best answer happens to be.
    relevance_margin: float = 0.35

    # Retained for callers that still want an absolute ceiling.
    max_distance: float = 1.15

    # --- LLM behaviour ---------------------------------------------------
    llm_max_output_tokens: int = 1400
    llm_max_retries: int = 3
    llm_retry_base_delay: float = 0.6
    # Ceiling on any single backoff. Providers sometimes ask for a 10s+ wait;
    # honouring that literally would leave a user staring at a spinner.
    llm_max_retry_delay: float = 4.0

    # Bump when the prompt template changes: it is part of the cache key, so
    # incrementing it invalidates every previously cached answer at once.
    prompt_version: str = "v3"

    # --- Caching ---------------------------------------------------------
    cache_enabled: bool = True
    cache_ttl_seconds: int = 60 * 60 * 24 * 7  # 7 days
    cache_prefix: str = "eaw"

    # --- Cost tracking (USD per 1M tokens) -------------------------------
    price_input_per_mtok: float = 0.10
    price_output_per_mtok: float = 0.40

    # --- Ingestion -------------------------------------------------------
    max_upload_bytes: int = 50 * 1024 * 1024
    embed_batch_size: int = 64

    class Config:
        env_file = ".env"


settings = Settings()
