#!/usr/bin/env python3
"""
Enterprise-grade Pi-hole Gravity DB Blocklist Extractor.
Features: Atomic writes, mutex locking, database retry logic, and idempotency.
"""

import fcntl
import logging
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Tuple

# Configuration Constants (Supports Environment Variable Overrides)
SOURCES_FILENAME = os.getenv("EXTRACTOR_SOURCES_FILE", "sources.txt")
DB_PATH = os.getenv(
    "EXTRACTOR_DB_PATH",
    "/mnt/dietpi_userdata/docker/primary-stack/pihole/etc-pihole/gravity.db",
)
OUTPUT_DIR_NAME = os.getenv("EXTRACTOR_OUTPUT_DIR", "maintainers")
LOCK_FILE = "/tmp/pihole_extractor.lock"

# Initialize standard logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(process)d] - %(module)s.%(funcName)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
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
        logger.error(
            "Another instance is currently running. Exiting to prevent race conditions."
        )
        sys.exit(0)
    except OSError as err:
        logger.error("Failed to acquire lock at %s: %s", lock_path, err)
        sys.exit(1)


def verify_file_exists(file_path: Path, file_description: str) -> None:
    """Verifies the existence of a critical file before proceeding."""
    if not file_path.is_file():
        logger.error(
            "Critical file missing: %s not found at %s", file_description, file_path
        )
        sys.exit(1)


def ensure_output_directory(dir_path: Path) -> None:
    """Ensures the output directory exists with strict permissions."""
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
        dir_path.chmod(0o755)
    except OSError as err:
        logger.error("Failed to create output directory %s: %s", dir_path, err)
        sys.exit(1)


def parse_sources_file(sources_path: Path) -> Dict[str, str]:
    """Parses the sources.txt file into a dictionary mapping maintainer names to URLs."""
    sources: Dict[str, str] = {}
    try:
        with sources_path.open("r", encoding="utf-8") as file_obj:
            for line_number, line in enumerate(file_obj, start=1):
                line = line.strip()
                if not line or line.startswith("#") or "-" not in line:
                    continue

                parts = line.split("-", 1)
                if len(parts) == 2:
                    name = parts[0].strip()
                    url = parts[1].strip()
                    sources[name] = url
                else:
                    logger.warning(
                        "Malformed entry in sources.txt at line %d. Skipping.",
                        line_number,
                    )
        return sources
    except IOError as err:
        logger.error("Failed to read sources file %s: %s", sources_path, err)
        sys.exit(1)


def fetch_blocklists_from_db(
    db_path: Path, max_retries: int = 5
) -> List[Tuple[str, str]]:
    """Connects to the SQLite database with exponential backoff to handle temporary locks."""
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

        except sqlite3.OperationalError as err:
            if "locked" in str(err).lower() and attempt < max_retries:
                sleep_time = 2**attempt
                logger.warning(
                    "Database locked. Retrying in %d seconds (Attempt %d/%d)...",
                    sleep_time,
                    attempt,
                    max_retries,
                )
                time.sleep(sleep_time)
            else:
                logger.error("Database operational error on %s: %s", db_path, err)
                sys.exit(1)
        except sqlite3.Error as err:
            logger.error("Fatal database error: %s", err)
            sys.exit(1)
        finally:
            if "conn" in locals():
                conn.close()

    return adlists


def categorize_blocklists(
    adlists: List[Tuple[str, str]], sources: Dict[str, str]
) -> Dict[str, List[str]]:
    """Cross-references database comments with sources to categorize URLs by maintainer."""
    categorized: Dict[str, List[str]] = {maintainer: [] for maintainer in sources}

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
            with file_path.open("r", encoding="utf-8") as file_obj:
                if file_obj.read() == content:
                    return False
        except IOError:
            pass  # Proceed to overwrite if read fails

    # Atomic write: Write to temp file, then rename (POSIX guarantees atomic rename)
    file_descriptor, tmp_path = tempfile.mkstemp(dir=file_path.parent, text=True)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_obj:
            file_obj.write(content)
        os.replace(tmp_path, file_path)
        file_path.chmod(0o644)
        return True
    except OSError as err:
        logger.error("Failed atomic write to %s: %s", file_path, err)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_markdown_files(
    categorized_data: Dict[str, List[str]], sources: Dict[str, str], output_dir: Path
) -> None:
    """Generates Markdown files iteratively for each categorized maintainer."""
    updated_count = 0
    skipped_count = 0

    for maintainer, urls in categorized_data.items():
        if not urls:
            continue

        maintainer_url = sources.get(maintainer, "#")

        # Break up string operations to respect the 100-character line limit
        valid_chars = [c for c in maintainer if c.isalnum() or c == " "]
        safe_filename = "".join(valid_chars).rstrip().replace(" ", "_") + ".md"
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
        except OSError:
            # Error already logged in atomic_write_markdown, avoiding broad Exception catch
            continue

    logger.info(
        "Disk Operations: %d files updated, %d files skipped (unchanged).",
        updated_count,
        skipped_count,
    )


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
        logger.info("Protocol completed successfully in %.3f seconds.", elapsed_time)

    finally:
        # Ensure lock is released even if the script crashes
        os.close(lock_fd)
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
