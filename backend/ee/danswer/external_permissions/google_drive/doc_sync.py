from collections.abc import Iterator
from typing import Any
from typing import cast

from googleapiclient.discovery import build  # type: ignore
from googleapiclient.errors import HttpError  # type: ignore
from sqlalchemy.orm import Session

from danswer.access.models import ExternalAccess
from danswer.connectors.cross_connector_utils.retry_wrapper import retry_builder
from danswer.connectors.google_drive.connector_auth import (
    get_google_drive_creds,
)
from danswer.connectors.google_drive.constants import FETCH_PERMISSIONS_SCOPES
from danswer.db.models import ConnectorCredentialPair
from danswer.db.users import batch_add_non_web_user_if_not_exists__no_commit
from danswer.utils.logger import setup_logger
from ee.danswer.db.document import upsert_document_external_perms__no_commit
from ee.danswer.external_permissions.permission_sync_utils import DocsWithAdditionalInfo

# Google Drive APIs are quite flakey and may 500 for an
# extended period of time. Trying to combat here by adding a very
# long retry period (~20 minutes of trying every minute)
add_retries = retry_builder(tries=5, delay=5, max_delay=30)


logger = setup_logger()


def _fetch_permissions_paginated(
    drive_service: Any, drive_file_id: str
) -> Iterator[dict[str, Any]]:
    next_token = None

    # Check if the file is trashed
    # Returning nothing here will cause the external permissions to
    # be empty which will get written to vespa (failing shut)
    try:
        file_metadata = add_retries(
            lambda: drive_service.files()
            .get(fileId=drive_file_id, fields="id, trashed")
            .execute()
        )()
    except HttpError as e:
        if e.resp.status == 404 or e.resp.status == 403:
            return
        logger.error(f"Failed to fetch permissions: {e}")
        raise

    if file_metadata.get("trashed", False):
        logger.debug(f"File with ID {drive_file_id} is trashed")
        return

    # Get paginated permissions for the file id
    while True:
        try:
            permissions_resp: dict[str, Any] = add_retries(
                lambda: (
                    drive_service.permissions()
                    .list(
                        fileId=drive_file_id,
                        fields="permissions(id, emailAddress, role, type, domain)",
                        supportsAllDrives=True,
                        pageToken=next_token,
                    )
                    .execute()
                )
            )()
        except HttpError as e:
            if e.resp.status == 404 or e.resp.status == 403:
                break
            logger.error(f"Failed to fetch permissions: {e}")
            raise

        for permission in permissions_resp.get("permissions", []):
            yield permission

        next_token = permissions_resp.get("nextPageToken")
        if not next_token:
            break


def _fetch_google_permissions_for_document_id(
    db_session: Session,
    drive_file_id: str,
    raw_credentials_json: dict[str, str],
    company_google_domains: list[str],
) -> ExternalAccess:
    # Authenticate and construct service
    google_drive_creds, _ = get_google_drive_creds(
        raw_credentials_json, scopes=FETCH_PERMISSIONS_SCOPES
    )
    if not google_drive_creds.valid:
        raise ValueError("Invalid Google Drive credentials")

    drive_service = build("drive", "v3", credentials=google_drive_creds)

    user_emails: set[str] = set()
    group_emails: set[str] = set()
    public = False
    for permission in _fetch_permissions_paginated(drive_service, drive_file_id):
        permission_type = permission["type"]
        if permission_type == "user":
            user_emails.add(permission["emailAddress"])
        elif permission_type == "group":
            group_emails.add(permission["emailAddress"])
        elif permission_type == "domain":
            if permission["domain"] in company_google_domains:
                public = True
        elif permission_type == "anyone":
            public = True

    batch_add_non_web_user_if_not_exists__no_commit(db_session, list(user_emails))

    return ExternalAccess(
        external_user_emails=user_emails,
        external_user_group_ids=group_emails,
        is_public=public,
    )


def gdrive_doc_sync(
    db_session: Session,
    cc_pair: ConnectorCredentialPair,
    docs_with_additional_info: list[DocsWithAdditionalInfo],
    sync_details: dict[str, Any],
) -> None:
    """
    Adds the external permissions to the documents in postgres
    if the document doesn't already exists in postgres, we create
    it in postgres so that when it gets created later, the permissions are
    already populated
    """
    for doc in docs_with_additional_info:
        ext_access = _fetch_google_permissions_for_document_id(
            db_session=db_session,
            drive_file_id=doc.additional_info,
            raw_credentials_json=cc_pair.credential.credential_json,
            company_google_domains=[
                cast(dict[str, str], sync_details)["company_domain"]
            ],
        )
        upsert_document_external_perms__no_commit(
            db_session=db_session,
            doc_id=doc.id,
            external_access=ext_access,
            source_type=cc_pair.connector.source,
        )
