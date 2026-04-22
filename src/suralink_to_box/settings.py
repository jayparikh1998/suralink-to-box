from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Suralink
    suralink_base_url: str
    suralink_token: str
    suralink_download_endpoint: str | None = None
    suralink_engagement_id: str | None = None
    suralink_engagement_name: str | None = None
    suralink_customer_name: str | None = None
    suralink_customer_custom_id: str | None = None
    suralink_client_id: str | None = None
    suralink_active_engagements_only: bool = False

    # Box
    box_client_id: str | None = None
    box_client_secret: str | None = None
    box_enterprise_id: str | None = None
    box_auth_method: str | None = None
    box_developer_token: str | None = None
    box_jwt_config_path: str | None = None
    box_target_folder_id: str | None = None
    box_target_folder_path: str | None = None
    box_as_user_id: str | None = None
    box_overwrite_existing: bool = False
    box_structure_mode: str = "just_files"
    suralink_approved_only: bool = False

    # App behavior
    log_level: str = "INFO"
    http_timeout_seconds: float = 60.0
    http_max_retries: int = 3


def get_settings() -> Settings:
    return Settings()
