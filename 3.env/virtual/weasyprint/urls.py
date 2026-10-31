"""Virtualised stand-in for weasyprint.urls (import-time use only)."""


class URLFetcher:  # pragma: no cover
    def __init__(self, *args, **kwargs):
        pass

    def fetch(self, *args, **kwargs):
        raise RuntimeError(
            "weasyprint is virtualised in this environment: URLFetcher unavailable"
        )
