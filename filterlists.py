#!/usr/bin/env python3
"""
Enterprise-grade Pi-hole Gravity DB Blocklist Extractor.
Features: Atomic writes, mutex locking, database retry logic, and idempotency.
"""

import os
import sys
import time
import fcntl
import sqlite3
import logging
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

# Configuration Constants (Supports Environment Variable Overrides)
SOURCES_FILENAME = os.getenv("EXTRACTOR_SOURCES_FILE", "sources.txt")
DB_PATH = os.getenv("EXTRACTOR_DB_PATH", "/mnt/dietpi_userdata/docker/primary-stack/pihole/etc-pihole/gravity.db")
OUTPUT_DIR_NAME = os.getenv("EXTRACTOR_OUTPUT_DIR", "maintainer_blocklists")
LOCK_FILE = "/tmp/pihole_extractor.lock"

# Initialize standard logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(process)d] - %(module)s.%(funcName)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def acquire_mutex_lock(lock_path: str) -> int:
    """Acquires an exclusive, non-blocking file lock to prevent concurrent executions."""
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.debug("Mutex lock acquired successfully.")
        return lock_fd
    except BlockingIOError:
        logger.error("Another instance is currently running. Exiting to prevent race conditions.")
        sys.exit(0)
    except OSError as e:
        logger.error(f"Failed to acquire lock at {lock_path}: {e}")
        sys.exit(1)


def verify_file_exists(file_path: Path, file_description: str) -> None:
    """Verifies the existence of a critical file before proceeding."""
    if not file_path.is_file():
        logger.error(f"Critical file missing: {file_description} not found at {file_path}")
        sys.exit(1)


def ensure_output_directory(dir_path: Path) -> None:
    """Ensures the output directory exists with strict permissions."""
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
        dir_path.chmod(0o755)
    except OSError as e:
        logger.error(f"Failed to create output directory {dir_path}: {e}")
        sys.exit(1)


def parse_sources_file(sources_path: Path) -> Dict[str, str]:
    """Parses the sources.txt file into a dictionary mapping maintainer names to URLs."""
    sources: Dict[str, str] = {}
    try:
        with sources_path.open('r', encoding='utf-8') as f:
            for line_number, line in enumerate(f, start=1):
                line = line.strip()
                if not line or line.startswith('#') or '-' not in line:
                    continue
                
                parts = line.split('-', 1)
                if len(parts) == 2:
                    name = parts[0].strip()
                    url = parts[1].strip()
                    sources[name] = url
                else:
                    logger.warning(f"Malformed entry in sources.txt at line {line_number}. Skipping.")
        return sources
    except IOError as e:
        logger.error(f"Failed to read sources file {sources_path}: {e}")
        sys.exit(1)


def fetch_blocklists_from_db(db_path: Path, max_retries: int = 5) -> List[Tuple[str, str]]:
    """
    Connects to the SQLite database with exponential backoff to handle temporary locks.
    """
    adlists: List[Tuple[str, str]] = []
    
    for attempt in range(1, max_retries + 1):
        try:
            # uri=True and mode=ro ensures read-only mode. timeout=10 waits for internal locks.
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
            cursor = conn.cursor()
            
            cursor.execute("SELECT address, comment FROM adlist WHERE enabled = 1;")
            rows = cursor.fetchall()
            
            for row in rows:
                address = str(row[0]).strip() if row[0] else ""
                comment = str(row[1]).strip() if row[1] else ""
                if address:
                    adlists.append((address, comment))
            return adlists
            
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_retries:
                sleep_time = 2 ** attempt
                logger.warning(f"Database locked. Retrying in {sleep_time} seconds (Attempt {attempt}/{max_retries})...")
                time.sleep(sleep_time)
            else:
                logger.error(f"Database operational error on {db_path}: {e}")
                sys.exit(1)
        except sqlite3.Error as e:
            logger.error(f"Fatal database error: {e}")
            sys.exit(1)
        finally:
            if 'conn' in locals():
                conn.close()
                
    return adlists


