"""Web UI + REST API. ``create_app`` is imported lazily so that
``python -m projektor.web.auth`` works without loading FastAPI."""


def __getattr__(name):
    if name == "create_app":
        from projektor.web.app import create_app

        return create_app
    raise AttributeError(name)
