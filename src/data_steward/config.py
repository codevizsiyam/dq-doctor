from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATA_STEWARD_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    database_path: Path = Path("data/data_steward.db")
    checkpoint_path: Path = Path("data/checkpoints.db")
    contract_path: Path = Path("contracts/customer.yaml")
    breaking_schema_path: Path = Path("fixtures/customer_breaking_schema.yaml")
    approval_confidence_threshold: float = Field(default=0.85, ge=0, le=1)
    max_investigator_steps: int = Field(default=6, ge=1, le=20)
    openai_model: str = "gpt-4o-mini"
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "DATA_STEWARD_OPENAI_API_KEY"),
    )
    arize_space_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ARIZE_SPACE_ID", "DATA_STEWARD_ARIZE_SPACE_ID"),
    )
    arize_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ARIZE_API_KEY", "DATA_STEWARD_ARIZE_API_KEY"),
    )
    arize_project: str = Field(
        default="data-steward-ai",
        validation_alias=AliasChoices(
            "ARIZE_PROJECT_NAME",
            "DATA_STEWARD_ARIZE_PROJECT",
            "DATA_STEWARD_PHOENIX_PROJECT",
        ),
    )
    arize_collector_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ARIZE_COLLECTOR_ENDPOINT",
            "DATA_STEWARD_ARIZE_COLLECTOR_ENDPOINT",
        ),
    )
    phoenix_project: str = "data-steward-ai"
    phoenix_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHOENIX_COLLECTOR_ENDPOINT", "DATA_STEWARD_PHOENIX_ENDPOINT"),
    )
    phoenix_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHOENIX_API_KEY", "DATA_STEWARD_PHOENIX_API_KEY"),
    )
    phoenix_client_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHOENIX_CLIENT_URL", "DATA_STEWARD_PHOENIX_CLIENT_URL"),
    )

    @property
    def tracing_enabled(self) -> bool:
        return bool((self.arize_space_id and self.arize_api_key) or self.phoenix_api_key or self.phoenix_endpoint)

    @property
    def tracing_project(self) -> str:
        return self.arize_project or self.phoenix_project or "data-steward-ai"


@lru_cache
def get_settings() -> Settings:
    return Settings()
