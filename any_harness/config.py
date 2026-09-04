import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


WORKING_ROOT = Path(os.environ.get("WORKING_ROOT", Path.cwd()))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )

