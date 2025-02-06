# datasette-load

[![PyPI](https://img.shields.io/pypi/v/datasette-load.svg)](https://pypi.org/project/datasette-load/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-load?include_prereleases&label=changelog)](https://github.com/datasette/datasette-load/releases)
[![Tests](https://github.com/datasette/datasette-load/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-load/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-load/blob/main/LICENSE)

API and UI for bulk loading data into Datasette from a URL

## Installation

Install this plugin in the same environment as Datasette.
```bash
datasette install datasette-load
```
## API

Users and API tokens with the `datasette-load` permission will be able to use the following API:

```
POST /-/load
{"url": "https://s3.amazonaws.com/til.simonwillison.net/tils.db", "name": "tils"}
```
This tells Datasette to download the SQLite database from the given URL and use it to create (or replace) the `/tils` database in the Datasette instance.

That API endpoint returns:
```json
{
  "id": "1D2A2328-199E-4D4D-AF3B-967131ADB795",
  "url": "https://s3.amazonaws.com/til.simonwillison.net/tils.db",
  "name": "tils",
  "done": false,
  "error": null,
  "todo": 20250624,
  "done": 0,
  "status_url": "https://blah.datasette/-/load/status/1D2A2328-199E-4D4D-AF3B-967131ADB795"
}
```
The `status_url` can be polled for completion. It will return the same JSON format.

When the download has finished the API will return `"done": true` and either `"error": null` if it worked or `"error": "error description"` if something went wrong.

## Development

To set up this plugin locally, first checkout the code. Then create a new virtual environment:
```bash
cd datasette-load
python -m venv venv
source venv/bin/activate
```
Now install the dependencies and test dependencies:
```bash
pip install -e '.[test]'
```
To run the tests:
```bash
python -m pytest
```
