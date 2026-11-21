"""Run the real app with attachment backups confined to the test runtime."""

from pathlib import Path

from main import create_app as create_pipeline_app
from src.bootstrap.env_loader import SETTINGS


def create_app():
    app = create_pipeline_app()
    service = app.state.pipeline_runtime_modules.attachment_notification_service
    if service is not None:
        # Production permits only shared backup mounts. Substitute the isolated
        # test root while retaining the service's real path validation and outbox.
        service._shared_backup_roots = (Path(SETTINGS.file_block_backup_dir).resolve(),)
    return app
