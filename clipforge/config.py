"""Configuration: clipforge.yaml (cwd or $CLIPFORGE_CONFIG) with env-var overrides.

Env overrides use prefix CLIPFORGE_ and "__" as nesting delimiter, e.g.
  CLIPFORGE_WHISPER__MODEL=small  CLIPFORGE_SELECTOR__MODE=llm  CLIPFORGE_CLIPS__COUNT=3
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

CONFIG_ENV = "CLIPFORGE_CONFIG"
DEFAULT_CONFIG_FILE = "clipforge.yaml"
FONT_EXTS = (".ttf", ".otf")


def has_font_files(directory: Path) -> bool:
    """True when `directory` holds at least one .ttf/.otf file."""
    return directory.is_dir() and any(p.suffix.lower() in FONT_EXTS for p in directory.iterdir())


class WhisperCfg(BaseModel):
    model: str = "auto"  # auto -> base (cpu) / distil-large-v3 (nvidia gpu). Any faster-whisper model name.
    device: str = "auto"  # auto | cpu | cuda
    compute_type: str = "auto"  # auto -> int8 (cpu) / float16 (cuda)
    language: str | None = None  # None = autodetect
    beam_size: int = 1


class ClipsCfg(BaseModel):
    count: int = 5
    min_s: float = 20.0
    max_s: float = 58.0
    pad_s: float = 0.15  # padding added on each side of a selected segment when rendering


class SelectorCfg(BaseModel):
    mode: Literal["heuristic", "llm"] = "heuristic"
    llm_base_url: str = "http://localhost:11434/v1"  # any OpenAI-compatible endpoint (Ollama default)
    llm_model: str = "llama3.1"
    llm_api_key: str = "ollama"
    llm_timeout_s: float = 120.0
    llm_max_sentences: int = 600  # transcript compression budget sent to the LLM


class StyleCfg(BaseModel):
    """Caption preset. Colors are hex RRGGBB."""

    font: str = "Montserrat ExtraBold"  # family name as seen by libass (fontsdir=Settings.fonts_dir)
    font_size: int = 120  # in 1080x1920 pixel space (libass Fontsize; ~0.47*size cap height for Montserrat)
    uppercase: bool = True
    primary_color: str = "FFFFFF"
    accent_color: str = "FFD400"  # active (karaoke) word
    outline_color: str = "000000"
    outline: float = 6.0
    shadow: float = 3.0
    bold: bool = True
    pos_y: float = 0.62  # fraction of frame height for the caption baseline (keeps out of TikTok UI zone)
    max_words: int = 4  # words per caption group (2..4)
    hook_font_size: int = 64
    hook_color: str = "FFFFFF"
    hook_box: bool = True  # draw a dark box behind the hook card


DEFAULT_STYLES: dict[str, StyleCfg] = {
    "hormozi": StyleCfg(),
    "clean": StyleCfg(uppercase=False, font_size=100, accent_color="4CC9F0", outline=4.0, shadow=0.0),
    "minimal": StyleCfg(uppercase=False, font_size=84, accent_color="FFFFFF", primary_color="DDDDDD", outline=2.5, shadow=0.0, bold=False, hook_box=False),
}


class RenderCfg(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    preset: str = "veryfast"
    crf: int = 22
    encoder: Literal["auto", "libx264", "h264_nvenc"] = "auto"
    audio_bitrate: str = "128k"
    workers: int = 0  # 0 = min(cpu_count, 4)
    loudnorm_i: float = -14.0
    hook_seconds: float = 1.8
    progress_bar_px: int = 6
    silence_db: float = -35.0  # silencedetect threshold
    silence_min_s: float = 0.35  # gaps longer than this are removed by --tighten
    silence_keep_s: float = 0.08  # padding kept on each side of a removed gap
    punch_zoom: float = 1.08
    punch_seconds: float = 0.3


class MusicCfg(BaseModel):
    enabled: bool = False
    file: str = ""  # empty -> first file found in assets/music
    gain_db: float = -22.0


class YouTubeCfg(BaseModel):
    privacy: Literal["private", "unlisted", "public"] = "private"
    category: str = "22"  # People & Blogs
    per_day: int = 3
    upload_cost: int = 1  # quota units per videos.insert (current docs: uploads have their own bucket at 1 unit each; older docs said 1600)
    daily_quota: int = 10000  # project quota units per day (general bucket)
    uploads_per_day: int = 100  # videos.insert calls allowed per day (the separate upload bucket); verify at developers.google.com/youtube/v3/docs/videos/insert
    publish_at: str | None = None  # RFC3339, only honoured with privacy=private (scheduled publish)
    client_secret: str = "client_secret.json"
    token_file: str = "youtube_token.json"  # relative paths resolve under paths.workspace
    made_for_kids: bool = False


class TikTokCfg(BaseModel):
    privacy: str | None = None  # TikTok requires an explicit choice: one of creator_info/query privacy_level_options (e.g. SELF_ONLY)
    per_day: int = 2
    allow_comments: bool = False  # interaction settings default OFF per TikTok's guidelines; the user enables them
    allow_duet: bool = False
    allow_stitch: bool = False
    commercial_content: bool = False  # content discloses a commercial relationship (enables the toggles below)
    brand_organic: bool = False  # "Your brand": promotes the creator's own business
    branded_content: bool = False  # "Branded content": paid partnership (TikTok forces non-private privacy for it)
    music_usage_confirmed: bool = False  # you accepted TikTok's Music Usage Confirmation; required before any Direct Post
    client_key: str = ""
    client_secret: str = ""
    redirect_uri: str = ""  # any redirect URI registered on the app; paste-back flow
    token_file: str = "tiktok_token.json"
    chunk_size: int = 10_000_000  # bytes per PUT (5 MB..64 MB allowed; last chunk may be larger)


class PlatformsCfg(BaseModel):
    youtube: YouTubeCfg = Field(default_factory=YouTubeCfg)
    tiktok: TikTokCfg = Field(default_factory=TikTokCfg)


class ScheduleCfg(BaseModel):
    times: list[str] = Field(default_factory=lambda: ["09:00", "13:00", "18:00"])  # local HH:MM
    min_gap_h: float = 2.0
    tick_s: int = 60
    backoff_base_s: int = 300
    backoff_max_s: int = 6 * 3600
    max_attempts: int = 5


class DownloadCfg(BaseModel):
    max_height: int = 1080
    caption_langs: list[str] = Field(default_factory=lambda: ["en"])
    cookies_file: str = ""  # optional cookies.txt for yt-dlp


class PathsCfg(BaseModel):
    workspace: str = "workspace"
    db: str = ""  # empty -> <workspace>/clipforge.db
    logs: str = "logs"
    assets: str = ""  # empty -> <repo>/assets (fonts/ and music/ live there)


def _yaml_path() -> Path | None:
    p = Path(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_FILE))
    return p if p.is_file() else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLIPFORGE_", env_nested_delimiter="__", extra="ignore"
    )

    whisper: WhisperCfg = Field(default_factory=WhisperCfg)
    clips: ClipsCfg = Field(default_factory=ClipsCfg)
    style: str = "hormozi"
    layout: Literal["crop", "blur"] = "crop"
    tighten: bool = False
    smart: bool = False
    punch: bool = False
    music: MusicCfg = Field(default_factory=MusicCfg)
    selector: SelectorCfg = Field(default_factory=SelectorCfg)
    platforms: PlatformsCfg = Field(default_factory=PlatformsCfg)
    schedule: ScheduleCfg = Field(default_factory=ScheduleCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    render: RenderCfg = Field(default_factory=RenderCfg)
    download: DownloadCfg = Field(default_factory=DownloadCfg)
    styles: dict[str, StyleCfg] = Field(default_factory=lambda: {k: v.model_copy() for k, v in DEFAULT_STYLES.items()})

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        yp = _yaml_path()
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if yp is not None:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=yp))
        return tuple(sources)

    # ---- derived paths -------------------------------------------------
    @property
    def workspace_dir(self) -> Path:
        p = Path(self.paths.workspace)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return Path(self.paths.db) if self.paths.db else self.workspace_dir / "clipforge.db"

    @property
    def logs_dir(self) -> Path:
        return Path(self.paths.logs)

    @property
    def assets_dir(self) -> Path:
        if self.paths.assets:
            return Path(self.paths.assets)
        repo = Path(__file__).resolve().parent.parent / "assets"
        return repo if repo.is_dir() else Path("assets")

    @property
    def fonts_dir(self) -> Path:
        """<assets>/fonts: the repo's assets/ (editable install) or paths.assets. `clipforge doctor` fails when empty."""
        return self.assets_dir / "fonts"

    @property
    def music_dir(self) -> Path:
        return self.assets_dir / "music"

    def video_dir(self, video_id: str) -> Path:
        p = self.workspace_dir / video_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def style_cfg(self, name: str | None = None) -> StyleCfg:
        name = name or self.style
        merged = {k: v.model_copy() for k, v in DEFAULT_STYLES.items()}
        merged.update(self.styles)
        if name not in merged:
            raise KeyError(f"unknown style {name!r}; known: {sorted(merged)}")
        return merged[name]

    def platform_path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.workspace_dir / p


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings


def example_yaml() -> str:
    """A commented clipforge.yaml with every default, for `clipforge init-config` and the README."""
    import yaml

    data = Settings().model_dump(mode="json")
    return "# ClipForge configuration. Every key is optional; env vars CLIPFORGE_<SECTION>__<KEY> override.\n" + yaml.safe_dump(
        data, sort_keys=False
    )
