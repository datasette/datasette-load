import asyncio
from datasette import hookimpl, Response
import json
import uuid

JOBS = {}


@hookimpl
def register_routes():
    return [
        (r"/-/load$", load_api),
        (r"/-/load/status/(?P<job_id>[^/]+)$", load_status_api),
    ]


@hookimpl
def skip_csrf(scope):
    return scope["path"] == "/-/load"


async def load_api(request, datasette):
    """
    Handle POST /-/load

    Expected JSON body:
        {"url": "<database URL>", "name": "<database name>"}
    """
    if request.method != "POST":
        return Response.text("Method not allowed", status=405)

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

    # Generate a unique job ID.
    job_id = str(uuid.uuid4())

    # Build a status URL.
    status_url = datasette.absolute_url(
        request, datasette.urls.path(f"/-/load/status/{job_id}")
    )

    # Create our job record. The progress fields 'todo' and 'done_count'
    # can be updated as work is completed.
    job = {
        "id": job_id,
        "url": url,
        "name": name,
        "done": False,  # Will be set to True when processing is finished.
        "error": None,  # If an error occurs, this will be set.
        "todo": 0,  # TODO: Set to the total amount of work (e.g. file size, number of rows)
        "done_count": 0,  # TODO: Update with progress (e.g. bytes processed or rows loaded)
        "status_url": status_url,
    }
    JOBS[job_id] = job

    # Launch the processing in the background.
    asyncio.create_task(process_load(job, datasette))

    return Response.json(job)


async def process_load(job, datasette):
    """
    Process the load job.

    This function is launched as a background task.
    It should download the SQLite database from job["url"] and
    then create or replace the Datasette database with name job["name"].

    Update job["todo"] and job["done_count"] as appropriate.
    """
    try:
        # TODO: Download the SQLite file from job["url"].
        # For example, you might use aiohttp to stream the file.
        #
        # TODO: Once downloaded, use Datasette's internal API or mechanism
        # to create (or replace) the database with the name job["name"].
        #
        # For now we simulate work with a loop and sleep.
        job["todo"] = (
            100  # TODO: Set to actual total work (e.g. file size or number of rows)
        )
        for i in range(10):
            # Simulate doing some work.
            await asyncio.sleep(1)
            job["done_count"] += 10  # TODO: Update with the actual progress value.
        # Mark the job as finished.
        job["done"] = True
    except Exception as e:
        # Record any errors and mark the job as done.
        job["error"] = str(e)
        job["done"] = True


async def load_status_api(request, datasette):
    """
    Handle GET /-/load/status/<job_id>

    Returns the JSON status record for the given job.
    """
    job_id = request.url_vars["job_id"]
    job = JOBS.get(job_id)
    if job is None:
        return Response.json({"error": "Job not found"}, status=404)
    return Response.json(job)
