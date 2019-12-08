"""Unit tests for //deeplearning/ml4pl/models:log_database."""
import random
from typing import NamedTuple
from typing import Tuple

import numpy as np
import pandas as pd
import sqlalchemy as sql

from deeplearning.ml4pl import run_id
from deeplearning.ml4pl.graphs.labelled import graph_tuple_database
from deeplearning.ml4pl.models import log_database
from deeplearning.ml4pl.testing import random_graph_tuple_database_generator
from deeplearning.ml4pl.testing import random_log_database_generator
from deeplearning.ml4pl.testing import testing_databases
from labm8.py import app
from labm8.py import test

FLAGS = app.FLAGS

###############################################################################
# Fixtures and mocks.
###############################################################################


@test.Fixture(scope="function", params=testing_databases.GetDatabaseUrls())
def db(request) -> log_database.Database:
  """A test fixture which yields an empty log database."""
  yield from testing_databases.YieldDatabase(
    log_database.Database, request.param
  )


@test.Fixture(scope="function")
def db_session(db: log_database.Database) -> log_database.Database.SessionType:
  with db.Session() as session:
    yield session


@test.Fixture(scope="session", params=((0, 2), (2, 0)))
def y_dimensionalities(request) -> Tuple[int, int]:
  """A test fixture which enumerates node and graph label dimensionalities."""
  return request.param


@test.Fixture(scope="session", params=testing_databases.GetDatabaseUrls())
def populated_graph_db(
  request, y_dimensionalities: Tuple[int, int]
) -> graph_tuple_database.Database:
  """Test fixture which returns a populated graph database."""
  node_y_dimensionality, graph_y_dimensionality = y_dimensionalities
  with testing_databases.DatabaseContext(
    graph_tuple_database.Database, request.param
  ) as db:
    random_graph_tuple_database_generator.PopulateDatabaseWithRandomGraphTuples(
      db,
      graph_count=100,
      node_y_dimensionality=node_y_dimensionality,
      graph_y_dimensionality=graph_y_dimensionality,
    )
    yield db


@test.Fixture(scope="session")
def generator(
  populated_graph_db: graph_tuple_database.Database,
) -> random_log_database_generator.RandomLogDatabaseGenerator:
  """A test fixture which returns a log generator."""
  return random_log_database_generator.RandomLogDatabaseGenerator(
    populated_graph_db
  )


@test.Fixture(scope="session", params=testing_databases.GetDatabaseUrls())
def populated_log_db(
  request, generator: random_log_database_generator.RandomLogDatabaseGenerator
) -> log_database.Database:
  """A test fixture which yields an empty log database."""
  with testing_databases.DatabaseContext(
    log_database.Database, request.param
  ) as db:
    db._run_ids = generator.PopulateLogDatabase(db, run_count=10)
    yield db


class DatabaseSessionWithRunLogs(NamedTuple):
  """Tuple for a test fixture which returns a database session with run logs."""

  session: log_database.Database.SessionType
  a: log_database.RunLogs
  b: log_database.RunLogs


@test.Fixture(scope="function")
def two_run_id_session(
  db: log_database.Database,
  generator: random_log_database_generator.RandomLogDatabaseGenerator,
) -> log_database.Database.SessionType:
  """A test fixture which yields a database with two runs."""
  a = generator.CreateRandomRunLogs(run_id=run_id.RunId.GenerateUnique("a"))
  b = generator.CreateRandomRunLogs(run_id=run_id.RunId.GenerateUnique("b"))

  with db.Session() as session:
    session.add_all(a.all + b.all)
    yield DatabaseSessionWithRunLogs(session=session, a=a, b=b)


###############################################################################
# Tests.
###############################################################################


def test_RunId_add_one(db_session: log_database.Database.SessionType):
  """Test adding a run ID to the database."""
  run = run_id.RunId.GenerateUnique("test")
  db_session.add(log_database.RunId(run_id=str(run)))
  db_session.commit()


def test_Parameter_CreateManyFromDict(
  db_session: log_database.Database.SessionType,
):
  run = run_id.RunId.GenerateUnique("test")
  params = log_database.Parameter.CreateManyFromDict(
    run_id=run,
    type=log_database.ParameterType.BUILD_INFO,
    parameters={"a": 1, "b": "foo",},
  )

  assert len(params) == 2
  for param in params:
    assert param.run_id == run
    assert param.type == log_database.ParameterType.BUILD_INFO
    assert param.name in {"a", "b"}
    assert param.value in {1, "foo"}
  db_session.add_all([log_database.RunId(run_id=str(run))] + params)
  db_session.commit()


def test_Batch_cascaded_delete(two_run_id_session: DatabaseSessionWithRunLogs):
  """Test cascaded delete of detailed batch logs."""
  session = two_run_id_session.session

  # Sanity check that there are detailed batches.
  assert (
    session.query(sql.func.count(log_database.Batch.id))
    .join(log_database.BatchDetails)
    .filter(log_database.Batch.run_id == str(two_run_id_session.a.run_id))
    .scalar()
    >= 1
  )

  session.query(log_database.Batch).filter(
    log_database.Batch.run_id == str(two_run_id_session.a.run_id)
  ).delete()
  session.commit()

  assert session.query(
    sql.func.distinct(log_database.Batch.run_id)
  ).scalar() == str(two_run_id_session.b.run_id)

  assert (
    session.query(sql.func.count(log_database.Batch.id))
    .join(log_database.BatchDetails)
    .filter(log_database.Batch.run_id == str(two_run_id_session.a.run_id))
    .scalar()
    == 0
  )


