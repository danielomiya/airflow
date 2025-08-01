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

from datetime import datetime
from unittest import mock
from uuid import uuid4

import pytest
import uuid6
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from airflow._shared.timezones import timezone
from airflow.api_fastapi.auth.tokens import JWTValidator
from airflow.api_fastapi.execution_api.app import lifespan
from airflow.models import RenderedTaskInstanceFields, TaskReschedule, Trigger
from airflow.models.asset import AssetActive, AssetAliasModel, AssetEvent, AssetModel
from airflow.models.taskinstance import TaskInstance
from airflow.models.taskinstancehistory import TaskInstanceHistory
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import Asset, TaskGroup, task, task_group
from airflow.utils.state import DagRunState, State, TaskInstanceState, TerminalTIState

from tests_common.test_utils.db import (
    clear_db_assets,
    clear_db_dags,
    clear_db_runs,
    clear_db_serialized_dags,
    clear_rendered_ti_fields,
)

pytestmark = pytest.mark.db_test


DEFAULT_START_DATE = timezone.parse("2024-10-31T11:00:00Z")
DEFAULT_END_DATE = timezone.parse("2024-10-31T12:00:00Z")
DEFAULT_RENDERED_MAP_INDEX = "test rendered map index"


def _create_asset_aliases(session, num: int = 2) -> None:
    asset_aliases = [
        AssetAliasModel(
            id=i,
            name=f"simple{i}",
            group="alias",
        )
        for i in range(1, 1 + num)
    ]
    session.add_all(asset_aliases)
    session.commit()


@pytest.fixture
def client_with_extra_route(): ...


def test_id_matches_sub_claim(client, session, create_task_instance):
    # Test that this is validated at the router level, so we don't have to test it in each component
    # We validate it is set correctly, and test it once

    ti = create_task_instance(
        task_id="test_ti_run_state_conflict_if_not_queued",
        state="queued",
    )
    session.commit()

    validator = mock.AsyncMock(spec=JWTValidator)
    claims = {"sub": ti.id}

    def side_effect(cred, validators):
        if not validators:
            return claims
        if validators["sub"]["value"] != ti.id:
            raise RuntimeError("Fake auth denied")
        return claims

    # validator.avalidated_claims.side_effect = [{}, RuntimeError("fail for tests"), claims, claims]
    validator.avalidated_claims.side_effect = side_effect

    lifespan.registry.register_value(JWTValidator, validator)

    payload = {
        "state": "running",
        "hostname": "random-hostname",
        "unixname": "random-unixname",
        "pid": 100,
        "start_date": "2024-10-31T12:00:00Z",
    }

    resp = client.patch("/execution/task-instances/9c230b40-da03-451d-8bd7-be30471be383/run", json=payload)
    assert resp.status_code == 403
    validator.avalidated_claims.assert_called_with(
        mock.ANY, {"sub": {"essential": True, "value": "9c230b40-da03-451d-8bd7-be30471be383"}}
    )
    validator.avalidated_claims.reset_mock()

    resp = client.patch(f"/execution/task-instances/{ti.id}/run", json=payload)

    assert resp.status_code == 200, resp.json()

    validator.avalidated_claims.assert_awaited()


