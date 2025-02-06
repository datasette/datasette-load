# datasette-load

[![PyPI](https://img.shields.io/pypi/v/datasette-load.svg)](https://pypi.org/project/datasette-load/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-load?include_prereleases&label=changelog)](https://github.com/datasette/datasette-load/releases)
[![Tests](https://github.com/datasette/datasette-load/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-load/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-load/blob/main/LICENSE)

API an UI for bulk loading data into Datasette from a URL

## Installation

Install this plugin in the same environment as Datasette.
```bash
datasette install datasette-load
```
## Usage

Usage instructions go here.

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
