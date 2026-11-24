"""Load settings.toml into a Settings object."""
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.1"


@dataclass(frozen=True)
class Settings:
    contact: str
    work_dir: Path
    media_root: Path
    store_subdir: str
    pairs: list[tuple[str, str]]
    reserve_gb: int
    commons_max_depth: int
    api_interval: float
    download_interval: float
    max_file_bytes: int
    nas_share: str | None = None
    nas_dir: str = "illusions"
    nas_credentials: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.work_dir / "illusions.db"

    @property
    def tmp_dir(self) -> Path:
        return self.work_dir / "tmp"

    @property
    def log_dir(self) -> Path:
        return self.work_dir / "logs"

    @property
    def user_agent(self) -> str:
        import requests
        contact = self.contact or "no contact configured"
        return (f"claude-illusions-bot/{VERSION} (optical illusion research crawler; {contact}) "
                f"python-requests/{requests.__version__}")


def load(path: Path = PROJECT_ROOT / "settings.toml") -> Settings:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    paths, storage, commons = raw["paths"], raw["storage"], raw["commons"]
    return Settings(
        contact=raw.get("contact", "").strip(),
        work_dir=Path(paths["work_dir"]),
        media_root=Path(paths["media_root"]),
        store_subdir=paths["store_subdir"],
        pairs=[tuple(p) for p in storage["pairs"]],
        reserve_gb=storage["reserve_gb"],
        commons_max_depth=commons["max_depth"],
        api_interval=commons["api_interval_sec"],
        download_interval=commons["download_interval_sec"],
        max_file_bytes=commons["max_file_mb"] * 1024 * 1024,
        nas_share=raw.get("nas", {}).get("share") if raw.get("nas", {}).get("enabled") else None,
        nas_dir=raw.get("nas", {}).get("dir", "illusions"),
        nas_credentials=Path(raw["nas"]["credentials"]).expanduser() if raw.get("nas", {}).get("credentials") else None,
    )
