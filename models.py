# ==============================================================================
# HB CLUSTER MANAGER - DATABASE MODELS (models.py v2.11.4K)
# ==============================================================================

from sqlalchemy import Column, Integer, BigInteger, String, Float, DateTime, Boolean, Index
from database import Base
import datetime
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.types import JSON


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_priority", "status", "priority", "created_at", "id"),
        Index("ix_jobs_status_completed", "status", "completed_at"),
        Index("ix_jobs_status_worker", "status", "worker_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    job_hash = Column(String, unique=True, index=True, nullable=True)
    original_filename = Column(String, index=True)
    source_path = Column(String)
    dest_path = Column(String)

    hb_json_payload = Column(MutableDict.as_mutable(JSON))
    hb_creation_time = Column(String, nullable=True)

    # WAITING, STAGING, PREMUX, IN_PROGRESS, COPYING, WAITING_DISK, PAUSED, COMPLETED, ERROR, CANCELLED
    status = Column(String, default="WAITING", index=True)
    is_paused = Column(Boolean, default=False)
    progress = Column(Float, default=0.0)

    worker_name = Column(String, nullable=True, index=True)
    worker_version = Column(String, nullable=True)
    priority = Column(Integer, default=10, index=True)
    attempt_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # === Čisté číselné metriky průběhu převodu ===
    fps_current = Column(Float, default=0.0)
    fps_avg = Column(Float, default=0.0)
    speed_mbps = Column(Float, default=0.0)
    elapsed_sec = Column(Integer, default=0)
    eta_sec = Column(Integer, default=0)
    transferred_bytes = Column(BigInteger, default=0)
    is_net_fallback = Column(Boolean, default=False)
    net_fallback_reason = Column(String, nullable=True)

    # === Čisté číselné výsledky převodu (velikost a čas) ===
    src_size_bytes = Column(BigInteger, default=0)
    out_size_bytes = Column(BigInteger, default=0)
    duration_sec = Column(Integer, default=0)
    completed_at = Column(DateTime, nullable=True, index=True)

    # === Bitrate telemetrie (v kb/s) a pokročilý post-processing ===
    v_bitrate_in_kbps = Column(Integer, default=0)
    a_bitrate_in_kbps = Column(Integer, default=0)
    v_bitrate_out_kbps = Column(Integer, default=0)
    a_bitrate_out_kbps = Column(Integer, default=0)
    mkv_stats_written = Column(Boolean, default=False)
    lpcm_Tracks_muxed = Column(Integer, default=0)
    premux_applied = Column(Boolean, default=False)
    premux_status = Column(String, default="")        # OK, FALLBACK, OFF, TRYING
    disk_hold_reason = Column(String, nullable=True)  # Detail při pozdržení úlohy (WAITING_DISK)

    # === Strukturované stavy titulků a chyb ===
    subs_removed = Column(Integer, default=0)
    subs_status = Column(String, nullable=True)       # OK, CLEANED, ERROR, ANALYZE_ERR
    error_code = Column(String, nullable=True)        # ERR_SOURCE_MISSING, ERR_NAS_UNREACHABLE, ERR_NETWORK_LOST, ERR_HB_FAILED, ERR_ZOMBIE_TIMEOUT, ERR_LOST_CONNECTION
    error_detail = Column(String, nullable=True)

    # === Předpočítané parametry videa a stop (umožňují defer(hb_json_payload)) ===
    res_in = Column(String, nullable=True)
    res_out = Column(String, nullable=True)
    video_duration_sec = Column(Float, default=0.0)
    preset_name = Column(String, nullable=True, index=True)
    encoder_summary = Column(String, nullable=True)

    video_encoder = Column(String, nullable=True)
    video_preset = Column(String, nullable=True)
    video_rf = Column(Float, nullable=True)
    audio_count = Column(Integer, default=0)
    subs_count = Column(Integer, default=0)
    dup_key = Column(String, nullable=True, index=True)
    is_disc_source = Column(Boolean, default=False)
    burned_subs = Column(Integer, default=-1)


# Alias pro sjednocený přístup k LPCM atributu
if hasattr(Job, "lpcm_Tracks_muxed") and not hasattr(Job, "lpcm_tracks_muxed"):
    Job.lpcm_tracks_muxed = Job.lpcm_Tracks_muxed


class WorkerPolicy(Base):
    __tablename__ = "worker_policies"

    worker_name = Column(String, primary_key=True, index=True)
    max_slots = Column(Integer, default=4)
    capability = Column(String, default="all")
    allow_4k = Column(Boolean, default=True)
    prioritize_4k = Column(Boolean, default=False)
    is_enabled = Column(Boolean, default=True)
    use_local_temp = Column(Boolean, default=True)
    staging_mode = Column(String, default="Všechny formáty")
    power_mode = Column(String, default="AUTO")
    custom_cores = Column(String, default="")


class EncodingStats(Base):
    __tablename__ = "encoding_stats"

    id = Column(Integer, primary_key=True, index=True)
    worker_name = Column(String, index=True)
    res_in = Column(String, index=True)
    res_out = Column(String, index=True)
    video_encoder = Column(String, index=True)
    video_preset = Column(String, index=True)
    video_rf = Column(Float, index=True, nullable=True)

    jobs_count = Column(Integer, default=0)
    total_src_bytes = Column(BigInteger, default=0)
    total_out_bytes = Column(BigInteger, default=0)
    total_encoding_sec = Column(Integer, default=0)
    total_video_duration_sec = Column(Float, default=0.0)
    avg_fps = Column(Float, default=0.0)
    avg_v_bitrate_in_kbps = Column(Integer, default=0)
    avg_v_bitrate_out_kbps = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)