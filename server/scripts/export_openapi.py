"""Print the API's OpenAPI schema as JSON (used to generate the frontend's TypeScript types).

Only builds the schema - never serves requests - so placeholder secrets are fine here.
"""

import json
import os

os.environ.setdefault("SECRET_KEY", "openapi-export-placeholder-secret-key-0000")
os.environ.setdefault("ENCRYPTION_KEYS", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")

from app.main import create_app  # noqa: E402

print(json.dumps(create_app().openapi(), indent=2))
