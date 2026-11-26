from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
import os
import sys
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SELFTEST = "--selftest" in sys.argv[1:]


# Datové složky Manageru v Dockeru: README / docker-compose.yml připojují ./data:/data, starší návody ./data:/app/data.
DOCKER_DATA_DIRS = ("/data", "/app/data")


def _mount_points() -> set:
    """Přípojné body z /proc/self/mountinfo (najde i bind mount ze stejného disku, který os.path.ismount nepozná)."""
    pts = set()
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                parts = ln.split()
                if len(parts) > 4:
                    pts.add(parts[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\"))
    except Exception:
        pass
    return pts


def _is_mount(path: str) -> bool:
    try:
        if os.path.ismount(path):
            return True
    except Exception:
        pass
    try:
        return os.path.abspath(path) in _mount_points()
    except Exception:
        return False


def _on_mounted_volume(path: str) -> bool:
    """True, když složka nebo některá nadřazená složka (kromě /) je připojený svazek (bind mount / volume)."""
    try:
        p = os.path.abspath(path)
        while p and p != os.path.dirname(p):
            if _is_mount(p):
                return True
            p = os.path.dirname(p)
    except Exception:
        return True
    return False


def _resolve_manager_data_dir() -> str:
    env_dir = os.getenv("HB_DATA_DIR", "").strip()
    # 1) Vlastní HB_DATA_DIR (cokoli jiného než /data nebo /app/data) platí vždy – nepřebije ho /data/binaries z obrazu.
    if env_dir and os.path.normpath(env_dir) not in DOCKER_DATA_DIRS:
        return env_dir
    # 2) Připojený svazek má přednost před výchozím HB_DATA_DIR=/data z obrazu: jinak by se databáze uložila
    #    do vrstvy kontejneru a s další aktualizací (nový kontejner) by zmizela. Platí pro ./data:/data i ./data:/app/data.
    mounted = [c for c in DOCKER_DATA_DIRS if _is_mount(c)]
    if env_dir and os.path.normpath(env_dir) in mounted:
        mounted.remove(os.path.normpath(env_dir))
        mounted.insert(0, os.path.normpath(env_dir))
    if len(mounted) > 1:
        for cand in mounted:
            if os.path.exists(os.path.join(cand, "cluster.db")):
                return cand
    if mounted:
        return mounted[0]
    # 3) Žádný svazek (místní spuštění, testy, zapomenutý svazek v Dockeru): výchozí HB_DATA_DIR, jinak dřívější odhad.
    if env_dir:
        return env_dir
    for cand in ("/app/data", "/data"):
        if os.path.isdir(cand):
            try:
                if any(os.path.exists(os.path.join(cand, fn)) for fn in ("cluster.db", "manager_config.json", "binaries")):
                    return cand
            except Exception:
                pass
    for cand in ("/app/data", "/data"):
        if os.path.isdir(cand):
            return cand
    return os.path.join(BASE_DIR, "data")


DATA_DIR = _resolve_manager_data_dir()
if not SELFTEST:
    os.makedirs(DATA_DIR, exist_ok=True)
# V kontejneru bez připojené datové složky by databáze, nastavení i nástroje zmizely s další aktualizací – Manager varuje v logu.
IN_CONTAINER = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
DATA_DIR_PERSISTENT = (not IN_CONTAINER) or _on_mounted_volume(DATA_DIR)

DB_PATH = os.path.join(DATA_DIR, "cluster.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30.0},
    pool_size=20,
    max_overflow=40,
    pool_timeout=60.0,
    pool_pre_ping=True,
)

# Per-thread SQLite transaction mode: True -> BEGIN IMMEDIATE (wait up to busy_timeout instead of immediate SQLITE_BUSY).
_tx_local = threading.local()


def set_tx_immediate(enabled: bool) -> bool:
    prev = bool(getattr(_tx_local, "immediate", False))
    _tx_local.immediate = bool(enabled)
    return prev


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    # Disable pysqlite implicit BEGIN so SQLAlchemy's "begin" event controls BEGIN vs BEGIN IMMEDIATE cleanly.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA cache_size=-16000")
    cursor.close()


@event.listens_for(engine, "begin")
def _on_sqlite_begin(conn):
    if getattr(_tx_local, "immediate", False):
        conn.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        conn.exec_driver_sql("BEGIN")


def run_sqlite_pragma(pragma_sql: str):
    """Spustí PRAGMA (např. journal_mode=WAL, wal_checkpoint) mimo transakci bez změny isolation_level poolu."""
    raw_conn = engine.raw_connection()
    try:
        raw_conn.isolation_level = None
        cur = raw_conn.cursor()
        try:
            cur.execute(pragma_sql)
            cur.fetchall()
        finally:
            cur.close()
    finally:
        raw_conn.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
