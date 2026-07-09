import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    url: str
    email: str
    password: str
    ssl_skip_verify: bool


def load_config() -> Config:
    return Config(
        url=os.environ["SO_URL"].rstrip("/"),
        email=os.environ["SO_EMAIL"],
        password=os.environ["SO_PASSWORD"],
        ssl_skip_verify=os.environ.get("SO_SSL_SKIP_VERIFY", "false").lower() == "true",
    )
