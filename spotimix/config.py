"""Loading and validating config.yaml."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PLAYLIST_URL_RE = re.compile(r"^https://open\.spotify\.com/(?:intl-[a-z-]+/)?playlist/([A-Za-z0-9]{22})")


class ConfigError(Exception):
    """config.yaml is missing, malformed, or still has placeholder values."""


@dataclass
class DiscoveryConfig:
    block_playlist_writes: bool = True
    capture_hosts: list[str] = field(default_factory=lambda: ["spotify.com"])
    max_body_bytes: int = 5_000_000
    keywords: list[str] = field(default_factory=list)


@dataclass
class Config:
    path: Path
    playlist_url: str
    playlist_id: str
    browser_profile: Path
    output_dir: Path
    browser_channel: str | None
    slow_mo_ms: int
    discovery: DiscoveryConfig


def _resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path).resolve()
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e

    url = str(raw.get("playlist_url") or "").strip()
    m = PLAYLIST_URL_RE.match(url)
    if not m:
        raise ConfigError(
            f"playlist_url in {path} must look like "
            f"https://open.spotify.com/playlist/<22-char id>, got: {url!r}"
        )

    base = path.parent
    paths = raw.get("paths") or {}
    browser = raw.get("browser") or {}
    disc = raw.get("discovery") or {}

    keywords = [str(k).strip() for k in (disc.get("keywords") or []) if str(k).strip()]
    if not keywords:
        raise ConfigError("discovery.keywords must list at least one keyword")

    return Config(
        path=path,
        playlist_url=url,
        playlist_id=m.group(1),
        browser_profile=_resolve(base, paths.get("browser_profile", ".browser-profile")),
        output_dir=_resolve(base, paths.get("output_dir", "output")),
        browser_channel=browser.get("channel") or None,
        slow_mo_ms=int(browser.get("slow_mo_ms", 250)),
        discovery=DiscoveryConfig(
            block_playlist_writes=bool(disc.get("block_playlist_writes", True)),
            capture_hosts=[str(h).lower() for h in (disc.get("capture_hosts") or ["spotify.com"])],
            max_body_bytes=int(disc.get("max_body_bytes", 5_000_000)),
            keywords=keywords,
        ),
    )
