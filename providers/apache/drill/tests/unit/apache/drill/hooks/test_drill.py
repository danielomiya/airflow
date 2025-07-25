#
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

from unittest.mock import MagicMock, patch

import pytest

from airflow.providers.apache.drill.hooks.drill import DrillHook


@pytest.mark.parametrize("host, expect_error", [("host_with?", True), ("good_host", False)])
def test_get_host(host, expect_error):
    with (
        patch("airflow.providers.apache.drill.hooks.drill.DrillHook.get_connection") as mock_get_connection,
        patch("sqlalchemy.engine.base.Engine.raw_connection") as raw_connection,
    ):
        raw_connection.return_value = MagicMock()
        mock_get_connection.return_value = MagicMock(
            host=host, port=80, login="drill_user", password="secret"
        )
        mock_get_connection.return_value.extra_dejson = {
            "dialect_driver": "drill+sadrill",
            "storage_plugin": "dfs",
        }
        if expect_error:
            with pytest.raises(ValueError, match=r"Drill database_url should not contain a '\?'"):
                DrillHook().get_conn()
        else:
            assert DrillHook().get_conn()


class TestDrillHook:
    def setup_method(self):
        self.cur = MagicMock(rowcount=0)
        self.conn = conn = MagicMock()
        self.conn.login = "drill_user"
        self.conn.password = "secret"
        self.conn.host = "host"
        self.conn.port = "8047"
        self.conn.conn_type = "drill"
        self.conn.extra_dejson = {"dialect_driver": "drill+sadrill", "storage_plugin": "dfs"}
        self.conn.cursor.return_value = self.cur

        class TestDrillHook(DrillHook):
            def get_conn(self):
                return conn

            def get_connection(self, conn_id):
                return conn

        self.db_hook = TestDrillHook

    @pytest.mark.parametrize(
        "host, port, conn_type, extra_dejson, expected_uri",
        [
            (
                "host",
                "8047",
                "drill",
                {"dialect_driver": "drill+sadrill", "storage_plugin": "dfs"},
                "drill://host:8047/dfs?dialect_driver=drill+sadrill",
            ),
            (
                "host",
                None,
                "drill",
                {"dialect_driver": "drill+sadrill", "storage_plugin": "dfs"},
                "drill://host/dfs?dialect_driver=drill+sadrill",
            ),
            (
                "host",
                "8047",
                None,
                {"dialect_driver": "drill+sadrill", "storage_plugin": "dfs"},
                "drill://host:8047/dfs?dialect_driver=drill+sadrill",
            ),
            (
                "host",
                "8047",
                "drill",
                {},  # no extra_dejson fields
                "drill://host:8047/dfs?dialect_driver=drill+sadrill",
            ),
            (
                "myhost",
                1234,
                "custom",
                {"dialect_driver": "mydriver", "storage_plugin": "myplugin"},
                "custom://myhost:1234/myplugin?dialect_driver=mydriver",
            ),
            (
                "host",
                "8047",
                "drill",
                {"storage_plugin": "myplugin"},
                "drill://host:8047/myplugin?dialect_driver=drill+sadrill",
            ),
            (
                "host",
                "8047",
                "drill",
                {"dialect_driver": "mydriver"},
                "drill://host:8047/dfs?dialect_driver=mydriver",
            ),
        ],
    )
    def test_get_uri(self, host, port, conn_type, extra_dejson, expected_uri):
        self.conn.host = host
        self.conn.port = port
        self.conn.conn_type = conn_type
        self.conn.extra_dejson = extra_dejson

        db_hook = self.db_hook()
        assert db_hook.get_uri() == expected_uri

    def test_get_first_record(self):
        statement = "SQL"
        result_sets = [("row1",), ("row2",)]
        self.cur.fetchone.return_value = result_sets[0]

        assert result_sets[0] == self.db_hook().get_first(statement)
        assert self.conn.close.call_count == 1
        assert self.cur.close.call_count == 1
        self.cur.execute.assert_called_once_with(statement)

    def test_get_records(self):
        statement = "SQL"
        result_sets = [("row1",), ("row2",)]
        self.cur.fetchall.return_value = result_sets

        assert result_sets == self.db_hook().get_records(statement)
        assert self.conn.close.call_count == 1
        assert self.cur.close.call_count == 1
        self.cur.execute.assert_called_once_with(statement)

    def test_get_df_pandas(self):
        statement = "SQL"
        column = "col"
        result_sets = [("row1",), ("row2",)]
        self.cur.description = [(column,)]
        self.cur.fetchall.return_value = result_sets
        df = self.db_hook().get_df(statement, df_type="pandas")

        assert column == df.columns[0]
        for i, item in enumerate(result_sets):
            assert item[0] == df.values.tolist()[i][0]
        assert self.conn.close.call_count == 1
        assert self.cur.close.call_count == 1
        self.cur.execute.assert_called_once_with(statement)

    def test_get_df_polars(self):
        statement = "SQL"
        column = "col"
        result_sets = [("row1",), ("row2",)]
        mock_execute = MagicMock()
        mock_execute.description = [(column, None, None, None, None, None, None)]
        mock_execute.fetchall.return_value = result_sets
        self.cur.execute.return_value = mock_execute
        df = self.db_hook().get_df(statement, df_type="polars")

        self.cur.execute.assert_called_once_with(statement)
        mock_execute.fetchall.assert_called_once_with()
        assert column == df.columns[0]
        assert result_sets[0][0] == df.row(0)[0]
        assert result_sets[1][0] == df.row(1)[0]

    def test_set_autocommit_raises_not_implemented(self):
        db_hook = self.db_hook()
        conn = db_hook.get_conn()

        with pytest.raises(NotImplementedError, match=r"There are no transactions in Drill."):
            db_hook.set_autocommit(conn=conn, autocommit=True)

    def test_insert_rows_raises_not_implemented(self):
        db_hook = self.db_hook()
        with pytest.raises(NotImplementedError, match=r"There is no INSERT statement in Drill."):
            db_hook.insert_rows(table="my_table", rows=[("a",)])