class TestTIRunState:
    def setup_method(self):
        clear_db_runs()
        clear_db_serialized_dags()
        clear_db_dags()

    def teardown_method(self):
        clear_db_runs()
        clear_db_serialized_dags()
        clear_db_dags()

    @pytest.mark.parametrize(
        "max_tries, should_retry",
        [
            pytest.param(0, False, id="max_retries=0"),
            pytest.param(3, True, id="should_retry"),
        ],
    )
    def test_ti_run_state_to_running(
        self,
        client,
        session,
        create_task_instance,
        time_machine,
        max_tries,
        should_retry,
    ):
        """
        Test that the Task Instance state is updated to running when the Task Instance is in a state where it can be
        marked as running.
        """
        instant_str = "2024-09-30T12:00:00Z"
        instant = timezone.parse(instant_str)
        time_machine.move_to(instant, tick=False)

        ti = create_task_instance(
            task_id="test_ti_run_state_to_running",
            state=State.QUEUED,
            dagrun_state=DagRunState.RUNNING,
            session=session,
            start_date=instant,
            dag_id=str(uuid4()),
        )
        ti.max_tries = max_tries
        session.commit()

        response = client.patch(
            f"/execution/task-instances/{ti.id}/run",
            json={
                "state": "running",
                "hostname": "random-hostname",
                "unixname": "random-unixname",
                "pid": 100,
                "start_date": instant_str,
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "dag_run": {
                "dag_id": ti.dag_id,
                "run_id": "test",
                "clear_number": 0,
                "logical_date": instant_str,
                "data_interval_start": instant.subtract(days=1).to_iso8601_string(),
                "data_interval_end": instant_str,
                "run_after": instant_str,
                "start_date": instant_str,
                "state": "running",
                "end_date": None,
                "run_type": "manual",
                "conf": {},
                "consumed_asset_events": [],
            },
            "task_reschedule_count": 0,
            "upstream_map_indexes": {},
            "max_tries": max_tries,
            "should_retry": should_retry,
            "variables": [],
            "connections": [],
            "xcom_keys_to_clear": [],
        }

        # Refresh the Task Instance from the database so that we can check the updated values
        session.refresh(ti)
        assert ti.state == State.RUNNING
        assert ti.hostname == "random-hostname"
        assert ti.unixname == "random-unixname"
        assert ti.pid == 100

        response1 = response.json()

        # Test that if we make a second request (simulating a network glitch so the client issues a retry)
        # that it is accepted and we get the same info back

        response = client.patch(
            f"/execution/task-instances/{ti.id}/run",
            json={
                "state": "running",
                "hostname": "random-hostname",
                "unixname": "random-unixname",
                "pid": 100,
                "start_date": instant_str,
            },
        )
        assert response.status_code == 200
        assert response.json() == response1

        # But that for a different pid on the same host (etc) it fails
        response = client.patch(
            f"/execution/task-instances/{ti.id}/run",
            json={
                "state": "running",
                "hostname": "random-hostname",
                "unixname": "random-unixname",
                "pid": 101,
                "start_date": instant_str,
            },
        )
        assert response.status_code == 409

    def test_dynamic_task_mapping_with_parse_time_value(self, client, dag_maker):
        """
        Test that the Task Instance upstream_map_indexes is correctly fetched when to running the Task Instances
        """

        with dag_maker("test_dynamic_task_mapping_with_parse_time_value", serialized=True):

            @task_group
            def task_group_1(arg1):
                @task
                def group1_task_1(arg1):
                    return {"a": arg1}

                @task
                def group1_task_2(arg2):
                    return arg2

                group1_task_2(group1_task_1(arg1))

            @task
            def task2():
                return None

            task_group_1.expand(arg1=[0, 1]) >> task2()

        dr = dag_maker.create_dagrun()
        for ti in dr.get_task_instances():
            ti.set_state(State.QUEUED)
        dag_maker.session.flush()

        # key: (task_id, map_index)
        # value: result upstream_map_indexes ({task_id: map_indexes})
        expected_upstream_map_indexes = {
            # no upstream task for task_group_1.group_task_1
            ("task_group_1.group1_task_1", 0): {},
            ("task_group_1.group1_task_1", 1): {},
            # the upstream task for task_group_1.group_task_2 is task_group_1.group_task_2
            # since they are in the same task group, the upstream map index should be the same as the task
            ("task_group_1.group1_task_2", 0): {"task_group_1.group1_task_1": 0},
            ("task_group_1.group1_task_2", 1): {"task_group_1.group1_task_1": 1},
            # the upstream task for task2 is the last tasks of task_group_1, which is
            # task_group_1.group_task_2
            # since they are not in the same task group, the upstream map index should include all the
            # expanded tasks
            ("task2", -1): {"task_group_1.group1_task_2": [0, 1]},
        }

        for ti in dr.get_task_instances():
            response = client.patch(
                f"/execution/task-instances/{ti.id}/run",
                json={
                    "state": "running",
                    "hostname": "random-hostname",
                    "unixname": "random-unixname",
                    "pid": 100,
                    "start_date": "2024-09-30T12:00:00Z",
                },
            )

            assert response.status_code == 200
            upstream_map_indexes = response.json()["upstream_map_indexes"]
            assert upstream_map_indexes == expected_upstream_map_indexes[(ti.task_id, ti.map_index)]

    def test_dynamic_task_mapping_with_xcom(self, client, dag_maker, create_task_instance, session, run_task):
        """
        Test that the Task Instance upstream_map_indexes is correctly fetched when to running the Task Instances with xcom
        """
        from airflow.models.taskmap import TaskMap

        with dag_maker(session=session):

            @task
            def task_1():
                return [0, 1]

            @task_group
            def tg(x, y):
                @task
                def task_2():
                    pass

                task_2()

            @task
            def task_3():
                pass

            tg.expand(x=task_1(), y=[1, 2, 3]) >> task_3()

        dr = dag_maker.create_dagrun()

        decision = dr.task_instance_scheduling_decisions(session=session)

        # Simulate task_1 execution to produce TaskMap.
        (ti_1,) = decision.schedulable_tis
        # ti_1 = dr.get_task_instance(task_id="task_1")
        ti_1.state = TaskInstanceState.SUCCESS
        session.add(TaskMap.from_task_instance_xcom(ti_1, [0, 1]))
        session.flush()

        # Now task_2 in mapped tagk group is expanded.
        decision = dr.task_instance_scheduling_decisions(session=session)
        for ti in decision.schedulable_tis:
            ti.state = TaskInstanceState.SUCCESS
        session.flush()

        decision = dr.task_instance_scheduling_decisions(session=session)
        (task_3_ti,) = decision.schedulable_tis
        task_3_ti.set_state(State.QUEUED)

        response = client.patch(
            f"/execution/task-instances/{task_3_ti.id}/run",
            json={
                "state": "running",
                "hostname": "random-hostname",
                "unixname": "random-unixname",
                "pid": 100,
                "start_date": "2024-09-30T12:00:00Z",
            },
        )
        assert response.json()["upstream_map_indexes"] == {"tg.task_2": [0, 1, 2, 3, 4, 5]}

    def test_next_kwargs_still_encoded(self, client, session, create_task_instance, time_machine):
        instant_str = "2024-09-30T12:00:00Z"
        instant = timezone.parse(instant_str)
        time_machine.move_to(instant, tick=False)

        ti = create_task_instance(
            task_id="test_next_kwargs_still_encoded",
            state=State.QUEUED,
            session=session,
            start_date=instant,
            dag_id=str(uuid4()),
        )

        ti.next_method = "execute_complete"
        # ti.next_kwargs under the hood applies the serde encoding for us
        ti.next_kwargs = {"moment": instant}

        session.commit()

        response = client.patch(
            f"/execution/task-instances/{ti.id}/run",
            json={
                "state": "running",
                "hostname": "random-hostname",
                "unixname": "random-unixname",
                "pid": 100,
                "start_date": instant_str,
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "dag_run": mock.ANY,
            "task_reschedule_count": 0,
            "upstream_map_indexes": {},
            "max_tries": 0,
            "should_retry": False,
            "variables": [],
            "connections": [],
            "xcom_keys_to_clear": [],
            "next_method": "execute_complete",
            "next_kwargs": {
                "__type": "dict",
                "__var": {"moment": {"__type": "datetime", "__var": 1727697600.0}},
            },
        }

    @pytest.mark.parametrize("resume", [True, False])
    def test_next_kwargs_determines_start_date_update(self, client, session, create_task_instance, resume):
        dag_start_time_str = "2024-09-30T12:00:00Z"
        dag_start_time = timezone.parse(dag_start_time_str)
        orig_task_start_time = dag_start_time.add(seconds=5)

        ti = create_task_instance(
            task_id="test_next_kwargs_still_encoded",
            state=State.QUEUED,
            session=session,
            start_date=orig_task_start_time,
            dag_id=str(uuid4()),
        )

        ti.start_date = orig_task_start_time
        ti.next_method = "execute_complete"

        second_start_time = orig_task_start_time.add(seconds=30)
        second_start_time_str = second_start_time.isoformat()

        # ti.next_kwargs under the hood applies the serde encoding for us
        if resume:
            ti.next_kwargs = {"moment": second_start_time}
            expected_start_date = orig_task_start_time
            expected_next_kwargs = {
                "__type": "dict",
                "__var": {"moment": {"__type": "datetime", "__var": second_start_time.timestamp()}},
            }
        else:
            expected_start_date = second_start_time
            expected_next_kwargs = None

        session.commit()

        response = client.patch(
            f"/execution/task-instances/{ti.id}/run",
            json={
                "state": "running",
                "hostname": "random-hostname",
                "unixname": "random-unixname",
                "pid": 100,
                "start_date": second_start_time_str,
            },
        )
        session.commit()

        assert response.status_code == 200
        assert response.json() == {
            "dag_run": mock.ANY,
            "task_reschedule_count": 0,
            "upstream_map_indexes": {},
            "max_tries": 0,
            "should_retry": False,
            "variables": [],
            "connections": [],
            "xcom_keys_to_clear": [],
            "next_method": "execute_complete",
            "next_kwargs": expected_next_kwargs,
        }
        session.expunge_all()
        ti = session.get(TaskInstance, ti.id)
        assert ti.start_date == expected_start_date

    @pytest.mark.parametrize(
        "initial_ti_state",
        [s for s in TaskInstanceState if s not in (TaskInstanceState.QUEUED, TaskInstanceState.RESTARTING)],
    )
    def test_ti_run_state_conflict_if_not_queued(
        self, client, session, create_task_instance, initial_ti_state
    ):
        """
        Test that a 409 error is returned when the Task Instance is not in a state where it can be marked as
        running. In this case, the Task Instance is first in NONE state so it cannot be marked as running.
        """
        ti = create_task_instance(
            task_id="test_ti_run_state_conflict_if_not_queued",
            state=initial_ti_state,
        )
        session.commit()

        response = client.patch(
            f"/execution/task-instances/{ti.id}/run",
            json={
                "state": "running",
                "hostname": "random-hostname",
                "unixname": "random-unixname",
                "pid": 100,
                "start_date": "2024-10-31T12:00:00Z",
            },
        )

        assert response.status_code == 409
        assert response.json() == {
            "detail": {
                "message": "TI was not in a state where it could be marked as running",
                "previous_state": initial_ti_state,
                "reason": "invalid_state",
            }
        }

        assert session.scalar(select(TaskInstance.state).where(TaskInstance.id == ti.id)) == initial_ti_state

    def test_xcom_not_cleared_for_deferral(self, client, session, create_task_instance, time_machine):
        """
        Test that the xcoms are not cleared when the Task Instance state is re-running after deferral.
        """
        instant_str = "2024-09-30T12:00:00Z"
        instant = timezone.parse(instant_str)
        time_machine.move_to(instant, tick=False)

        ti = create_task_instance(
            task_id="test_xcom_not_cleared_for_deferral",
            state=State.RUNNING,
            session=session,
            start_date=instant,
            dag_id=str(uuid4()),
        )
        session.commit()

        # Move this task to deferred
        payload = {
            "state": "deferred",
            "trigger_kwargs": {"key": "value", "moment": "2024-12-18T00:00:00Z"},
            "trigger_timeout": "P1D",  # 1 day
            "classpath": "my-classpath",
            "next_method": "execute_callback",
        }

        response = client.patch(f"/execution/task-instances/{ti.id}/state", json=payload)
        assert response.status_code == 204
        assert response.text == ""
        session.expire_all()

        # Deferred -> Queued so that we can run it again
        query = update(TaskInstance).where(TaskInstance.id == ti.id).values(state="queued")
        session.execute(query)
        session.commit()

        # Lets stage a xcom push
        ti.xcom_push(key="key", value="value")

        response = client.patch(
            f"/execution/task-instances/{ti.id}/run",
            json={
                "state": "running",
                "hostname": "random-hostname",
                "unixname": "random-unixname",
                "pid": 100,
                "start_date": instant_str,
            },
        )

        assert response.status_code == 200
        assert ti.xcom_pull(task_ids="test_xcom_not_cleared_for_deferral", key="key") == "value"


class TestTIUpdateState:
    def setup_method(self):
        clear_db_assets()
        clear_db_runs()

    def teardown_method(self):
        clear_db_assets()
        clear_db_runs()

    @pytest.mark.parametrize(
        ("state", "end_date", "expected_state"),
        [
            (State.SUCCESS, DEFAULT_END_DATE, State.SUCCESS),
            (State.FAILED, DEFAULT_END_DATE, State.FAILED),
            (State.SKIPPED, DEFAULT_END_DATE, State.SKIPPED),
        ],
    )
    def test_ti_update_state_to_terminal(
        self, client, session, create_task_instance, state, end_date, expected_state
    ):
        ti = create_task_instance(
            task_id="test_ti_update_state_to_terminal",
            start_date=DEFAULT_START_DATE,
            state=State.RUNNING,
        )
        session.commit()

        response = client.patch(
            f"/execution/task-instances/{ti.id}/state",
            json={
                "state": state,
                "end_date": end_date.isoformat(),
            },
        )

        assert response.status_code == 204
        assert response.text == ""

        session.expire_all()

        ti = session.get(TaskInstance, ti.id)
        assert ti.state == expected_state
        assert ti.end_date == end_date

    @pytest.mark.parametrize(
        ("state", "end_date", "expected_state", "rendered_map_index"),
        [
            (State.SUCCESS, DEFAULT_END_DATE, State.SUCCESS, DEFAULT_RENDERED_MAP_INDEX),
            (State.FAILED, DEFAULT_END_DATE, State.FAILED, DEFAULT_RENDERED_MAP_INDEX),
            (State.SKIPPED, DEFAULT_END_DATE, State.SKIPPED, DEFAULT_RENDERED_MAP_INDEX),
        ],
    )
    def test_ti_update_state_to_terminal_with_rendered_map_index(
        self, client, session, create_task_instance, state, end_date, expected_state, rendered_map_index
    ):
        ti = create_task_instance(
            task_id="test_ti_update_state_to_terminal_with_rendered_map_index",
            start_date=DEFAULT_START_DATE,
            state=State.RUNNING,
        )
        session.commit()

        response = client.patch(
            f"/execution/task-instances/{ti.id}/state",
            json={"state": state, "end_date": end_date.isoformat(), "rendered_map_index": rendered_map_index},
        )

        assert response.status_code == 204
        assert response.text == ""

        session.expire_all()

        ti = session.get(TaskInstance, ti.id)
        assert ti.state == expected_state
        assert ti.end_date == end_date
        assert ti.rendered_map_index == rendered_map_index

    @pytest.mark.parametrize(
        "task_outlets",
        [
            pytest.param([{"name": "my-task", "uri": "s3://bucket/my-task", "type": "Asset"}], id="asset"),
            pytest.param([{"name": "my-task", "type": "AssetNameRef"}], id="name-ref"),
            pytest.param([{"uri": "s3://bucket/my-task", "type": "AssetUriRef"}], id="uri-ref"),
        ],
    )
    @pytest.mark.parametrize(
        "outlet_events, expected_extra",
        [
            pytest.param([], {}, id="default"),
            pytest.param(
                [
                    {
                        "dest_asset_key": {"name": "my-task", "uri": "s3://bucket/my-task"},
                        "extra": {"foo": 1},
                    },
                    {
                        "dest_asset_key": {"name": "my-task-2", "uri": "s3://bucket/my-task-2"},
                        "extra": {"foo": 2},
                    },
                ],
                {"foo": 1},
                id="extra",
            ),
        ],
    )
    def test_ti_update_state_to_success_with_asset_events(
        self, client, session, create_task_instance, task_outlets, outlet_events, expected_extra
    ):
        asset = AssetModel(
            id=1,
            name="my-task",
            uri="s3://bucket/my-task",
            group="asset",
            extra={},
        )
        asset_active = AssetActive.for_asset(asset)
        session.add_all([asset, asset_active])

        ti = create_task_instance(
            task_id="test_ti_update_state_to_success_with_asset_events",
            start_date=DEFAULT_START_DATE,
            state=State.RUNNING,
        )
        session.commit()

        response = client.patch(
            f"/execution/task-instances/{ti.id}/state",
            json={
                "state": "success",
                "end_date": DEFAULT_END_DATE.isoformat(),
                "task_outlets": task_outlets,
                "outlet_events": outlet_events,
            },
        )

        assert response.status_code == 204
        assert response.text == ""
        session.expire_all()

        event = session.scalars(select(AssetEvent)).all()
        assert len(event) == 1
        assert event[0].asset == AssetModel(name="my-task", uri="s3://bucket/my-task", extra={})
        assert event[0].extra == expected_extra

    def test_ti_update_state_to_failed_with_inactive_asset(self, client, session, create_task_instance):
        # inactive
        asset = AssetModel(
            id=1,
            name="my-task-2",
            uri="s3://bucket/my-task",
            group="asset",
            extra={},
        )
        session.add(asset)

        ti = create_task_instance(
            task_id="test_ti_update_state_to_success_with_asset_events",
            start_date=DEFAULT_START_DATE,
            state=State.RUNNING,
        )
        session.commit()

        response = client.patch(
            f"/execution/task-instances/{ti.id}/state",
            json={
                "state": "success",
                "end_date": DEFAULT_END_DATE.isoformat(),
                "task_outlets": [{"name": "my-task-2", "uri": "s3://bucket/my-task", "type": "Asset"}],
                "outlet_events": [],
            },
        )

        assert response.status_code == 204
        session.expire_all()

        ti = session.get(TaskInstance, ti.id)
        assert ti.state == State.FAILED

    @pytest.mark.parametrize(
        "outlet_events, expected_extra",
        [
            pytest.param([], None, id="default"),
            pytest.param(
                [
                    {
                        "dest_asset_key": {"name": "my-task", "uri": "s3://bucket/my-task"},
                        "source_alias_name": "simple1",
                        "extra": {"foo": 1},
                    },
                    {
                        "dest_asset_key": {"name": "my-task-2", "uri": "s3://bucket/my-task-2"},
                        "extra": {"foo": 2},
                    },
                    {
                        "dest_asset_key": {"name": "my-task-2", "uri": "s3://bucket/my-task-2"},
                        "source_alias_name": "simple2",
                        "extra": {"foo": 3},
                    },
                ],
                {"foo": 1},
                id="extra",
            ),
        ],
    )
    def test_ti_update_state_to_success_with_asset_alias_events(
        self, client, session, create_task_instance, outlet_events, expected_extra
    ):
        asset = AssetModel(
            id=1,
            name="my-task",
            uri="s3://bucket/my-task",
            group="asset",
            extra={},
        )
        asset_active = AssetActive.for_asset(asset)
        session.add_all([asset, asset_active])

        _create_asset_aliases(session, num=2)

        ti = create_task_instance(
            task_id="test_ti_update_state_to_success_with_asset_events",
            start_date=DEFAULT_START_DATE,
            state=State.RUNNING,
        )
        session.commit()

        response = client.patch(
            f"/execution/task-instances/{ti.id}/state",
            json={
                "state": "success",
                "end_date": DEFAULT_END_DATE.isoformat(),
                "task_outlets": [{"name": "simple1", "type": "AssetAlias"}],
                "outlet_events": outlet_events,
            },
        )

        assert response.status_code == 204
        assert response.text == ""
        session.expire_all()

        events = session.scalars(select(AssetEvent)).all()
        if expected_extra is None:
            assert events == []
        else:
            assert len(events) == 1
            assert events[0].asset == AssetModel(name="my-task", uri="s3://bucket/my-task", extra={})
            assert events[0].extra == expected_extra

    def test_ti_update_state_not_found(self, client, session):
        """
        Test that a 404 error is returned when the Task Instance does not exist.
        """
        task_instance_id = "0182e924-0f1e-77e6-ab50-e977118bc139"

        # Pre-condition: the Task Instance does not exist
        assert session.get(TaskInstance, task_instance_id) is None

        payload = {"state": "success", "end_date": "2024-10-31T12:30:00Z"}

        response = client.patch(f"/execution/task-instances/{task_instance_id}/state", json=payload)
        assert response.status_code == 404
        assert response.json()["detail"] == {
            "reason": "not_found",
            "message": "Task Instance not found",
        }

    def test_ti_update_state_running_errors(self, client, session, create_task_instance, time_machine):
        """
        Test that a 422 error is returned when the Task Instance state is RUNNING in the payload.

        Task should be set to Running state via the /execution/task-instances/{task_instance_id}/run endpoint.
        """
        ti = create_task_instance(
            task_id="test_ti_update_state_running_errors",
            state=State.QUEUED,
            session=session,
            start_date=DEFAULT_START_DATE,
        )

        session.commit()

        response = client.patch(f"/execution/task-instances/{ti.id}/state", json={"state": "running"})

        assert response.status_code == 422

    def test_ti_update_state_database_error(self, client, session, create_task_instance):
        """
        Test that a database error is handled correctly when updating the Task Instance state.
        """
        ti = create_task_instance(
            task_id="test_ti_update_state_database_error",
            state=State.QUEUED,
        )
        session.commit()
        payload = {
            "state": "success",
            "end_date": "2024-10-31T12:00:00Z",
        }

        with (
            mock.patch(
                "airflow.api_fastapi.common.db.common.Session.execute",
                side_effect=[
                    mock.Mock(one=lambda: ("running", 1, 0, "dag")),  # First call returns "queued"
                    mock.Mock(one=lambda: ("running", 1, 0, "dag")),  # Second call returns "queued"
                    SQLAlchemyError("Database error"),  # Last call raises an error
                ],
            ),
            mock.patch(
                "airflow.models.taskinstance.TaskInstance.register_asset_changes_in_db",
            ) as mock_register_asset_changes_in_db,
        ):
            mock_register_asset_changes_in_db.return_value = None
            response = client.patch(f"/execution/task-instances/{ti.id}/state", json=payload)
            assert response.status_code == 500
            assert response.json()["detail"] == "Database error occurred"

    def test_ti_update_state_to_deferred(self, client, session, create_task_instance, time_machine):
        """
        Test that tests if the transition to deferred state is handled correctly.
        """
        ti = create_task_instance(
            task_id="test_ti_update_state_to_deferred",
            state=State.RUNNING,
            session=session,
        )
        session.commit()

        instant = timezone.datetime(2024, 11, 22)
        time_machine.move_to(instant, tick=False)

        payload = {
            "state": "deferred",
            # Raw payload is already "encoded", but not encrypted
            "trigger_kwargs": {
                "__type": "dict",
                "__var": {"key": "value", "moment": {"__type": "datetime", "__var": 1734480001.0}},
            },
            "trigger_timeout": "P1D",  # 1 day
            "classpath": "my-classpath",
            "next_method": "execute_callback",
            "next_kwargs": {
                "__type": "dict",
                "__var": {"foo": {"__type": "datetime", "__var": 1734480000.0}, "bar": "abc"},
            },
        }

        response = client.patch(f"/execution/task-instances/{ti.id}/state", json=payload)

        assert response.status_code == 204
        assert response.text == ""

        session.expire_all()

        tis = session.query(TaskInstance).all()
        assert len(tis) == 1

        assert tis[0].state == TaskInstanceState.DEFERRED
        assert tis[0].next_method == "execute_callback"
        assert tis[0].next_kwargs == {
            "bar": "abc",
            "foo": datetime(2024, 12, 18, 00, 00, 00, tzinfo=timezone.utc),
        }
        assert tis[0].trigger_timeout == timezone.make_aware(datetime(2024, 11, 23), timezone=timezone.utc)

        t = session.query(Trigger).all()
        assert len(t) == 1
        assert t[0].created_date == instant
        assert t[0].classpath == "my-classpath"
        assert t[0].kwargs == {
            "key": "value",
            "moment": datetime(2024, 12, 18, 00, 00, 1, tzinfo=timezone.utc),
        }

    def test_ti_update_state_to_reschedule(self, client, session, create_task_instance, time_machine):
        """
        Test that tests if the transition to reschedule state is handled correctly.
        """
        instant = timezone.datetime(2024, 10, 30)
        time_machine.move_to(instant, tick=False)

        ti = create_task_instance(
            task_id="test_ti_update_state_to_reschedule",
            state=State.RUNNING,
            session=session,
        )
        ti.start_date = instant
        session.commit()

        payload = {
            "state": "up_for_reschedule",
            "reschedule_date": "2024-10-31T11:03:00+00:00",
            "end_date": DEFAULT_END_DATE.isoformat(),
        }

        response = client.patch(f"/execution/task-instances/{ti.id}/state", json=payload)

        assert response.status_code == 204
        assert response.text == ""

        session.expire_all()

        tis = session.query(TaskInstance).all()
        assert len(tis) == 1
        assert tis[0].state == TaskInstanceState.UP_FOR_RESCHEDULE
        assert tis[0].next_method is None
        assert tis[0].next_kwargs is None
        assert tis[0].duration == 129600

        trs = session.query(TaskReschedule).all()
        assert len(trs) == 1
        assert trs[0].task_instance.dag_id == "dag"
        assert trs[0].task_instance.task_id == "test_ti_update_state_to_reschedule"
        assert trs[0].task_instance.run_id == "test"
        assert trs[0].ti_id == tis[0].id
        assert trs[0].start_date == instant
        assert trs[0].end_date == DEFAULT_END_DATE
        assert trs[0].reschedule_date == timezone.parse("2024-10-31T11:03:00+00:00")
        assert trs[0].task_instance.map_index == -1
        assert trs[0].duration == 129600

    @pytest.mark.backend("mysql")
    def test_ti_update_state_reschedule_mysql_limit(
        self, client, session, create_task_instance, time_machine
    ):
        """Test that the reschedule date is validated against MySQL's TIMESTAMP limit."""
        instant = timezone.datetime(2024, 10, 30)
        time_machine.move_to(instant, tick=False)

        ti = create_task_instance(
            task_id="test_ti_update_state_reschedule_mysql_limit",
            state=State.RUNNING,
            session=session,
        )
        ti.start_date = instant
        session.commit()

        # Date beyond MySQL's TIMESTAMP limit (2038-01-19 03:14:07)
        future_date = timezone.datetime(2038, 1, 19, 3, 14, 8)

        response = client.patch(
            f"/execution/task-instances/{ti.id}/state",
            json={
                "state": TaskInstanceState.UP_FOR_RESCHEDULE,
                "reschedule_date": future_date.isoformat(),
                "end_date": DEFAULT_END_DATE.isoformat(),
            },
        )

        assert response.status_code == 204
        assert response.text == ""

        session.expire_all()

        ti = session.get(TaskInstance, ti.id)
        assert ti.state == State.FAILED

    def test_ti_update_state_handle_retry(self, client, session, create_task_instance):
        ti = create_task_instance(
            task_id="test_ti_update_state_to_retry",
            state=State.RUNNING,
        )
        session.commit()

        response = client.patch(
            f"/execution/task-instances/{ti.id}/state",
            json={
                "state": State.UP_FOR_RETRY,
                "end_date": DEFAULT_END_DATE.isoformat(),
            },
        )

        assert response.status_code == 204
        assert response.text == ""

        ti = session.scalar(
            select(TaskInstance).filter_by(task_id=ti.task_id, run_id=ti.run_id, dag_id=ti.dag_id)
        )
        # ti = session.get(TaskInstance, ti.id)
        assert ti.state == State.UP_FOR_RETRY
        assert ti.next_method is None
        assert ti.next_kwargs is None

        tih = (
            session.query(TaskInstanceHistory)
            .where(TaskInstanceHistory.task_id == ti.task_id, TaskInstanceHistory.run_id == ti.run_id)
            .one()
        )
        assert tih.task_instance_id
        assert tih.task_instance_id != ti.id

    def test_ti_update_state_to_failed_table_check(self, client, session, create_task_instance):
        # we just want to fail in this test, no need to retry
        ti = create_task_instance(
            task_id="test_ti_update_state_to_failed_table_check",
            state=State.RUNNING,
        )
        ti.start_date = DEFAULT_START_DATE
        session.commit()

        response = client.patch(
            f"/execution/task-instances/{ti.id}/state",
            json={
                "state": TerminalTIState.FAILED,
                "end_date": DEFAULT_END_DATE.isoformat(),
            },
        )

        assert response.status_code == 204
        assert response.text == ""

        session.expire_all()

        ti = session.get(TaskInstance, ti.id)
        assert ti.state == State.FAILED
        assert ti.next_method is None
        assert ti.next_kwargs is None
        assert ti.duration == 3600.00

    def test_ti_update_state_not_running(self, client, session, create_task_instance):
        """Test that a 409 error is returned when attempting to update a TI that is not in RUNNING state."""
        ti = create_task_instance(
            task_id="test_ti_update_state_not_running",
            state=State.SUCCESS,
            session=session,
            start_date=DEFAULT_START_DATE,
        )
        session.commit()

        payload = {
            "state": "failed",
            "end_date": DEFAULT_END_DATE.isoformat(),
        }

        response = client.patch(f"/execution/task-instances/{ti.id}/state", json=payload)
        assert response.status_code == 409
        assert response.json()["detail"] == {
            "reason": "invalid_state",
            "message": "TI was not in the running state so it cannot be updated",
            "previous_state": State.SUCCESS,
        }

        # Verify the task instance state hasn't changed
        session.refresh(ti)
        assert ti.state == State.SUCCESS


class TestTISkipDownstream:
    def setup_method(self):
        clear_db_runs()

    def teardown_method(self):
        clear_db_runs()

    @pytest.mark.parametrize("_json", (({"tasks": ["t1"]}), ({"tasks": [("t1", -1)]})))
    def test_ti_skip_downstream(self, client, session, create_task_instance, dag_maker, _json):
        with dag_maker("skip_downstream_dag", session=session):
            t0 = EmptyOperator(task_id="t0")
            t1 = EmptyOperator(task_id="t1")
            t0 >> t1
        dr = dag_maker.create_dagrun(run_id="run")

        ti0 = dr.get_task_instance("t0")
        ti0.set_state(State.SUCCESS)

        response = client.patch(
            f"/execution/task-instances/{ti0.id}/skip-downstream",
            json=_json,
        )
        ti1 = dr.get_task_instance("t1")

        assert response.status_code == 204
        assert ti1.state == State.SKIPPED


class TestTIHealthEndpoint:
    def setup_method(self):
        clear_db_runs()

    def teardown_method(self):
        clear_db_runs()

    @pytest.mark.parametrize(
        ("hostname", "pid", "expected_status_code", "expected_detail"),
        [
            # Case: Successful heartbeat
            ("random-hostname", 1789, 204, None),
            # Case: Conflict due to hostname mismatch
            (
                "wrong-hostname",
                1789,
                409,
                {
                    "reason": "running_elsewhere",
                    "message": "TI is already running elsewhere",
                    "current_hostname": "random-hostname",
                    "current_pid": 1789,
                },
            ),
            # Case: Conflict due to pid mismatch
            (
                "random-hostname",
                1054,
                409,
                {
                    "reason": "running_elsewhere",
                    "message": "TI is already running elsewhere",
                    "current_hostname": "random-hostname",
                    "current_pid": 1789,
                },
            ),
        ],
    )
    def test_ti_heartbeat(
        self,
        client,
        session,
        create_task_instance,
        hostname,
        pid,
        expected_status_code,
        expected_detail,
        time_machine,
    ):
        """Test the TI heartbeat endpoint for various scenarios including conflicts."""
        time_now = timezone.parse("2024-10-31T12:00:00Z")

        # Freeze time to a specific time
        time_machine.move_to(time_now, tick=False)

        ti = create_task_instance(
            task_id="test_ti_heartbeat",
            state=State.RUNNING,
            hostname="random-hostname",
            pid=1789,
            session=session,
        )
        session.commit()
        task_instance_id = ti.id

        # Pre-condition: TI heartbeat is NONE
        assert ti.last_heartbeat_at is None

        response = client.put(
            f"/execution/task-instances/{task_instance_id}/heartbeat",
            json={"hostname": hostname, "pid": pid},
        )

        assert response.status_code == expected_status_code

        if expected_status_code == 204:
            # If successful, ensure last_heartbeat_at is updated
            session.refresh(ti)
            assert ti.last_heartbeat_at == time_now
            assert response.text == ""
        else:
            # If there's an error, check the error detail
            assert response.json()["detail"] == expected_detail

    def test_ti_heartbeat_non_existent_task(self, client, session, create_task_instance):
        """Test that a 404 error is returned when the Task Instance does not exist."""
        task_instance_id = "0182e924-0f1e-77e6-ab50-e977118bc139"

        # Pre-condition: the Task Instance does not exist
        assert session.get(TaskInstance, task_instance_id) is None

        response = client.put(
            f"/execution/task-instances/{task_instance_id}/heartbeat",
            json={"hostname": "random-hostname", "pid": 1547},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == {
            "reason": "not_found",
            "message": "Task Instance not found",
        }

    @pytest.mark.parametrize(
        "ti_state",
        [State.SUCCESS, State.FAILED],
    )
    def test_ti_heartbeat_when_task_not_running(self, client, session, create_task_instance, ti_state):
        """Test that a 409 error is returned when the Task Instance is not in RUNNING state."""
        ti = create_task_instance(
            task_id="test_ti_heartbeat_when_task_not_running",
            state=ti_state,
            hostname="random-hostname",
            pid=1547,
            session=session,
        )
        session.commit()
        task_instance_id = ti.id

        response = client.put(
            f"/execution/task-instances/{task_instance_id}/heartbeat",
            json={"hostname": "random-hostname", "pid": 1547},
        )

        assert response.status_code == 409
        assert response.json()["detail"] == {
            "reason": "not_running",
            "message": "TI is no longer in the running state and task should terminate",
            "current_state": ti_state,
        }

    def test_ti_heartbeat_update(self, client, session, create_task_instance, time_machine):
        """Test that the Task Instance heartbeat is updated when the Task Instance is running."""
        # Set initial time for the test
        time_now = timezone.parse("2024-10-31T12:00:00Z")
        time_machine.move_to(time_now, tick=False)

        ti = create_task_instance(
            task_id="test_ti_heartbeat_update",
            state=State.RUNNING,
            hostname="random-hostname",
            pid=1547,
            last_heartbeat_at=time_now,
            session=session,
        )
        session.commit()
        task_instance_id = ti.id

        # Pre-condition: TI heartbeat is set
        assert ti.last_heartbeat_at == time_now, "Initial last_heartbeat_at should match time_now"

        # Move time forward by 10 minutes
        new_time = time_now.add(minutes=10)
        time_machine.move_to(new_time, tick=False)

        response = client.put(
            f"/execution/task-instances/{task_instance_id}/heartbeat",
            json={"hostname": "random-hostname", "pid": 1547},
        )

        assert response.status_code == 204

        # If successful, ensure last_heartbeat_at is updated
        session.refresh(ti)
        assert ti.last_heartbeat_at == time_now.add(minutes=10)


class TestTIPutRTIF:
    def setup_method(self):
        clear_db_runs()
        clear_rendered_ti_fields()

    def teardown_method(self):
        clear_db_runs()
        clear_rendered_ti_fields()

    @pytest.mark.parametrize(
        "payload",
        [
            # string value
            {"field1": "string_value", "field2": "another_string"},
            # dictionary value
            {"field1": {"nested_key": "nested_value"}},
            # string lists value
            {"field1": ["123"], "field2": ["a", "b", "c"]},
            # list of JSON values
            {"field1": [1, "string", 3.14, True, None, {"nested": "dict"}]},
            # nested dictionary with mixed types in lists
            {
                "field1": {"nested_dict": {"key1": 123, "key2": "value"}},
                "field2": [3.14, {"sub_key": "sub_value"}, [1, 2]],
            },
        ],
    )
    def test_ti_put_rtif_success(self, client, session, create_task_instance, payload):
        ti = create_task_instance(
            task_id="test_ti_put_rtif_success",
            state=State.RUNNING,
            session=session,
        )
        session.commit()
        response = client.put(f"/execution/task-instances/{ti.id}/rtif", json=payload)
        assert response.status_code == 201
        assert response.json() == {"message": "Rendered task instance fields successfully set"}

        session.expire_all()

        rtifs = session.query(RenderedTaskInstanceFields).all()
        assert len(rtifs) == 1

        assert rtifs[0].dag_id == "dag"
        assert rtifs[0].run_id == "test"
        assert rtifs[0].task_id == "test_ti_put_rtif_success"
        assert rtifs[0].map_index == -1
        assert rtifs[0].rendered_fields == payload

    def test_ti_put_rtif_missing_ti(self, client, session, create_task_instance):
        create_task_instance(
            task_id="test_ti_put_rtif_missing_ti",
            state=State.RUNNING,
            session=session,
        )
        session.commit()

        payload = {"field1": "rendered_value1", "field2": "rendered_value2"}

        random_id = uuid6.uuid7()
        response = client.put(f"/execution/task-instances/{random_id}/rtif", json=payload)
        assert response.status_code == 404
        assert response.json()["detail"] == "Not Found"


class TestPreviousDagRun:
    def setup_method(self):
        clear_db_runs()

    def teardown_method(self):
        clear_db_runs()

    def test_ti_previous_dag_run(self, client, session, create_task_instance, dag_maker):
        """Test that the previous dag run is returned correctly for a task instance."""
        ti = create_task_instance(
            task_id="test_ti_previous_dag_run",
            dag_id="test_dag",
            logical_date=timezone.datetime(2025, 1, 19),
            state=State.RUNNING,
            start_date=timezone.datetime(2024, 1, 17),
            session=session,
        )
        session.commit()

        # Create 2 DagRuns for the same DAG to verify that the correct DagRun (last) is returned
        dr1 = dag_maker.create_dagrun(
            run_id="test_run_id_1",
            logical_date=timezone.datetime(2025, 1, 17),
            run_type="scheduled",
            state=State.SUCCESS,
            session=session,
        )
        dr1.end_date = timezone.datetime(2025, 1, 17, 1, 0, 0)

        dr2 = dag_maker.create_dagrun(
            run_id="test_run_id_2",
            logical_date=timezone.datetime(2025, 1, 18),
            run_type="scheduled",
            state=State.SUCCESS,
            session=session,
        )

        dr2.end_date = timezone.datetime(2025, 1, 18, 1, 0, 0)

        session.commit()

        response = client.get(f"/execution/task-instances/{ti.id}/previous-successful-dagrun")
        assert response.status_code == 200
        assert response.json() == {
            "data_interval_start": "2025-01-18T00:00:00Z",
            "data_interval_end": "2025-01-19T00:00:00Z",
            "start_date": "2024-01-17T00:00:00Z",
            "end_date": "2025-01-18T01:00:00Z",
        }

    def test_ti_previous_dag_run_not_found(self, client, session):
        ti_id = "0182e924-0f1e-77e6-ab50-e977118bc139"

        assert session.get(TaskInstance, ti_id) is None

        response = client.get(f"/execution/task-instances/{ti_id}/previous-successful-dagrun")
        assert response.status_code == 200
        assert response.json() == {
            "data_interval_start": None,
            "data_interval_end": None,
            "start_date": None,
            "end_date": None,
        }

    def test_ti_with_none_as_logical_date(self, client, session, create_task_instance, dag_maker):
        ti = create_task_instance(
            task_id="test_ti_with_none_as_logical_date",
            dag_id="test_dag",
            logical_date=None,
            state=State.RUNNING,
            start_date=timezone.datetime(2024, 1, 17),
            session=session,
        )
        session.commit()

        assert ti.logical_date is None

        dr1 = dag_maker.create_dagrun(
            run_id="test_ti_with_none_as_logical_date",
            logical_date=timezone.datetime(2025, 1, 17),
            run_type="scheduled",
            state=State.SUCCESS,
            session=session,
        )
        dr1.end_date = timezone.datetime(2025, 1, 17, 1, 0, 0)

        session.commit()

        response = client.get(f"/execution/task-instances/{ti.id}/previous-successful-dagrun")
        assert response.status_code == 200
        assert response.json() == {
            "data_interval_start": None,
            "data_interval_end": None,
            "start_date": None,
            "end_date": None,
        }


class TestGetRescheduleStartDate:
    def test_get_start_date(self, client, session, create_task_instance):
        ti = create_task_instance(
            task_id="test_ti_update_state_reschedule_mysql_limit",
            state=State.RUNNING,
            start_date=timezone.datetime(2024, 1, 1),
            session=session,
        )
        tr = TaskReschedule(
            ti_id=ti.id,
            start_date=timezone.datetime(2024, 1, 1),
            end_date=timezone.datetime(2024, 1, 1, 1),
            reschedule_date=timezone.datetime(2024, 1, 1, 2),
        )
        session.add(tr)
        session.commit()

        response = client.get(f"/execution/task-reschedules/{ti.id}/start_date")
        assert response.status_code == 200
        assert response.json() == "2024-01-01T00:00:00Z"

    def test_get_start_date_not_found(self, client):
        ti_id = "0182e924-0f1e-77e6-ab50-e977118bc139"
        response = client.get(f"/execution/task-reschedules/{ti_id}/start_date")
        assert response.json() is None


class TestGetCount:
    def setup_method(self):
        clear_db_runs()

    def teardown_method(self):
        clear_db_runs()

    def test_get_count_basic(self, client, session, create_task_instance):
        create_task_instance(task_id="test_task", state=State.SUCCESS)
        session.commit()

        response = client.get("/execution/task-instances/count", params={"dag_id": "dag"})
        assert response.status_code == 200
        assert response.json() == 1

    def test_get_count_with_task_ids(self, client, session, create_task_instance):
        for i in range(3):
            create_task_instance(
                task_id=f"task{i}",
                state=State.SUCCESS,
                dag_id="test_get_count_with_task_ids",
                run_id=f"test_run_id{i}",
            )
        session.commit()

        response = client.get(
            "/execution/task-instances/count",
            params={"dag_id": "test_get_count_with_task_ids", "task_ids": ["task1", "task2"]},
        )
        assert response.status_code == 200
        assert response.json() == 2

    def test_get_count_with_states(self, client, session, dag_maker):
        """Test counting tasks in specific states."""
        with dag_maker("test_get_count_with_states"):
            EmptyOperator(task_id="task1")
            EmptyOperator(task_id="task2")
            EmptyOperator(task_id="task3")

        dr = dag_maker.create_dagrun()

        tis = dr.get_task_instances()

        # Set different states for the task instances
        for ti, state in zip(tis, [State.SUCCESS, State.FAILED, State.SKIPPED]):
            ti.state = state
            session.merge(ti)
        session.commit()

        response = client.get(
            "/execution/task-instances/count",
            params={"dag_id": "test_get_count_with_states", "states": [State.SUCCESS, State.FAILED]},
        )
        assert response.status_code == 200
        assert response.json() == 2

    def test_get_count_with_logical_dates(self, client, session, dag_maker):
        with dag_maker("test_get_count_with_logical_dates"):
            EmptyOperator(task_id="task1")

        date1 = timezone.datetime(2025, 1, 1)
        date2 = timezone.datetime(2025, 1, 2)

        dag_maker.create_dagrun(run_id="test_run_id1", logical_date=date1)
        dag_maker.create_dagrun(run_id="test_run_id2", logical_date=date2)

        session.commit()

        response = client.get(
            "/execution/task-instances/count",
            params={
                "dag_id": "test_get_count_with_logical_dates",
                "logical_dates": [date1.isoformat(), date2.isoformat()],
            },
        )
        assert response.status_code == 200
        assert response.json() == 2

    def test_get_count_with_run_ids(self, client, session, dag_maker):
        with dag_maker("test_get_count_with_run_ids"):
            EmptyOperator(task_id="task1")

        dag_maker.create_dagrun(run_id="run1", logical_date=timezone.datetime(2025, 1, 1))
        dag_maker.create_dagrun(run_id="run2", logical_date=timezone.datetime(2025, 1, 2))

        session.commit()

        response = client.get(
            "/execution/task-instances/count",
            params={"dag_id": "test_get_count_with_run_ids", "run_ids": ["run1", "run2"]},
        )
        assert response.status_code == 200
        assert response.json() == 2

    def test_get_count_with_task_group(self, client, session, dag_maker):
        with dag_maker(dag_id="test_dag", serialized=True):
            with TaskGroup("group1"):
                EmptyOperator(task_id="task1")
                EmptyOperator(task_id="task2")

            with TaskGroup("group2"):
                EmptyOperator(task_id="task3")

        dag_maker.create_dagrun(session=session)
        session.commit()

        response = client.get(
            "/execution/task-instances/count",
            params={"dag_id": "test_dag", "task_group_id": "group1"},
        )
        assert response.status_code == 200
        assert response.json() == 2

    def test_get_count_task_group_not_found(self, client, session, dag_maker):
        with dag_maker(dag_id="test_get_count_task_group_not_found", serialized=True):
            with TaskGroup("group1"):
                EmptyOperator(task_id="task1")
        dag_maker.create_dagrun(session=session)

        response = client.get(
            "/execution/task-instances/count",
            params={"dag_id": "test_get_count_task_group_not_found", "task_group_id": "non_existent_group"},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == {
            "reason": "not_found",
            "message": "Task group non_existent_group not found in DAG test_get_count_task_group_not_found",
        }

    def test_get_count_dag_not_found(self, client, session):
        response = client.get(
            "/execution/task-instances/count",
            params={"dag_id": "non_existent_dag", "task_group_id": "group1"},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == {
            "reason": "not_found",
            "message": "The Dag with ID: `non_existent_dag` was not found",
        }

    def test_get_count_with_none_state(self, client, session, create_task_instance):
        create_task_instance(task_id="task1", dag_id="get_count_with_none", state=None)
        session.commit()

        response = client.get(
            "/execution/task-instances/count",
            params={"dag_id": "get_count_with_none", "states": ["null"]},
        )
        assert response.status_code == 200
        assert response.json() == 1

    def test_get_count_with_mixed_states(self, client, session, create_task_instance):
        create_task_instance(task_id="task1", state=State.SUCCESS, run_id="runid1", dag_id="mixed_states")
        create_task_instance(task_id="task2", state=None, run_id="runid2", dag_id="mixed_states")
        session.commit()

        response = client.get(
            "/execution/task-instances/count",
            params={"dag_id": "mixed_states", "states": [State.SUCCESS, "null"]},
        )
        assert response.status_code == 200
        assert response.json() == 2

    def test_get_count_with_map_index_less_than_zero(self, client, session, create_task_instance):
        create_task_instance(task_id="task1", state=State.SUCCESS, run_id="runid1", dag_id="map_index_test")
        session.commit()

        response = client.get(
            "/execution/task-instances/count",
            params={"dag_id": "map_index_test", "states": [State.SUCCESS], "map_index": -1},
        )
        assert response.status_code == 200
        assert response.json() == 1

    def test_get_count_with_multiple_tasks_and_map_index_less_than_zero(
        self, dag_maker, client, session, create_task_instance
    ):
        with dag_maker("test_get_count_with_multiple_tasks_and_map_index_less_than_zero"):
            EmptyOperator(task_id="task1")
            EmptyOperator(task_id="task2")
            EmptyOperator(task_id="task3")

        dr = dag_maker.create_dagrun()

        tis = dr.get_task_instances()

        # Set different states for the task instances
        for ti, state in zip(tis, [State.SUCCESS, State.FAILED, State.SKIPPED]):
            ti.state = state
            session.merge(ti)
        session.commit()

        response = client.get(
            "/execution/task-instances/count",
            params={
                "dag_id": "test_get_count_with_multiple_tasks_and_map_index_less_than_zero",
                "map_index": -1,
            },
        )
        assert response.status_code == 200
        assert response.json() == 3

    @pytest.mark.parametrize(
        ["map_index", "dynamic_task_args", "expected_count"],
        (
            pytest.param(None, [1, 2, 3], 4, id="use-default-map-index"),
            pytest.param(-1, [1, 2, 3], 1, id="map-index-(-1)"),
            pytest.param(0, [1, 2, 3], 1, id="map-index-0"),
            pytest.param(1, [1, 2, 3], 1, id="map-index-1"),
            pytest.param(2, [1, 2, 3], 1, id="map-index-2"),
        ),
    )
    def test_get_count_for_dynamic_task_mapping(
        self, dag_maker, client, session, map_index, dynamic_task_args, expected_count
    ):
        """
        case 1: map_index is None, it should fetch all the tasks
        other cases: when map index is provided, it should return the count of tasks that falls under the map index
        """
        with dag_maker(session=session) as dag:
            EmptyOperator(task_id="op1")

            @dag.task()
            def add_one(x):
                return [x + 1]

            add_one.expand(x=dynamic_task_args)

        dr = dag_maker.create_dagrun()

        tis = dr.get_task_instances()

        for ti in tis:
            ti.state = State.SUCCESS
            session.merge(ti)
        session.commit()

        map_index = {} if map_index is None else {"map_index": map_index}

        response = client.get(
            "/execution/task-instances/count",
            params={"dag_id": dr.dag_id, "run_ids": [dr.run_id], **map_index},
        )
        assert response.status_code == 200
        assert response.json() == expected_count

    @pytest.mark.parametrize(
        [
            "map_index",
            "dynamic_task_args",
            "task_ids",
            "task_group_name",
            "expected_count",
        ],
        (
            pytest.param(None, [1, 2, 3], None, None, 5, id="use-default-map-index-None"),
            pytest.param(-1, [1, 2, 3], ["task1"], None, 1, id="with-task-ids-and-map-index-(-1)"),
            pytest.param(None, [1, 2, 3], None, "group1", 4, id="with-task-group-id-and-map-index-None"),
            pytest.param(0, [1, 2, 3], None, "group1", 1, id="with-task-group-id-and-map-index-0"),
            pytest.param(-1, [1, 2, 3], None, "group1", 1, id="with-task-group-id-and-map-index-(-1)"),
        ),
    )
    def test_get_count_mix_of_task_and_task_group_dynamic_task_mapping(
        self,
        dag_maker,
        client,
        session,
        map_index,
        dynamic_task_args,
        task_ids,
        task_group_name,
        expected_count,
    ):
        """
        case 1: map_index is None, task_ids is None, task_group_name is None, it should fetch all the tasks
        case 2: when map index -1 and provided task_ids, it should return the count of task_ids
        case 3: when map index is None and provided task_group_id, it should return the count of tasks under the task group
        case 4: when map index is 0 and provided task_group_id, it should return the count of tasks under the task group that falls map index =0
        case 5: when map index is -1 and provided task_group_id, it should return the count of tasks under the task group that falls map index =-1 i.e this task is not mapped
        """

        with dag_maker(session=session, serialized=True) as dag:
            EmptyOperator(task_id="task1")

            with TaskGroup("group1"):

                @dag.task()
                def add_one(x):
                    return [x + 1]

                add_one.expand(x=dynamic_task_args)

                EmptyOperator(task_id="task2")

        dr = dag_maker.create_dagrun(session=session)

        session.commit()
        params = {}

        if task_ids:
            params["task_ids"] = task_ids
        if task_group_name:
            params["task_group_id"] = task_group_name
        if map_index is not None:
            params["map_index"] = map_index

        response = client.get(
            "/execution/task-instances/count",
            params={"dag_id": dr.dag_id, "run_ids": [dr.run_id], **params},
        )
        assert response.status_code == 200
        assert response.json() == expected_count


class TestGetTaskStates:
    def setup_method(self):
        clear_db_runs()

    def teardown_method(self):
        clear_db_runs()

    def test_get_task_states_basic(self, client, session, create_task_instance):
        create_task_instance(task_id="test_task", state=State.SUCCESS)
        session.commit()

        response = client.get("/execution/task-instances/states", params={"dag_id": "dag"})
        assert response.status_code == 200
        assert response.json() == {"task_states": {"test": {"test_task": "success"}}}

    def test_get_task_states_group_id_basic(self, client, dag_maker, session):
        with dag_maker(dag_id="test_dag", serialized=True):
            with TaskGroup("group1"):
                EmptyOperator(task_id="task1")

        dag_maker.create_dagrun(session=session)
        session.commit()

        response = client.get(
            "/execution/task-instances/states",
            params={"dag_id": "test_dag", "task_group_id": "group1"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "task_states": {
                "test": {
                    "group1.task1": None,
                },
            },
        }

    def test_get_task_states_with_task_group_id_and_task_id(self, client, session, dag_maker):
        with dag_maker("test_get_task_group_states_with_multiple_task_tasks", serialized=True):
            with TaskGroup("group1"):
                EmptyOperator(task_id="task1")
            EmptyOperator(task_id="task2")

        dr = dag_maker.create_dagrun()

        tis = dr.get_task_instances()

        # Set different states for the task instances
        for ti, state in zip(tis, [State.SUCCESS, State.FAILED]):
            ti.state = state
            session.merge(ti)
        session.commit()

        response = client.get(
            "/execution/task-instances/states",
            params={
                "dag_id": "test_get_task_group_states_with_multiple_task_tasks",
                "task_group_id": "group1",
            },
        )
        assert response.status_code == 200
        assert response.json() == {
            "task_states": {
                "test": {
                    "group1.task1": "success",
                },
            },
        }

    def test_get_task_group_states_with_multiple_task(self, client, session, dag_maker):
        with dag_maker("test_get_task_group_states_with_multiple_task_tasks", serialized=True):
            with TaskGroup("group1"):
                EmptyOperator(task_id="task1")
                EmptyOperator(task_id="task2")
                EmptyOperator(task_id="task3")

        dr = dag_maker.create_dagrun()

        tis = dr.get_task_instances()

        # Set different states for the task instances
        for ti, state in zip(tis, [State.SUCCESS, State.FAILED, State.SKIPPED]):
            ti.state = state
            session.merge(ti)
        session.commit()

        response = client.get(
            "/execution/task-instances/states",
            params={
                "dag_id": "test_get_task_group_states_with_multiple_task_tasks",
                "task_group_id": "group1",
            },
        )
        assert response.status_code == 200
        assert response.json() == {
            "task_states": {
                "test": {
                    "group1.task1": "success",
                    "group1.task2": "failed",
                    "group1.task3": "skipped",
                },
            },
        }

    def test_get_task_group_states_with_logical_dates(self, client, session, dag_maker):
        with dag_maker("test_get_task_group_states_with_logical_dates", serialized=True):
            with TaskGroup("group1"):
                EmptyOperator(task_id="task1")

        date1 = timezone.datetime(2025, 1, 1)
        date2 = timezone.datetime(2025, 1, 2)

        dag_maker.create_dagrun(run_id="test_run_id1", logical_date=date1)
        dag_maker.create_dagrun(run_id="test_run_id2", logical_date=date2)

        session.commit()

        response = client.get(
            "/execution/task-instances/states",
            params={
                "dag_id": "test_get_task_group_states_with_logical_dates",
                "logical_dates": [date1.isoformat(), date2.isoformat()],
                "task_group_id": "group1",
            },
        )
        assert response.status_code == 200
        assert response.json() == {
            "task_states": {
                "test_run_id1": {
                    "group1.task1": None,
                },
                "test_run_id2": {
                    "group1.task1": None,
                },
            },
        }

    def test_get_task_group_states_with_run_ids(self, client, session, dag_maker):
        with dag_maker("test_get_task_group_states_with_run_ids", serialized=True):
            with TaskGroup("group1"):
                EmptyOperator(task_id="task1")

        dag_maker.create_dagrun(run_id="run1", logical_date=timezone.datetime(2025, 1, 1))
        dag_maker.create_dagrun(run_id="run2", logical_date=timezone.datetime(2025, 1, 2))

        session.commit()

        response = client.get(
            "/execution/task-instances/states",
            params={
                "dag_id": "test_get_task_group_states_with_run_ids",
                "run_ids": ["run1", "run2"],
                "task_group_id": "group1",
            },
        )
        assert response.status_code == 200
        assert response.json() == {
            "task_states": {
                "run1": {
                    "group1.task1": None,
                },
                "run2": {
                    "group1.task1": None,
                },
            },
        }

    def test_get_task_states_task_group_not_found(self, client, session, dag_maker):
        with dag_maker(dag_id="test_get_task_states_task_group_not_found", serialized=True):
            with TaskGroup("group1"):
                EmptyOperator(task_id="task1")
        dag_maker.create_dagrun(session=session)

        response = client.get(
            "/execution/task-instances/states",
            params={
                "dag_id": "test_get_task_states_task_group_not_found",
                "task_group_id": "non_existent_group",
            },
        )
        assert response.status_code == 404
        assert response.json()["detail"] == {
            "reason": "not_found",
            "message": "Task group non_existent_group not found in DAG test_get_task_states_task_group_not_found",
        }

    def test_get_task_states_dag_not_found(self, client, session):
        response = client.get(
            "/execution/task-instances/states",
            params={"dag_id": "non_existent_dag", "task_group_id": "group1"},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == {
            "reason": "not_found",
            "message": "The Dag with ID: `non_existent_dag` was not found",
        }

    @pytest.mark.parametrize(
        ["map_index", "dynamic_task_args", "states", "expected"],
        (
            pytest.param(
                None,
                [1, 2, 3],
                {"-1": State.SUCCESS, "0": State.SUCCESS, "1": State.SUCCESS, "2": State.SUCCESS},
                {"task1": "success", "add_one_0": "success", "add_one_1": "success", "add_one_2": "success"},
                id="with-default-map-index-None",
            ),
            pytest.param(
                0,
                [1, 2, 3],
                {"-1": State.SUCCESS, "0": State.FAILED, "1": State.SUCCESS, "2": State.SUCCESS},
                {"add_one_0": "failed"},
                id="with-map-index-0",
            ),
            pytest.param(
                1,
                [1, 2, 3],
                {"-1": State.SUCCESS, "0": State.SUCCESS, "1": State.FAILED, "2": State.SUCCESS},
                {"add_one_1": "failed"},
                id="with-map-index-1",
            ),
        ),
    )
    def test_get_task_states_for_dynamic_task_mapping(
        self, dag_maker, client, session, map_index, dynamic_task_args, states, expected
    ):
        """
        case 1: map_index is None, it should fetch all the tasks
        other cases: when map index is provided, it should return the count of tasks that falls under the map index
        """
        with dag_maker(session=session, serialized=True) as dag:
            EmptyOperator(task_id="task1")

            @dag.task()
            def add_one(x):
                return [x + 1]

            add_one.expand(x=dynamic_task_args)

        dr = dag_maker.create_dagrun()

        tis = dr.get_task_instances()
        for ti in tis:
            ti.state = states.get(str(ti.map_index))
            session.merge(ti)
        session.commit()

        map_index = {} if map_index is None else {"map_index": map_index}

        response = client.get("/execution/task-instances/states", params={"dag_id": dr.dag_id, **map_index})
        assert response.status_code == 200
        assert response.json() == {"task_states": {dr.run_id: expected}}

    @pytest.mark.parametrize(
        [
            "map_index",
            "dynamic_task_args",
            "task_ids",
            "task_group_name",
            "states",
            "expected",
        ],
        (
            pytest.param(
                None,
                [1, 2, 3],
                None,
                None,
                {"-1": State.SUCCESS, "0": State.SUCCESS, "1": State.SUCCESS, "2": State.SUCCESS},
                {
                    "group1.add_one_0": "success",
                    "group1.add_one_1": "success",
                    "group1.add_one_2": "success",
                    "group1.task2": "success",
                    "task1": "success",
                },
                id="with-default-map-index-None",
            ),
            pytest.param(
                -1,
                [1, 2, 3],
                ["task1"],
                None,
                {"-1": State.SUCCESS, "0": State.SUCCESS, "1": State.SUCCESS, "2": State.SUCCESS},
                {"task1": "success"},
                id="with-task-ids-map-index-(-1)",
            ),
            pytest.param(
                None,
                [1, 2, 3],
                None,
                "group1",
                {"-1": State.SUCCESS, "0": State.SUCCESS, "1": State.SUCCESS, "2": State.SUCCESS},
                {
                    "group1.task2": "success",
                    "group1.add_one_0": "success",
                    "group1.add_one_1": "success",
                    "group1.add_one_2": "success",
                },
                id="with-task-group-id-and-map-index-None",
            ),
            pytest.param(
                0,
                [1, 2, 3],
                None,
                "group1",
                {"-1": State.SUCCESS, "0": State.FAILED, "1": State.SUCCESS, "2": State.SUCCESS},
                {"group1.add_one_0": "failed"},
                id="with-task-group-id-and-map-index-0",
            ),
            pytest.param(
                -1,
                [1, 2, 3],
                ["task1"],
                "group1",
                {"-1": State.SUCCESS, "0": State.SUCCESS, "1": State.SUCCESS, "2": State.SUCCESS},
                {"task1": "success", "group1.task2": "success"},
                id="with-task-id-and-task-group-map-index-(-1)",
            ),
        ),
    )
    def test_get_task_states_mix_of_task_and_task_group_dynamic_task_mapping(
        self,
        dag_maker,
        client,
        session,
        map_index,
        dynamic_task_args,
        task_ids,
        task_group_name,
        states,
        expected,
    ):
        """
        case1: map_index is None, task_ids is None, task_group_name is None, it should fetch all the task states
        case2: when map index -1 and provided task_ids, it should return the task states of task_ids
        case3: when map index is None and provided task_group_id, it should return the task states of tasks under the task group and normal task states under task group
        case4: when map index is 0 and provided task_group_id, it should return the task states of tasks under the task group that falls under map index = 0
        case5: when map index is -1 and provided both task_id and task_group_id, it should return the task states of tasks under the task group that falls under map index = -1 and normal task_ids states
        """

        with dag_maker(session=session, serialized=True) as dag:
            EmptyOperator(task_id="task1")

            with TaskGroup("group1"):

                @dag.task()
                def add_one(x):
                    return [x + 1]

                add_one.expand(x=dynamic_task_args)

                EmptyOperator(task_id="task2")

        dr = dag_maker.create_dagrun(session=session)

        tis = dr.get_task_instances()
        for ti in tis:
            ti.state = states.get(str(ti.map_index))
            session.merge(ti)
        session.commit()
        params = {}

        if task_ids:
            params["task_ids"] = task_ids
        if task_group_name:
            params["task_group_id"] = task_group_name
        if map_index is not None:
            params["map_index"] = map_index

        response = client.get("/execution/task-instances/states", params={"dag_id": dr.dag_id, **params})
        assert response.status_code == 200
        assert response.json() == {"task_states": {dr.run_id: expected}}


class TestInvactiveInletsAndOutlets:
    @pytest.mark.parametrize(
        "logical_date",
        [
            datetime(2025, 6, 6, tzinfo=timezone.utc),
            None,
        ],
    )
    def test_ti_inactive_inlets_and_outlets(self, logical_date, client, dag_maker):
        """Test the inactive assets in inlets and outlets can be found."""
        with dag_maker("test_inlets_and_outlets"):
            EmptyOperator(
                task_id="task1",
                inlets=[Asset(name="inlet-name"), Asset(name="inlet-name", uri="but-different-uri")],
                outlets=[
                    Asset(name="outlet-name", uri="uri"),
                    Asset(name="outlet-name", uri="second-different-uri"),
                ],
            )

        dr = dag_maker.create_dagrun(logical_date=logical_date)

        task1_ti = dr.get_task_instance("task1")
        response = client.get(f"/execution/task-instances/{task1_ti.id}/validate-inlets-and-outlets")
        assert response.status_code == 200
        inactive_assets = response.json()["inactive_assets"]
        expected_inactive_assets = (
            {
                "name": "inlet-name",
                "type": "Asset",
                "uri": "but-different-uri",
            },
            {
                "name": "outlet-name",
                "type": "Asset",
                "uri": "second-different-uri",
            },
        )
        for asset in expected_inactive_assets:
            assert asset in inactive_assets

    @pytest.mark.parametrize(
        "logical_date",
        [
            datetime(2025, 6, 6, tzinfo=timezone.utc),
            None,
        ],
    )
    def test_ti_inactive_inlets_and_outlets_without_inactive_assets(self, logical_date, client, dag_maker):
        """Test the task without inactive assets in its inlets or outlets returns empty list."""
        with dag_maker("test_inlets_and_outlets_inactive"):
            EmptyOperator(
                task_id="inactive_task1",
                inlets=[Asset(name="inlet-name")],
                outlets=[Asset(name="outlet-name", uri="uri")],
            )

        dr = dag_maker.create_dagrun(logical_date=logical_date)

        task1_ti = dr.get_task_instance("inactive_task1")
        response = client.get(f"/execution/task-instances/{task1_ti.id}/validate-inlets-and-outlets")
        assert response.status_code == 200
        assert response.json() == {"inactive_assets": []}
