"""Helper functions for syncing files between terminal pods and object storage."""

import logging

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024


def restore_terraform_files_from_storage(
    core_v1, pod_name: str, namespace: str, user_id: str, session_id: str
) -> None:
    """
    Restore terraform files from storage to terminal pod if they exist.

    Args:
        core_v1: Kubernetes CoreV1Api client
        pod_name: Name of the terminal pod
        namespace: Kubernetes namespace
        user_id: User ID
        session_id: Chat session ID
    """
    try:
        from utils.storage.storage import get_storage_manager
        from kubernetes.stream import stream

        storage = get_storage_manager(user_id=user_id)
        prefix = f"{session_id}/terraform_dir/"
        full_prefix = f"users/{user_id}/{prefix}"

        file_infos = storage.list_files(prefix=prefix)

        if not file_infos:
            logger.info(f"No terraform files to restore for session {session_id}")
            return

        logger.info(
            f"Restoring {len(file_infos)} terraform files from storage to pod {pod_name}"
        )

        # Build correct terraform directory path (matches iac_write_tool.py)
        # User IDs are now plain UUIDs without prefixes (Auth.js migration)
        terraform_dir = (
            f"/home/appuser/terraform_workdir/user_{user_id}/session_{session_id}"
        )

        mkdir_cmd = ["/bin/sh", "-c", f"mkdir -p {terraform_dir}"]
        stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            command=mkdir_cmd,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )

        for file_info in file_infos:
            filename = file_info["path"].replace(full_prefix, "")
            if filename and not filename.startswith(".terraform"):
                size = file_info.get("size")
                if size and size > _MAX_FILE_SIZE_BYTES:
                    logger.warning(
                        f"Skipping file {filename}: too large ({size} bytes)"
                    )
                    continue

                content_bytes = storage.download_bytes(file_info["path"])
                content = content_bytes.decode("utf-8")

                escaped_content = content.replace("'", "'\"'\"'")
                write_cmd = [
                    "/bin/sh",
                    "-c",
                    f"cat > {terraform_dir}/{filename} << 'EOF'\n{escaped_content}\nEOF",
                ]
                stream(
                    core_v1.connect_get_namespaced_pod_exec,
                    pod_name,
                    namespace,
                    command=write_cmd,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                )
                logger.info(f"Restored file: {filename}")

        logger.info(f"Successfully restored terraform files for session {session_id}")

    except Exception as e:
        logger.warning(f"Failed to restore terraform files from storage: {e}")
        # Don't fail pod creation if restore fails
