#!/usr/bin/env python3
import asyncio
import dataclasses
import json
import os
import pathlib
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
import httpx

from datasette import hookimpl, Response, Permission
from datasette.database import Database


# Configuration dataclass
@dataclasses.dataclass
class Config:
    staging_directory: pathlib.Path
    database_directory: pathlib.Path
    enable_wal: bool


@hookimpl
def register_routes():
    return [
        (r"/-/load$", load_view),
        (r"/-/load/status/(?P<job_id>[^/]+)$", load_status_api),
    ]


@hookimpl
def skip_csrf(scope):
    return scope["path"] == "/-/load"


@hookimpl
def register_permissions():
    return [
        Permission(
            name="datasette-load",
            abbr=None,
            description="Load data into Datasette from a URL",
            takes_database=False,
            takes_resource=False,
            default=False,
        )
    ]


@hookimpl
def homepage_actions(datasette, actor):
    async def inner():
        if actor and await datasette.permission_allowed(actor, "datasette-load"):
            return [
                {
                    "href": datasette.urls.path("/-/load"),
                    "label": "Load data into Datasette from a URL",
                    "description": "Import a full SQLite database into Datasette",
                }
            ]

    return inner


def config(datasette):
    """
    Return configuration settings.
    Expects plugin config to supply:
      - staging_directory: where to temporarily download files
      - database_directory: where final databases are stored
    """
    plugin_config = datasette.plugin_config("datasette-load") or {}
    return Config(
        staging_directory=pathlib.Path(
            plugin_config.get("staging_directory") or tempfile.gettempdir()
        ),
        database_directory=pathlib.Path(
            plugin_config.get("database_directory") or "."
        ).absolute(),
        enable_wal=bool(plugin_config.get("enable_wal")),
    )


async def load_view(request, datasette):
    """
    Handles POST /-/load.
    Expected JSON body:
        {"url": "<database URL>", "name": "<database name>"}
    """
    if not await datasette.permission_allowed(request.actor, "datasette-load"):
        return Response.json(
            {"forbidden": "datasette-load permission is required"}, status=403
        )

    if request.method != "POST":
        return Response.html(
            await datasette.render_template("load_view.html", request=request)
        )

    try:
        data = json.loads(await request.post_body())
    except Exception as e:
        return Response.json({"error": f"Invalid JSON: {e}"}, status=400)

    url = data.get("url")
    name = data.get("name")
    headers = data.get("headers")
    if not url or not name:
        return Response.json(
            {"error": "Missing required parameters: url or name"}, status=400
        )

    # Create unique job id and a status record.
    job_id = str(uuid.uuid4())
    status_url = datasette.absolute_url(
        request, datasette.urls.path(f"/-/load/status/{job_id}")
    )
    job = {
        "id": job_id,
        "url": url,
        "name": name,
        "done": False,
        "error": None,
        "todo_bytes": 0,
        "done_bytes": 0,
        "status_url": status_url,
    }
    datasette._datasette_load_progress = getattr(
        datasette, "_datasette_load_progress", {}
    )
    datasette._datasette_load_progress[job_id] = job

    # Launch the background processing task.
    asyncio.create_task(load_database_task(job, datasette, headers=headers))
    await asyncio.sleep(0.2)
    return Response.json(job)


