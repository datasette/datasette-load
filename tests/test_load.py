import asyncio
from datasette.app import Datasette
import pytest
import sqlite_utils
import httpx
import os
import pathlib
import tempfile
import zipfile


def create_datasette(db_path=None, enable_wal=None):
    files = []
    if db_path:
        files = [db_path]
    temp_dir = tempfile.mkdtemp()
    options = {
        "staging_directory": os.path.join(temp_dir, "staging"),
        "database_directory": os.path.join(temp_dir, "database"),
    }
    if enable_wal is not None:
        options["enable_wal"] = enable_wal
    datasette = Datasette(
        files=files,
        memory=True,
        config={
            "permissions": {"datasette-load": {"id": "user"}},
            "plugins": {"datasette-load": options},
        },
    )
    return datasette


def create_temp_sqlite_db(tables_data):
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "data.db")
    db = sqlite_utils.Database(db_path)

    for table_name, rows in tables_data.items():
        db[table_name].insert_all(rows, pk="id")

    return db_path


@pytest.fixture
def non_mocked_hosts():
    # This ensures httpx doesn't mock external calls
    return ["localhost"]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", (None, "bob"))
async def test_permission_denied_to_load(user_id):
    datasette = create_datasette()
    cookies = {}
    if user_id:
        cookies["ds_actor"] = datasette.client.actor_cookie({"id": user_id})
    response = await datasette.client.post(
        "/-/load",
        json={"url": "https://example.com/data.db", "name": "new_db"},
        cookies=cookies,
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_load_api_success(httpx_mock):
    datasette = create_datasette()

    # 1. Set up a mock SQLite database and its URL
    tables_data = {
        "test_table": [
            {"id": 1, "name": "Test Row 1"},
            {"id": 2, "name": "Test Row 2"},
        ]
    }
    db_path = create_temp_sqlite_db(tables_data)
    db_url = "https://example.com/data.db"

    # Mock the HTTP request to return the content of the database
    with open(db_path, "rb") as f:
        db_content = f.read()
    httpx_mock.add_response(
        url=db_url,
        content=db_content,  # Directly serve the database file content
        headers={"Content-Length": str(len(db_content))},
    )

    # 2. Call POST /-/load
    response = await datasette.client.post(
        "/-/load",
        json={"url": db_url, "name": "new_db"},
        cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
    )
    assert response.status_code == 200
    job_id = response.json()["id"]
    assert job_id is not None
    status_url = response.json()["status_url"]
    assert status_url == f"http://localhost/-/load/status/{job_id}"
    status_path = status_url.split("localhost")[1]

    # 3. Poll /-/load/status/{job_id} until done
    while True:
        status_response = await datasette.client.get(status_path)
        assert status_response.status_code == 200
        status_data = status_response.json()
        if status_data["done"]:
            break
        await asyncio.sleep(0.1)  # short sleep to avoid busy-loop

    # 4. Verify the final status
    assert status_data["done"] is True
    assert status_data["error"] is None
    assert status_data["name"] == "new_db"
    assert "done_bytes" in status_data
    assert "todo_bytes" in status_data
    # Check that the database was correctly added, and that the tables and data are accurate
    db = datasette.get_database("new_db")
    assert "new_db" in datasette.databases
    result = await db.execute("SELECT * FROM test_table")
    assert len(result.rows) == 2
    assert tuple(result.rows[0]) == (1, "Test Row 1")
    assert tuple(result.rows[1]) == (2, "Test Row 2")


@pytest.mark.asyncio
async def test_load_api_with_headers(httpx_mock):
    datasette = create_datasette()

    db_path = create_temp_sqlite_db({"t": [{"id": 1}]})
    db_url = "https://example.com/headers.db"

    with open(db_path, "rb") as f:
        db_content = f.read()
    httpx_mock.add_response(
        url=db_url,
        content=db_content,
        headers={"Content-Length": str(len(db_content))},
    )

    response = await datasette.client.post(
        "/-/load",
        json={
            "url": db_url,
            "name": "headers_db",
            "headers": {"Authorization": "Bearer TEST"},
        },
        cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
    )
    assert response.status_code == 200
    status_path = response.json()["status_url"].split("localhost")[1]
    while True:
        status_response = await datasette.client.get(status_path)
        status_data = status_response.json()
        if status_data["done"]:
            break
        await asyncio.sleep(0.1)

    assert status_data["error"] is None
    req = httpx_mock.get_requests()[0]
    assert req.headers.get("Authorization") == "Bearer TEST"


@pytest.mark.asyncio
async def test_load_api_invalid_json(httpx_mock):
    datasette = create_datasette()
    response = await datasette.client.post(
        "/-/load",
        content="invalid json",  # Send invalid JSON
        cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
    )
    assert response.status_code == 400
    assert "Invalid JSON" in response.json()["error"]


@pytest.mark.asyncio
async def test_load_api_missing_params(httpx_mock):
    datasette = create_datasette()
    response = await datasette.client.post(
        "/-/load",
        json={"url": "http://example.com/db.sqlite"},  # Missing 'name'
        cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
    )
    assert response.status_code == 400
    assert "Missing required parameters" in response.json()["error"]

    response2 = await datasette.client.post(
        "/-/load",
        json={"name": "test_db"},  # Missing 'url'
        cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
    )
    assert response2.status_code == 400
    assert "Missing required parameters" in response.json()["error"]


@pytest.mark.asyncio
async def test_load_api_download_error(httpx_mock):
    datasette = create_datasette()

    # Simulate a network error during download
    httpx_mock.add_exception(
        httpx.NetworkError("Simulated network error"), url="http://example.com/error.db"
    )

    response = await datasette.client.post(
        "/-/load",
        json={"url": "http://example.com/error.db", "name": "error_db"},
        cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
    )
    assert response.status_code == 200
    status_url = response.json()["status_url"].split("localhost")[1]

    # Poll for completion
    while True:
        status_response = await datasette.client.get(status_url)
        status_data = status_response.json()
        if status_data["done"]:
            break
        await asyncio.sleep(0.1)

    assert status_data["done"] is True
    assert "Simulated network error" in status_data["error"]
    assert "error_db" not in datasette.databases


@pytest.mark.asyncio
async def test_load_api_integrity_check_failure(httpx_mock, tmp_path):
    datasette = create_datasette()
    # Create a corrupted database (e.g., by truncating a valid database file)
    good_db_path = create_temp_sqlite_db({"test": [{"id": 1}]})
    corrupted_db_path = os.path.join(tmp_path, "corrupted.db")

    with (
        open(good_db_path, "rb") as good_db,
        open(corrupted_db_path, "wb") as corrupted_db,
    ):
        corrupted_db.write(good_db.read(100))  # Truncate the file

    corrupted_db_url = "https://example.com/corrupted.db"

    # 2. Set up httpx_mock to return the content of the corrupted database
    with open(corrupted_db_path, "rb") as f:
        corrupted_db_content = f.read()

    httpx_mock.add_response(
        url=corrupted_db_url,
        content=corrupted_db_content,
        headers={"Content-Length": str(len(corrupted_db_content))},
    )

    # 3. Attempt to load the corrupted database
    response = await datasette.client.post(
        "/-/load",
        json={"url": corrupted_db_url, "name": "corrupted_db"},
        cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
    )
    job_id = response.json()["id"]
    status_url = f"/-/load/status/{job_id}"

    # 4. Poll /-/load/status/{job_id} until done
    while True:
        status_response = await datasette.client.get(status_url)
        status_data = status_response.json()
        if status_data["done"]:
            break
        await asyncio.sleep(0.1)
    # Verify that database loading failed and no database was created.
    assert status_data["done"]
    assert "database disk image is malformed" in status_data["error"]
    assert "corrupted_db" not in datasette.databases

    # It shouldn't be in the folder either
    path = datasette.plugin_config("datasette-load")["database_directory"]
    files = list(pathlib.Path(path).glob("*.db"))
    assert not files


@pytest.mark.asyncio
async def test_load_status_api_not_found():
    datasette = create_datasette()
    response = await datasette.client.get("/-/load/status/invalid-job-id")
    assert response.status_code == 404
    assert response.json()["error"] == "Job not found"


@pytest.mark.asyncio
async def test_database_removed_if_exists(httpx_mock):
    db_path = create_temp_sqlite_db({"test_table": [{"id": 1, "data": "exists"}]})
    db_uri = "https://example.com/data.db"

    httpx_mock.add_response(
        url=db_uri,
        content=open(db_path, "rb").read(),
        headers={"Content-Length": str(os.path.getsize(db_path))},
    )
    datasette = create_datasette(db_path)

    assert "data" in datasette.databases
    await datasette.invoke_startup()

    # Now, load a new database WITH THE SAME NAME
    response = await datasette.client.post(
        "/-/load",
        json={"url": db_uri, "name": "data"},
        cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
    )

    assert response.status_code == 200
    job_id = response.json()["id"]
    assert datasette._datasette_load_progress[job_id]

    status_url = response.json()["status_url"].split("localhost")[1]

    while True:
        response = await datasette.client.get(status_url)
        assert response.status_code == 200
        data = response.json()
        if data["done"]:
            break
        await asyncio.sleep(0.1)

    assert data["done"]
    assert not data["error"]
    assert "data" in datasette.databases
    db = datasette.databases["data"]
    result = await db.execute("select * from test_table")
    assert len(result.rows) == 1


@pytest.mark.asyncio
async def test_replace_database(httpx_mock):
    db_path1 = create_temp_sqlite_db({"test_table": [{"id": 1, "data": "exists"}]})
    db_path2 = create_temp_sqlite_db({"test_table": [{"id": 2, "data": "exists"}]})
    db_uri1 = "https://example.com/data.db"
    db_uri2 = "https://example.com/data2.db"

    httpx_mock.add_response(
        url=db_uri1,
        content=open(db_path1, "rb").read(),
        headers={"Content-Length": str(os.path.getsize(db_path1))},
    )
    httpx_mock.add_response(
        url=db_uri2,
        content=open(db_path2, "rb").read(),
        headers={"Content-Length": str(os.path.getsize(db_path2))},
    )
    datasette = create_datasette()
    await datasette.invoke_startup()

    # Load first database
    assert (
        await datasette.client.post(
            "/-/load",
            json={"url": db_uri1, "name": "data"},
            cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
        )
    ).status_code == 200
    await asyncio.sleep(0.1)
    data1 = (await datasette.client.get("/data/test_table.json?_shape=array")).json()
    assert data1 == [{"data": "exists", "id": 1}]

    # Load the second one to replace it
    assert (
        await datasette.client.post(
            "/-/load",
            json={"url": db_uri2, "name": "data"},
            cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
        )
    ).status_code == 200
    await asyncio.sleep(0.1)
    data1 = (await datasette.client.get("/data/test_table.json?_shape=array")).json()
    assert data1 == [{"data": "exists", "id": 2}]


@pytest.mark.asyncio
async def test_load_endpoint_html():
    datasette = create_datasette()
    response = await datasette.client.get(
        "/-/load",
        cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
    )
    assert response.status_code == 200
    assert ">Load data from a URL<" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", (None, "other", "user"))
async def test_homepage_menu(user_id):
    datasette = create_datasette()
    cookies = {}
    if user_id:
        cookies["ds_actor"] = datasette.client.actor_cookie({"id": user_id})
    response = await datasette.client.get("/", cookies=cookies)
    fragment = ">Load data into Datasette from a URL"
    if user_id == "user":
        assert fragment in response.text
    else:
        assert fragment not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_wal", (None, False, True))
async def test_enable_wal(httpx_mock, enable_wal):
    db_path = create_temp_sqlite_db({"test_table": [{"id": 1, "data": "exists"}]})
    db_url = "https://example.com/data.db"
    httpx_mock.add_response(
        url=db_url,
        content=open(db_path, "rb").read(),
        headers={"Content-Length": str(os.path.getsize(db_path))},
    )
    datasette = create_datasette(enable_wal=enable_wal)
    await datasette.invoke_startup()

    # Load first database
    assert (
        await datasette.client.post(
            "/-/load",
            json={"url": db_url, "name": "data"},
            cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
        )
    ).status_code == 200

    # check if enable-wal is on or off
    final_db_path = next(
        pathlib.Path(
            datasette.plugin_config("datasette-load")["database_directory"]
        ).glob("*")
    )
    db = sqlite_utils.Database(final_db_path)
    if not enable_wal:
        assert db.journal_mode == "delete"
    else:
        assert db.journal_mode == "wal"


@pytest.mark.asyncio
async def test_load_from_zip(httpx_mock, tmp_path):
    datasette = create_datasette()

    # Create a test database
    db_path = create_temp_sqlite_db({"test_table": [{"id": 1, "name": "Test Row"}]})

    # Create a zip file containing the database
    zip_path = os.path.join(tmp_path, "database.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(db_path, "test.db")

    # Mock the HTTP request
    zip_url = "https://example.com/database.zip"
    with open(zip_path, "rb") as f:
        zip_content = f.read()
    httpx_mock.add_response(
        url=zip_url,
        content=zip_content,
        headers={"Content-Length": str(len(zip_content))},
    )

    # Load the zipped database
    response = await datasette.client.post(
        "/-/load",
        json={"url": zip_url, "name": "from_zip"},
        cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
    )
    assert response.status_code == 200
    job_id = response.json()["id"]
    status_url = f"/-/load/status/{job_id}"

    # Poll until complete
    while True:
        status_response = await datasette.client.get(status_url)
        status_data = status_response.json()
        if status_data["done"]:
            break
        await asyncio.sleep(0.1)

    assert status_data["error"] is None
    assert "from_zip" in datasette.databases

    # Verify the database contents
    db = datasette.get_database("from_zip")
    result = await db.execute("SELECT * FROM test_table")
    assert len(result.rows) == 1
    assert tuple(result.rows[0]) == (1, "Test Row")


@pytest.mark.asyncio
async def test_load_from_zip_compression_bomb(httpx_mock, tmp_path):
    datasette = create_datasette()

    # Create a test database with lots of repeated data to get high compression
    db_path = create_temp_sqlite_db(
        {"test_table": [{"id": i, "name": "x" * 1000} for i in range(1000)]}
    )

    # Create a zip file containing the database
    zip_path = os.path.join(tmp_path, "database.zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_path, "test.db")

    # Mock the HTTP request
    zip_url = "https://example.com/database.zip"
    with open(zip_path, "rb") as f:
        zip_content = f.read()
    httpx_mock.add_response(
        url=zip_url,
        content=zip_content,
        headers={"Content-Length": str(len(zip_content))},
    )

    # Load the zipped database
    response = await datasette.client.post(
        "/-/load",
        json={"url": zip_url, "name": "from_zip"},
        cookies={"ds_actor": datasette.client.actor_cookie({"id": "user"})},
    )
    assert response.status_code == 200
    job_id = response.json()["id"]
    status_url = f"/-/load/status/{job_id}"

    # Poll until complete
    while True:
        status_response = await datasette.client.get(status_url)
        status_data = status_response.json()
        if status_data["done"]:
            break
        await asyncio.sleep(0.1)

    assert "would be more than 5x the size" in status_data["error"]
    assert "from_zip" not in datasette.databases
