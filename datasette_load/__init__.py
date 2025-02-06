import asyncio
from datasette.database import Database
from datasette import hookimpl, Response
import json
import uuid
import os
import pathlib
import sqlite3
import httpx


@hookimpl
def register_routes():
    return [
        (r"/-/load$", load_view),
        (r"/-/load/status/(?P<job_id>[^/]+)$", load_status_api),
    ]


@hookimpl
def skip_csrf(scope):
    return scope["path"] == "/-/load"


async def load_view(request, datasette):
    """
    Handle POST /-/load
    Expected JSON body:
        {"url": "<database URL>", "name": "<database name>"}
    """
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
    if not url or not name:
        return Response.json(
            {"error": "Missing required parameters: url or name"}, status=400
        )

    # Generate a unique job ID
    job_id = str(uuid.uuid4())

    # Build a status URL
    status_url = datasette.absolute_url(
        request, datasette.urls.path(f"/-/load/status/{job_id}")
    )

    # Create job record
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
    datasette._datasette_load_progress = (
        getattr(datasette, "_datasette_load_progress", None) or {}
    )
    datasette._datasette_load_progress[job_id] = job

    # Launch processing in background
    asyncio.create_task(process_load(job, datasette))
    await asyncio.sleep(0.2)

    return Response.json(job)


async def download_sqlite_db(
    url: str, name: str, directory_path: str, progress_callback, complete_callback
):
    """
    Downloads an SQLite DB from the specified URL to a file named {name}.db in the given directory.
    Calls progress_callback(bytes_so_far, total_bytes) periodically during the download,
    and when done performs a SQLite PRAGMA integrity_check before calling complete_callback().
    If any error occurs (during download or integrity check), it is passed to complete_callback().
    """
    file_path = os.path.join(directory_path, f"{name}.db")
    error = None

    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                total_bytes = (
                    int(content_length)
                    if content_length and content_length.isdigit()
                    else 0
                )

                bytes_so_far = 0
                with open(file_path, "wb") as f:
                    async for chunk in response.aiter_bytes(8192):
                        f.write(chunk)
                        bytes_so_far += len(chunk)
                        await progress_callback(bytes_so_far, total_bytes)

        # Perform integrity check
        try:
            conn = sqlite3.connect(file_path)
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

    except Exception as download_error:
        error = download_error

    await complete_callback(url, name, directory_path, error)


async def process_load(job, datasette):
    """
    Process the load job by downloading the SQLite database and installing it into Datasette.
    Updates job status and progress throughout the process.
    """
    try:
        # TODO: Get from plugin settings
        db_dir = str(pathlib.Path(".").absolute())

        async def progress_callback(bytes_so_far, total_bytes):
            """Update job progress"""
            job["todo_bytes"] = total_bytes
            job["done_bytes"] = bytes_so_far

        async def complete_callback(url, name, directory_path, error):
            """Handle completion of download"""
            if error:
                job["error"] = str(error)
                job["done"] = True
                return

            try:
                # Get the path to the downloaded database
                db_path = os.path.join(directory_path, f"{name}.db")

                # Remove existing database if it exists
                if name in datasette.databases:
                    datasette.remove_database(name)

                # Add the new database to Datasette
                datasette.add_database(
                    Database(
                        datasette,
                        path=db_path,
                    ),
                    name=name,
                )

                job["done"] = True

            except Exception as e:
                job["error"] = f"Error installing database: {str(e)}"
                job["done"] = True

        # Start the download
        await download_sqlite_db(
            job["url"], job["name"], db_dir, progress_callback, complete_callback
        )

    except Exception as e:
        job["error"] = f"Error initiating download: {str(e)}"
        job["done"] = True


async def load_status_api(request, datasette):
    """
    Handle GET /-/load/status/<job_id>
    Returns the JSON status record for the given job.
    """
    job_id = request.url_vars["job_id"]
    datasette._datasette_load_progress = (
        getattr(datasette, "_datasette_load_progress", None) or {}
    )
    job = datasette._datasette_load_progress.get(job_id)
    if job is None:
        return Response.json({"error": "Job not found"}, status=404)
    return Response.json(job)