async def download_sqlite_db(
    url: str,
    name: str,
    staging_dir: pathlib.Path,
    database_dir: pathlib.Path,
    enable_wal: bool,
    progress_callback,
    complete_callback,
    headers=None,
):
    """
    Downloads an SQLite DB from the given URL into a temporary file in the staging directory.
    The progress_callback is called as data arrives.
    After download, the temporary file is verified with PRAGMA integrity_check.
        • If the check fails, the temp file is deleted.
        • If the check succeeds, the file is moved to database_dir/{name}.db.
    The complete_callback is invoked with any error (or None if successful).
    Optional headers can be supplied for the HTTP request.
    """
    # Ensure the staging directory and final directory exist.
    staging_dir.mkdir(parents=True, exist_ok=True)
    database_dir.mkdir(parents=True, exist_ok=True)

    # Create a temporary file in the staging directory.
    temp_filename = f"{name}-{uuid.uuid4().hex}.temp.db"
    temp_file_path = staging_dir / temp_filename
    error = None

    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                total_bytes = (
                    int(content_length)
                    if content_length and content_length.isdigit()
                    else 0
                )
                bytes_so_far = 0

                with open(temp_file_path, "wb") as f:
                    async for chunk in response.aiter_bytes(8192):
                        f.write(chunk)
                        bytes_so_far += len(chunk)
                        await progress_callback(bytes_so_far, total_bytes)

        # Check if file is a zip file
        if zipfile.is_zipfile(temp_file_path):
            try:
                with zipfile.ZipFile(temp_file_path) as zf:
                    zip_size = os.path.getsize(temp_file_path)
                    # Find largest file in the zip
                    largest_file = max(zf.infolist(), key=lambda x: x.file_size)
                    if largest_file.file_size > zip_size * 5:
                        raise Exception(
                            "Extracted file would be more than 5x the size of the zip file"
                        )

                    # Extract largest file to a new temporary path
                    extracted_path = (
                        staging_dir / f"{name}-{uuid.uuid4().hex}.extracted.db"
                    )
                    with zf.open(largest_file.filename) as source, open(
                        extracted_path, "wb"
                    ) as target:
                        shutil.copyfileobj(source, target)

                # Remove the zip file and use extracted file for further processing
                os.remove(temp_file_path)
                temp_file_path = extracted_path
            except Exception as e:
                if temp_file_path.exists():
                    os.remove(temp_file_path)
                raise e

        # Run PRAGMA integrity_check on the file
        try:
            conn = sqlite3.connect(str(temp_file_path))
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            result = cursor.fetchone()
            cursor.close()
            conn.close()
            if not result or result[0].lower() != "ok":
                raise Exception(
                    f"Integrity check failed: {result[0] if result else 'No result returned.'}"
                )
        except Exception as integrity_error:
            error = integrity_error
            if temp_file_path.exists():
                os.remove(temp_file_path)

        # If integrity check succeeded, move file to final database directory.
        if not error:
            final_db_path = database_dir / f"{name}.db"
            if final_db_path.exists():
                os.remove(final_db_path)
            shutil.move(str(temp_file_path), str(final_db_path))
            if enable_wal:
                conn = sqlite3.connect(str(final_db_path))
                conn.execute("PRAGMA journal_mode=wal;")
                conn.close()

    except Exception as download_error:
        error = download_error
        if temp_file_path.exists():
            os.remove(temp_file_path)

    await complete_callback(name, database_dir, error)


async def load_database_task(job, datasette, headers=None):
    """
    Downloads and installs the SQLite DB as described in the job.
    Uses config(datasette) for staging and final database directories.
    Updates the job progress and, upon completion, if correct installs
    the new database into Datasette (removing any existing copy with the same name).
    """
    try:
        cfg = config(datasette)

        async def progress_callback(bytes_so_far, total_bytes):
            job["todo_bytes"] = total_bytes
            job["done_bytes"] = bytes_so_far

        async def complete_callback(name, database_directory, error):
            if error:
                job["error"] = str(error)
                job["done"] = True
                return

            try:
                final_db_path = database_directory / f"{name}.db"
                # If a database with this name is already loaded, remove it.
                if name in datasette.databases:
                    datasette.remove_database(name)
                # Add the new verified database to Datasette.
                datasette.add_database(
                    Database(datasette, path=str(final_db_path)),
                    name=name,
                )
                job["done"] = True
            except Exception as e:
                job["error"] = f"Error installing database: {str(e)}"
                job["done"] = True

        await download_sqlite_db(
            url=job["url"],
            name=job["name"],
            staging_dir=cfg.staging_directory,
            database_dir=cfg.database_directory,
            enable_wal=cfg.enable_wal,
            progress_callback=progress_callback,
            complete_callback=complete_callback,
            headers=headers,
        )
    except Exception as e:
        job["error"] = f"Error initiating download: {str(e)}"
        job["done"] = True


async def load_status_api(request, datasette):
    """
    Handles GET /-/load/status/<job_id> and returns a JSON record for the job.
    """
    job_id = request.url_vars["job_id"]
    datasette._datasette_load_progress = getattr(
        datasette, "_datasette_load_progress", {}
    )
    job = datasette._datasette_load_progress.get(job_id)
    if job is None:
        return Response.json({"error": "Job not found"}, status=404)
    return Response.json(job)
