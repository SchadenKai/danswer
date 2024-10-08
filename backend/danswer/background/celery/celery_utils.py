from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import Session

from danswer.background.celery.celery_redis import RedisConnectorDeletion
from danswer.background.task_utils import name_cc_prune_task
from danswer.configs.app_configs import ALLOW_SIMULTANEOUS_PRUNING
from danswer.configs.app_configs import MAX_PRUNING_DOCUMENT_RETRIEVAL_PER_MINUTE
from danswer.connectors.cross_connector_utils.rate_limit_wrapper import (
    rate_limit_builder,
)
from danswer.connectors.interfaces import BaseConnector
from danswer.connectors.interfaces import IdConnector
from danswer.connectors.interfaces import LoadConnector
from danswer.connectors.interfaces import PollConnector
from danswer.connectors.models import Document
from danswer.db.connector_credential_pair import get_connector_credential_pair
from danswer.db.engine import get_db_current_time
from danswer.db.enums import TaskStatus
from danswer.db.models import Connector
from danswer.db.models import Credential
from danswer.db.models import TaskQueueState
from danswer.db.tasks import check_task_is_live_and_not_timed_out
from danswer.db.tasks import get_latest_task
from danswer.db.tasks import get_latest_task_by_type
from danswer.redis.redis_pool import RedisPool
from danswer.server.documents.models import DeletionAttemptSnapshot
from danswer.utils.logger import setup_logger

logger = setup_logger()
redis_pool = RedisPool()


def _get_deletion_status(
    connector_id: int, credential_id: int, db_session: Session
) -> TaskQueueState | None:
    """We no longer store TaskQueueState in the DB for a deletion attempt.
    This function populates TaskQueueState by just checking redis.
    """
    cc_pair = get_connector_credential_pair(
        connector_id=connector_id, credential_id=credential_id, db_session=db_session
    )
    if not cc_pair:
        return None

    rcd = RedisConnectorDeletion(cc_pair.id)

    r = redis_pool.get_client()
    if not r.exists(rcd.fence_key):
        return None

    return TaskQueueState(
        task_id="", task_name=rcd.fence_key, status=TaskStatus.STARTED
    )


def get_deletion_attempt_snapshot(
    connector_id: int, credential_id: int, db_session: Session
) -> DeletionAttemptSnapshot | None:
    deletion_task = _get_deletion_status(connector_id, credential_id, db_session)
    if not deletion_task:
        return None

    return DeletionAttemptSnapshot(
        connector_id=connector_id,
        credential_id=credential_id,
        status=deletion_task.status,
    )


def should_prune_cc_pair(
    connector: Connector, credential: Credential, db_session: Session
) -> bool:
    if not connector.prune_freq:
        return False

    pruning_task_name = name_cc_prune_task(
        connector_id=connector.id, credential_id=credential.id
    )
    last_pruning_task = get_latest_task(pruning_task_name, db_session)
    current_db_time = get_db_current_time(db_session)

    if not last_pruning_task:
        time_since_initialization = current_db_time - connector.time_created
        if time_since_initialization.total_seconds() >= connector.prune_freq:
            return True
        return False

    if not ALLOW_SIMULTANEOUS_PRUNING:
        pruning_type_task_name = name_cc_prune_task()
        last_pruning_type_task = get_latest_task_by_type(
            pruning_type_task_name, db_session
        )

        if last_pruning_type_task and check_task_is_live_and_not_timed_out(
            last_pruning_type_task, db_session
        ):
            return False

    if check_task_is_live_and_not_timed_out(last_pruning_task, db_session):
        return False

    if not last_pruning_task.start_time:
        return False

    time_since_last_pruning = current_db_time - last_pruning_task.start_time
    return time_since_last_pruning.total_seconds() >= connector.prune_freq


def document_batch_to_ids(doc_batch: list[Document]) -> set[str]:
    return {doc.id for doc in doc_batch}


def extract_ids_from_runnable_connector(runnable_connector: BaseConnector) -> set[str]:
    """
    If the PruneConnector hasnt been implemented for the given connector, just pull
    all docs using the load_from_state and grab out the IDs
    """
    all_connector_doc_ids: set[str] = set()

    doc_batch_generator = None
    if isinstance(runnable_connector, IdConnector):
        all_connector_doc_ids = runnable_connector.retrieve_all_source_ids()
    elif isinstance(runnable_connector, LoadConnector):
        doc_batch_generator = runnable_connector.load_from_state()
    elif isinstance(runnable_connector, PollConnector):
        start = datetime(1970, 1, 1, tzinfo=timezone.utc).timestamp()
        end = datetime.now(timezone.utc).timestamp()
        doc_batch_generator = runnable_connector.poll_source(start=start, end=end)
    else:
        raise RuntimeError("Pruning job could not find a valid runnable_connector.")

    if doc_batch_generator:
        doc_batch_processing_func = document_batch_to_ids
        if MAX_PRUNING_DOCUMENT_RETRIEVAL_PER_MINUTE:
            doc_batch_processing_func = rate_limit_builder(
                max_calls=MAX_PRUNING_DOCUMENT_RETRIEVAL_PER_MINUTE, period=60
            )(document_batch_to_ids)
        for doc_batch in doc_batch_generator:
            all_connector_doc_ids.update(doc_batch_processing_func(doc_batch))

    return all_connector_doc_ids
