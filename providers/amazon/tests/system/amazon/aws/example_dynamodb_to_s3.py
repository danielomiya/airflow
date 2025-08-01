# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import boto3
import tenacity
from tenacity import before_log, before_sleep_log

from airflow.providers.amazon.aws.operators.s3 import S3CreateBucketOperator, S3DeleteBucketOperator
from airflow.providers.amazon.aws.transfers.dynamodb_to_s3 import DynamoDBToS3Operator

from tests_common.test_utils.version_compat import AIRFLOW_V_3_0_PLUS

if AIRFLOW_V_3_0_PLUS:
    from airflow.sdk import DAG, chain, task, task_group
else:
    # Airflow 2.10 compat
    from airflow.decorators import task, task_group  # type: ignore[attr-defined,no-redef]
    from airflow.models.baseoperator import chain  # type: ignore[attr-defined,no-redef]
    from airflow.models.dag import DAG  # type: ignore[attr-defined,no-redef,assignment]
from airflow.utils.trigger_rule import TriggerRule

from system.amazon.aws.utils import ENV_ID_KEY, SystemTestContextBuilder

log = logging.getLogger(__name__)

DAG_ID = "example_dynamodb_to_s3"

sys_test_context_task = SystemTestContextBuilder().build()

TABLE_ATTRIBUTES = [
    {"AttributeName": "ID", "AttributeType": "S"},
    {"AttributeName": "Value", "AttributeType": "S"},
]
TABLE_KEY_SCHEMA = [
    {"AttributeName": "ID", "KeyType": "HASH"},
    {"AttributeName": "Value", "KeyType": "RANGE"},
]
TABLE_THROUGHPUT = {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1}
S3_KEY_PREFIX = "dynamodb-segmented-file"


# UpdateContinuousBackups API might need multiple attempts to succeed
# Sometimes the API returns the error "Backups are being enabled for the table: <...>. Please retry later"
# Using a retry strategy with exponential backoff to remediate that
@tenacity.retry(
    stop=tenacity.stop_after_attempt(20),
    wait=tenacity.wait_exponential(min=5),
    before=before_log(log, logging.INFO),
    before_sleep=before_sleep_log(log, logging.WARNING),
)
def enable_point_in_time_recovery(table_name: str):
    boto3.client("dynamodb").update_continuous_backups(
        TableName=table_name,
        PointInTimeRecoverySpecification={
            "PointInTimeRecoveryEnabled": True,
        },
    )


@task
def set_up_table(table_name: str):
    dynamo_resource = boto3.resource("dynamodb")
    table = dynamo_resource.create_table(
        AttributeDefinitions=TABLE_ATTRIBUTES,
        TableName=table_name,
        KeySchema=TABLE_KEY_SCHEMA,
        ProvisionedThroughput=TABLE_THROUGHPUT,
    )
    boto3.client("dynamodb").get_waiter("table_exists").wait(
        TableName=table_name, WaiterConfig={"Delay": 10, "MaxAttempts": 10}
    )
    enable_point_in_time_recovery(table_name)
    table.put_item(Item={"ID": "123", "Value": "Testing"})


