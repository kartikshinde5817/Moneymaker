# Legacy

The original single-file app, kept only so the old leads can be re-imported
if needed. It is not run and nothing in `app/` depends on it.

- `old_app.py` — the previous version (`python old_app.py`)
- `old_app.db` — its SQLite database

These rows carry **no evidence** - just a business name - so they cannot be
qualified, scored or written to. They are no longer imported automatically,
because they drowned out real leads. If you want them as history anyway:

    curl -X POST http://127.0.0.1:8000/api/legacy/import

Otherwise this whole folder can be deleted.
