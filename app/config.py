import os

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    google_api_key: str = ""

    # Admin access. If ADMIN_PASSWORD is empty (local development), everyone is treated as admin.
    admin_password: str = ""
    secret_key: str = ""  # signs the admin cookie; falls back to the admin password

    # Limits for visitors (the admin has no limits)
    visitor_max_repos: int = 3                 # repositories one visitor can keep
    visitor_max_chunks: int = 400              # size limit of a visitor's repository
    visitor_data_hours: int = 24               # visitor repositories are removed after this time
    visitor_new_repos_per_ip_per_day: int = 3  # new repositories one IP address can index per day
    visitor_new_repos_per_day: int = 10        # new repositories all visitors together can index per day
    visitor_questions_per_hour: int = 15       # questions one visitor can ask per hour
    visitor_questions_per_day: int = 300       # questions all visitors together can ask per day

    @field_validator("database_url")
    @classmethod
    def use_psycopg_driver(cls, value: str) -> str:
        """Providers such as Neon give a plain postgresql:// URL; SQLAlchemy needs the driver name."""
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix):]
        return value

    @model_validator(mode="after")
    def require_admin_password_in_production(self):
        """On Render a missing admin password would make every visitor an admin, so refuse to start."""
        if os.environ.get("RENDER") and not self.admin_password:
            raise ValueError("ADMIN_PASSWORD must be set when running on Render.")
        return self


settings = Settings()