def test_RunId_cascaded_delete(two_run_id_session: DatabaseSessionWithRunLogs):
  """Test the deleting the RunId deletes all other entries."""
  session = two_run_id_session.session

  # Sanity check that there are params and batches.
  assert (
    session.query(sql.func.count(log_database.Parameter.id))
    .filter(log_database.Parameter.run_id == str(two_run_id_session.a.run_id))
    .scalar()
    >= 1
  )
  assert (
    session.query(sql.func.count(log_database.Batch.id))
    .filter(log_database.Batch.run_id == str(two_run_id_session.a.run_id))
    .scalar()
    >= 1
  )

  session.query(log_database.RunId).filter(
    log_database.RunId.run_id == str(two_run_id_session.a.run_id.run_id)
  ).delete()
  session.commit()

  assert session.query(
    sql.func.distinct(log_database.Batch.run_id)
  ).scalar() == str(two_run_id_session.b.run_id)

  assert (
    session.query(sql.func.count(log_database.Parameter.id))
    .filter(log_database.Parameter.run_id == str(two_run_id_session.a.run_id))
    .scalar()
    == 0
  )
  assert (
    session.query(sql.func.count(log_database.Batch.id))
    .filter(log_database.Batch.run_id == str(two_run_id_session.a.run_id))
    .scalar()
    == 0
  )
  assert (
    session.query(sql.func.count(log_database.Checkpoint.id))
    .filter(log_database.Checkpoint.run_id == str(two_run_id_session.a.run_id))
    .scalar()
    == 0
  )


@test.Parametrize("eager_batch_details", (False, True))
@test.Parametrize("eager_checkpoint_data", (False, True))
def test_GetRunLogs(
  populated_log_db: log_database.Database,
  eager_batch_details: bool,
  eager_checkpoint_data: bool,
):
  """Test that run logs are returned."""
  run_id = random.choice(populated_log_db._run_ids)
  run_logs = populated_log_db.GetRunLogs(
    run_id,
    eager_batch_details=eager_batch_details,
    eager_checkpoint_data=eager_checkpoint_data,
  )
  assert isinstance(run_logs, log_database.RunLogs)


def test_GetRunLogs_invalid_run_id(populated_log_db: log_database.Database):
  """Test that error is raised when run not found."""
  with test.Raises(ValueError):
    populated_log_db.GetRunLogs("foo")


def test_GetRunParameters(populated_log_db: log_database.Database):
  """Test that parameters are returned."""
  run_id = random.choice(populated_log_db._run_ids)
  assert isinstance(populated_log_db.GetRunParameters(run_id), pd.DataFrame)


def test_GetRunParameters_invalid_run_id(
  populated_log_db: log_database.Database,
):
  """Test that error is raised when run not found."""
  with test.Raises(ValueError):
    populated_log_db.GetRunParameters("foo")


def test_GetBestResults(populated_log_db: log_database.Database):
  """Test that run logs are returned."""
  run_id = random.choice(populated_log_db._run_ids)
  assert populated_log_db.GetBestResults(run_id)


def test_GetBestResults_invalid_run_id(populated_log_db: log_database.Database):
  """Test that error is raised when run not found."""
  with test.Raises(ValueError):
    populated_log_db.GetBestResults("foo")


def test_GetModelConstructorArgs(populated_log_db: log_database.Database):
  """Test that run logs are returned."""
  run_id = random.choice(populated_log_db._run_ids)
  assert populated_log_db.GetModelConstructorArgs(run_id)


def test_GetModelConstructorArgs_invalid_run_id(
  populated_log_db: log_database.Database,
):
  """Test that error is raised when run not found."""
  with test.Raises(ValueError):
    populated_log_db.GetModelConstructorArgs("foo")


@test.Parametrize("extra_flags", (None, [], ["foo"], ["foo", "vmodule"]))
def test_GetTables_smoke_test(
  populated_log_db: log_database.Database, extra_flags
):
  """Test GetTables()."""
  tables = {
    name: df for name, df in populated_log_db.GetTables(extra_flags=extra_flags)
  }

  assert "parameters" in tables
  assert "epochs" in tables
  assert "runs" in tables

  # Test epoch column types.
  for table in ["epochs", "runs"]:
    assert "run_id" in tables[table]
    for type in ["train", "val"]:
      test.Log("table=%s, type=%s", table, type)
      assert tables[table][f"{type}_batch_count"].values.dtype == np.int64
      assert tables[table][f"{type}_graph_count"].values.dtype in {
        np.dtype("int64"),
        np.dtype("float64"),
      }
      assert tables[table][f"{type}_iteration_count"].values.dtype == np.float64
      assert tables[table][f"{type}_loss"].values.dtype == np.float64
      assert tables[table][f"{type}_accuracy"].values.dtype == np.float64
      assert tables[table][f"{type}_precision"].values.dtype == np.float64
      assert tables[table][f"{type}_recall"].values.dtype == np.float64
      assert tables[table][f"{type}_f1"].values.dtype == np.float64
      assert tables[table][f"{type}_runtime"].values.dtype == np.float64
      assert tables[table][f"{type}_throughput"].values.dtype == np.float64


if __name__ == "__main__":
  test.Main()
