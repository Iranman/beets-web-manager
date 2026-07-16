from typing import Iterable, Set


JOB_PLUGIN_EXCLUDED: Set[str] = {"plexsync"}


def filter_job_plugins(
    plugins: Iterable[str],
    excluded: Set[str] = JOB_PLUGIN_EXCLUDED,
) -> str:
    """Return configured plugins safe for temporary Beets job configs."""
    return " ".join(p for p in plugins if p and p not in excluded)