@task
def get_export_time(table_name: str):
    r = boto3.client("dynamodb").describe_continuous_backups(
        TableName=table_name,
    )

    return r["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]["EarliestRestorableDateTime"]


@task
def wait_for_bucket(s3_bucket_name):
    waiter = boto3.client("s3").get_waiter("bucket_exists")
    waiter.wait(Bucket=s3_bucket_name)


@task(trigger_rule=TriggerRule.ALL_DONE)
def delete_dynamodb_table(table_name: str):
    boto3.resource("dynamodb").Table(table_name).delete()
    boto3.client("dynamodb").get_waiter("table_not_exists").wait(
        TableName=table_name, WaiterConfig={"Delay": 10, "MaxAttempts": 10}
    )


@task_group
def incremental_export(table_name: str, start_time: datetime):
    """
    Incremental export requires a minimum window of 15 minutes of data to export.
    This task group allows us to have the sample code snippet for the docs while
    skipping the task when we run the actual test.
    """

    @task
    def get_latest_export_time(table_name: str):
        r = boto3.client("dynamodb").describe_continuous_backups(
            TableName=table_name,
        )

        return r["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]["LatestRestorableDateTime"]

    end_time = get_latest_export_time(table_name)

    # [START howto_transfer_dynamodb_to_s3_in_some_point_in_time_incremental_export]
    backup_db_to_point_in_time_incremental_export = DynamoDBToS3Operator(
        task_id="backup_db_to_point_in_time_incremental_export",
        dynamodb_table_name=table_name,
        s3_bucket_name=bucket_name,
        point_in_time_export=True,
        s3_key_prefix=f"{S3_KEY_PREFIX}-4-",
        export_table_to_point_in_time_kwargs={
            "ExportType": "INCREMENTAL_EXPORT",
            "IncrementalExportSpecification": {
                "ExportFromTime": start_time,
                "ExportToTime": end_time,
                "ExportViewType": "NEW_AND_OLD_IMAGES",
            },
        },
    )
    # [END howto_transfer_dynamodb_to_s3_in_some_point_in_time_incremental_export]
    # This operation can take a long time to complete
    backup_db_to_point_in_time_incremental_export.max_attempts = 90

    @task.short_circuit()
    def should_run_incremental_export(start_time: datetime, end_time: datetime):
        return end_time >= (start_time + timedelta(minutes=15))

    should_run_incremental = should_run_incremental_export(start_time=start_time, end_time=end_time)

    chain(end_time, should_run_incremental, backup_db_to_point_in_time_incremental_export)


with DAG(
    dag_id=DAG_ID,
    schedule="@once",
    start_date=datetime(2021, 1, 1),
    catchup=False,
    tags=["example"],
) as dag:
    test_context = sys_test_context_task()
    env_id = test_context[ENV_ID_KEY]
    table_name = f"{env_id}-dynamodb-table"
    bucket_name = f"{env_id}-dynamodb-bucket"

    create_table = set_up_table(table_name=table_name)

    create_bucket = S3CreateBucketOperator(task_id="create_bucket", bucket_name=bucket_name)

    # [START howto_transfer_dynamodb_to_s3]
    backup_db = DynamoDBToS3Operator(
        task_id="backup_db",
        dynamodb_table_name=table_name,
        s3_bucket_name=bucket_name,
        # Max output file size in bytes.  If the Table is too large, multiple files will be created.
        file_size=20,
    )
    # [END howto_transfer_dynamodb_to_s3]

    # [START howto_transfer_dynamodb_to_s3_segmented]
    # Segmenting allows the transfer to be parallelized into {segment} number of parallel tasks.
    backup_db_segment_1 = DynamoDBToS3Operator(
        task_id="backup_db_segment_1",
        dynamodb_table_name=table_name,
        s3_bucket_name=bucket_name,
        # Max output file size in bytes.  If the Table is too large, multiple files will be created.
        file_size=1000,
        s3_key_prefix=f"{S3_KEY_PREFIX}-1-",
        dynamodb_scan_kwargs={
            "TotalSegments": 2,
            "Segment": 0,
        },
    )

    backup_db_segment_2 = DynamoDBToS3Operator(
        task_id="backup_db_segment_2",
        dynamodb_table_name=table_name,
        s3_bucket_name=bucket_name,
        # Max output file size in bytes.  If the Table is too large, multiple files will be created.
        file_size=1000,
        s3_key_prefix=f"{S3_KEY_PREFIX}-2-",
        dynamodb_scan_kwargs={
            "TotalSegments": 2,
            "Segment": 1,
        },
    )
    # [END howto_transfer_dynamodb_to_s3_segmented]

    export_time = get_export_time(table_name)
    # [START howto_transfer_dynamodb_to_s3_in_some_point_in_time_full_export]
    backup_db_to_point_in_time_full_export = DynamoDBToS3Operator(
        task_id="backup_db_to_point_in_time_full_export",
        dynamodb_table_name=table_name,
        s3_bucket_name=bucket_name,
        point_in_time_export=True,
        export_time=export_time,
        s3_key_prefix=f"{S3_KEY_PREFIX}-3-",
    )
    # [END howto_transfer_dynamodb_to_s3_in_some_point_in_time_full_export]
    backup_db_to_point_in_time_full_export.max_attempts = 90

    delete_table = delete_dynamodb_table(table_name=table_name)

    delete_bucket = S3DeleteBucketOperator(
        task_id="delete_bucket",
        bucket_name=bucket_name,
        trigger_rule=TriggerRule.ALL_DONE,
        force_delete=True,
    )

    chain(
        # TEST SETUP
        test_context,
        create_table,
        create_bucket,
        wait_for_bucket(s3_bucket_name=bucket_name),
        # TEST BODY
        backup_db,
        backup_db_segment_1,
        backup_db_segment_2,
        export_time,
        backup_db_to_point_in_time_full_export,
        incremental_export(table_name=table_name, start_time=export_time),
        # TEST TEARDOWN
        delete_table,
        delete_bucket,
    )
    from tests_common.test_utils.watcher import watcher

    # This test needs watcher in order to properly mark success/failure
    # when "tearDown" task with trigger rule is part of the DAG
    list(dag.tasks) >> watcher()

from tests_common.test_utils.system_tests import get_test_run  # noqa: E402

# Needed to run the example DAG with pytest (see: tests/system/README.md#run_via_pytest)
test_run = get_test_run(dag)
