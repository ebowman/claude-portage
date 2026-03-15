# claude-portage

Single-file Python CLI tool (zero external dependencies) for making Claude Code project metadata portable.

## Project structure

- `claude_portage.py` — all logic, single file
- `pyproject.toml` — package metadata
- `tests/test_portage.py` — unit tests

## Development

```bash
python3 -m pytest tests/ -v    # run tests
python3 claude_portage.py <cmd> # run directly without installing
```

## Release process

Releases go to both PyPI and Homebrew. Steps:

1. **Bump version** in both files (keep in sync):
   - `claude_portage.py`: `__version__ = "X.Y.Z"`
   - `pyproject.toml`: `version = "X.Y.Z"`

2. **Run tests**: `python3 -m pytest tests/ -v`

3. **Build and publish to PyPI**:
   ```bash
   rm -rf dist/ build/ claude_portage.egg-info/
   python3 -m build
   python3 -m twine check dist/*
   python3 -m twine upload dist/*
   ```

4. **Update Homebrew tap** (separate repo: `ebowman/homebrew-claude-portage`):
   ```bash
   # Get the new sdist URL and SHA from PyPI
   curl -sL "https://pypi.org/pypi/claude-portage/X.Y.Z/json" | python3 -c "
   import sys, json
   data = json.load(sys.stdin)
   for f in data['urls']:
       if f['packagetype'] == 'sdist':
           print('url', repr(f['url']))
           print('sha256', repr(f['digests']['sha256']))
   "

   # Clone tap, update Formula/claude-portage.rb with new url + sha256 + version, push
   ```
   The formula is at: `Formula/claude-portage.rb` in the `ebowman/homebrew-claude-portage` repo.

5. **Commit and push** the version bump to this repo.
