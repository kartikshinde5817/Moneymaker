"""Discovery sources. Importing this package registers all of them."""

from app.services.discovery import (  # noqa: F401
    linkedin_search, maps_search, platforms, sources, tenders,
)
from app.services.discovery.base import (  # noqa: F401
    AI_AGENT, AI_AUTO, CV, EXPLICIT, INFERRED, KEYWORD_GROUPS, WEB,
    Prospect, RawSignal, SourceResult, available_sources, get_source,
    keywords_for, REGISTRY,
)
