#!/usr/bin/env python3

from probe_executor_runtime import *  # noqa: F401,F403
from probe_executor_runtime import begin_probe_session as _begin_probe_session
from probe_executor_runtime import main


def begin_probe_session(*args, **kwargs):
    probe_id = kwargs.get("probe_id")
    if probe_id != SUPPORTED_PROBE_ID and kwargs.get("source_path") == DEFAULT_SOURCE_PATH:
        kwargs["source_path"] = None
    session = _begin_probe_session(*args, **kwargs)
    if session["probe"]["id"] == SUPPORTED_PROBE_ID:
        session["baseline"]["counter"] = "Tcp.InErrs"
        session["baseline"]["value"] = session["baseline"]["values"]["counter"]
    return session


if __name__ == "__main__":
    raise SystemExit(main())
