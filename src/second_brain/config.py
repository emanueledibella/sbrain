from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SB_", env_file=".env", extra="ignore", populate_by_name=True
    )
    vault_path: Path = Path("vault")
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = Field(default=SecretStr(""), validation_alias="NEO4J_PASSWORD")
    neo4j_database: str = "neo4j"
    ollama_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen2.5:1.5b"
    embedding_model: str = "embeddinggemma"
    embedding_dimensions: int = Field(default=768, ge=1, le=8192)
    model_timeout: float = Field(default=180, gt=0, le=600)
    model_threads: int = Field(default=4, ge=1, le=128)
    model_context: int = Field(default=16384, ge=2048, le=131072)
    model_output_tokens: int = Field(default=2048, ge=256, le=16384)
    max_source_chars: int = Field(default=24000, ge=100, le=200000)
    mcp_read_token: SecretStr = SecretStr("")
    mcp_write_token: SecretStr = SecretStr("")
    mcp_requests_per_minute: int = Field(default=300, ge=1, le=10000)
    mcp_max_inflight: int = Field(default=8, ge=1, le=128)
    mcp_max_body_bytes: int = Field(default=2097152, ge=1024, le=16777216)
    semantic_max_claims: int = Field(default=250, ge=1, le=2000)
    semantic_max_triples: int = Field(default=100000, ge=1000, le=1000000)
    vocabulary_extension_path: Path | None = None
    worker_queue_path: Path | None = None
    worker_poll_seconds: float = Field(default=2, ge=0.1, le=60)
    worker_reconcile_seconds: float = Field(default=15, ge=1, le=3600)
    worker_lease_seconds: float = Field(default=60, ge=10, le=3600)
    worker_max_attempts: int = Field(default=5, ge=1, le=100)
    worker_retry_base_seconds: float = Field(default=30, ge=0.1, le=3600)
    worker_retry_max_seconds: float = Field(default=3600, ge=1, le=86400)
