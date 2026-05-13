"""
GitHub repo metadata — thin wrapper around shared utils/repo_metadata.

Preserves the original Celery task name for backwards-compatible routing.
"""
from celery_config import celery_app
from utils.repo_metadata import generate_repo_metadata as _shared_generate


@celery_app.task(name="routes.github.github_repo_metadata.generate_repo_metadata", bind=True, max_retries=2)
def generate_repo_metadata(self, user_id: str, repo_full_name: str):
    """Delegates to the shared provider-agnostic task."""
    _shared_generate(user_id, "github", repo_full_name)