def categorize_blocklists(adlists: List[Tuple[str, str]], sources: Dict[str, str]) -> Dict[str, List[str]]:
    """Cross-references database comments with sources to categorize URLs by maintainer."""
    categorized: Dict[str, List[str]] = {maintainer: [] for maintainer in sources.keys()}
    
    for url, comment in adlists:
        if comment in sources:
            categorized[comment].append(url)
            
    return categorized


def atomic_write_markdown(file_path: Path, content: str) -> bool:
    """
    Writes data atomically and idempotently. 
    Returns True if a write occurred, False if the file already matches the content.
    """
    # Idempotency check: Skip write if content is unchanged
    if file_path.exists():
        try:
            with file_path.open('r', encoding='utf-8') as f:
                if f.read() == content:
                    return False
        except IOError:
            pass # Proceed to overwrite if read fails

    # Atomic write: Write to temp file, then rename (POSIX guarantees atomic rename)
    fd, tmp_path = tempfile.mkstemp(dir=file_path.parent, text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, file_path)
        file_path.chmod(0o644)
        return True
    except OSError as e:
        logger.error(f"Failed atomic write to {file_path}: {e}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_markdown_files(categorized_data: Dict[str, List[str]], sources: Dict[str, str], output_dir: Path) -> None:
    """Generates Markdown files iteratively for each categorized maintainer."""
    updated_count = 0
    skipped_count = 0
    
    for maintainer, urls in categorized_data.items():
        if not urls:
            continue
            
        maintainer_url = sources.get(maintainer, "#")
        safe_filename = "".join([c for c in maintainer if c.isalnum() or c == ' ']).rstrip()
        safe_filename = safe_filename.replace(' ', '_') + ".md"
        file_path = output_dir / safe_filename
        
        # Build strict markdown structure
        markdown_content = f"# [{maintainer}]({maintainer_url})\n\n<br>\n\n```\n"
        markdown_content += "\n".join(sorted(urls))
        markdown_content += "\n```\n"
        
        try:
            if atomic_write_markdown(file_path, markdown_content):
                updated_count += 1
            else:
                skipped_count += 1
        except Exception:
            continue # Error already logged in atomic_write_markdown
            
    logger.info(f"Disk Operations: {updated_count} files updated, {skipped_count} files skipped (unchanged).")


def main() -> None:
    """Main execution orchestrator."""
    start_time = time.perf_counter()
    
    # 0. Acquire Mutex Lock (Ensures singleton execution)
    lock_fd = acquire_mutex_lock(LOCK_FILE)
    
    try:
        # Define paths relative to execution context
        script_dir = Path(__file__).resolve().parent
        sources_path = script_dir / SOURCES_FILENAME
        db_path = Path(DB_PATH)
        output_dir = script_dir / OUTPUT_DIR_NAME

        # 1. Environment Verification
        verify_file_exists(sources_path, "Maintainer sources file")
        verify_file_exists(db_path, "Pi-hole Gravity Database")
        ensure_output_directory(output_dir)

        # 2. Data Ingestion
        sources_dict = parse_sources_file(sources_path)
        if not sources_dict:
            logger.error("No valid entries found in sources.txt. Terminating.")
            sys.exit(1)

        adlists = fetch_blocklists_from_db(db_path)
        if not adlists:
            logger.warning("No blocklists found in database. Terminating gracefully.")
            sys.exit(0)

        # 3. Data Processing & Output
        categorized_data = categorize_blocklists(adlists, sources_dict)
        write_markdown_files(categorized_data, sources_dict, output_dir)
        
        elapsed_time = time.perf_counter() - start_time
        logger.info(f"Protocol completed successfully in {elapsed_time:.3f} seconds.")
        
    finally:
        # Ensure lock is released even if the script crashes
        os.close(lock_fd)
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
