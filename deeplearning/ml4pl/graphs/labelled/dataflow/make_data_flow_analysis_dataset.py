"""This module prepares datasets for data flow analyses."""
import multiprocessing
import time
from typing import Iterable
from typing import List
from typing import NamedTuple
from typing import Tuple

import sqlalchemy as sql

from deeplearning.ml4pl.graphs.labelled import graph_tuple_database
from deeplearning.ml4pl.graphs.labelled.dataflow import annotate
from deeplearning.ml4pl.graphs.unlabelled import unlabelled_graph_database
from labm8.py import app
from labm8.py import humanize
from labm8.py import ppar
from labm8.py import progress
from labm8.py import sqlutil

app.DEFINE_integer(
  "annotator_timeout",
  180,
  "The maximum number of seconds to allow an annotator to process a single "
  "graph.",
)
app.DEFINE_integer(
  "max_instances", 0, "If set, limit the number of processed instances."
)

app.DEFINE_integer(
  "nproc",
  multiprocessing.cpu_count(),
  "Tuning parameter. The number of processes to spawn.",
)
app.DEFINE_integer(
  "proto_batch_mb",
  4,
  "Tuning parameter. The number of megabytes of protocol buffers to read in "
  "a batch.",
)
app.DEFINE_integer(
  "max_reader_queue_size",
  3,
  "Tuning parameter. The maximum number of proto chunks to read ahead of the "
  "workers.",
)
app.DEFINE_integer(
  "max_tasks_per_worker",
  64,
  "Tuning parameter. The maximum number of tasks for a worker to process "
  "before restarting.",
)
app.DEFINE_integer(
  "chunk_size",
  32,
  "Tuning parameter. The number of protos to assign to each worker.",
)
app.DEFINE_integer(
  "write_buffer_mb",
  32,
  "Tuning parameter. The size of the write buffer, in megabytes.",
)
app.DEFINE_integer(
  "write_buffer_length",
  10000,
  "Tuning parameter. The maximum length of the write buffer.",
)

FLAGS = app.FLAGS


class ProgramGraphProto(NamedTuple):
  """A serialized program graph protocol buffer."""

  ir_id: int
  serialized_proto: bytes


def BatchedProtoReader(
  proto_db: unlabelled_graph_database.Database,
  ids_and_sizes_to_do: List[Tuple[int, int]],
  batch_size_in_bytes: int,
  ctx: progress.ProgressBarContext,
) -> Iterable[List[ProgramGraphProto]]:
  """Read from the given list of IDs in batches."""
  ids_and_sizes_to_do = sorted(ids_and_sizes_to_do, key=lambda x: x[0])
  i = 0
  while i < len(ids_and_sizes_to_do):
    end_i = i
    batch_size = 0
    while batch_size < batch_size_in_bytes:
      batch_size += ids_and_sizes_to_do[end_i][1]
      end_i += 1
      if end_i >= len(ids_and_sizes_to_do):
        # We have run out of graphs to read.
        break

    with proto_db.Session() as session:
      with ctx.Profile(
        2,
        f"[reader] Read {humanize.BinaryPrefix(batch_size, 'B')} "
        f"batch of {end_i - i} unlabelled graphs",
      ):
        start_id = ids_and_sizes_to_do[i][0]
        end_id = ids_and_sizes_to_do[end_i - 1][0]
        graphs = (
          session.query(unlabelled_graph_database.ProgramGraph)
          .filter(unlabelled_graph_database.ProgramGraph.ir_id >= start_id)
          .filter(unlabelled_graph_database.ProgramGraph.ir_id <= end_id)
          .options(
            sql.orm.joinedload(unlabelled_graph_database.ProgramGraph.data)
          )
          .all()
        )
      yield [
        ProgramGraphProto(
          ir_id=graph.ir_id, serialized_proto=graph.data.serialized_proto
        )
        for graph in graphs
      ]

    i = end_i


class AnnotationResult(NamedTuple):
  """The result of running ProcessProgramGraphs() on a list of protos."""

  runtime: float
  proto_count: int
  graph_tuples: List[graph_tuple_database.GraphTuple]


def ProcessWorker(packed_args) -> AnnotationResult:
  """The process pool worker function.

  Accepts a batch of unlabelled graphs as inputs, labels them, and returns
  a list of graph tuples.
  """
  start_time = time.time()

  # Unpack the args generated by ProcessWorkerArgsGenerator().
  # Index into the tuple rather than arg unpacking so that we can assign
  # type annotations.
  worker_id: str = f"{packed_args[0]:06d}"
  analysis: str = packed_args[1]
  program_graphs: List[ProgramGraphProto] = packed_args[2]
  ctx: progress.ProgressBarContext = packed_args[3]

  graph_tuples = []

  ctx.Log(
    2,
    "[worker %s] received %s unlabelled graphs to process",
    worker_id,
    len(program_graphs),
  )

  with ctx.Profile(
    2,
    lambda t: (
      f"[worker {worker_id} processed {len(program_graphs)} protos "
      f"({len(graph_tuples)} graphs, {t / len(program_graphs):.3f} sec/proto)"
    ),
  ):
    for i, program_graph in enumerate(program_graphs):
      try:
        # Run a separate process for each graph. This incurs a signficant
        # overhead for processing graphs, but (as far as I can tell) is the only
        # way of enforcing a per-proto timeout for the annotation.
        annotated_graphs = annotate.Annotate(
          analysis,
          program_graph.serialized_proto,
          n=FLAGS.n,
          binary_graph=True,
          timeout=FLAGS.annotator_timeout,
        )
        for annotated_graph in annotated_graphs.graph:
          graph_tuples.append(
            graph_tuple_database.GraphTuple.CreateFromProgramGraph(
              annotated_graph, ir_id=program_graph.ir_id
            )
          )
      except Exception as e:
        ctx.Error("%s: %s", type(e).__name__, e)
        graph_tuples.append(
          graph_tuple_database.GraphTuple.CreateEmpty(ir_id=program_graph.ir_id)
        )

  return AnnotationResult(
    runtime=time.time() - start_time,
    proto_count=len(program_graphs),
    graph_tuples=graph_tuples,
  )


class DatasetGenerator(progress.Progress):
  """Worker thread for dataset."""

  def __init__(
    self,
    input_db: unlabelled_graph_database.Database,
    analysis: str,
    output_db: graph_tuple_database.Database,
    max_instances: int = 0,
  ):
    self.analysis = analysis
    self.output_db = output_db

    # Check that the requested analysis exists.
    if analysis not in annotate.ANALYSES:
      raise app.UsageError(
        f"Unknown analysis: {analysis}. "
        f"Available analyses: {AVAILABLE_ANALYSES}",
      )

    # Get the graphs that have already been processed.
    with output_db.Session() as out_session:
      already_done_max, already_done_count = out_session.query(
        sql.func.max(graph_tuple_database.GraphTuple.ir_id),
        sql.func.count(
          sql.func.distinct(graph_tuple_database.GraphTuple.ir_id)
        ),
      ).one()
      already_done_max = already_done_max or -1

    # Get the total number of graphs to process, and the IDs of the graphs to
    # process.
    with input_db.Session() as in_session:
      total_graph_count = in_session.query(
        sql.func.count(unlabelled_graph_database.ProgramGraph.ir_id)
      ).scalar()
      ids_and_sizes_to_do = (
        in_session.query(
          unlabelled_graph_database.ProgramGraph.ir_id,
          unlabelled_graph_database.ProgramGraph.serialized_proto_size,
        )
        .filter(unlabelled_graph_database.ProgramGraph.ir_id > already_done_max)
        .order_by(unlabelled_graph_database.ProgramGraph.ir_id)
      )
      # Optionally limit the number of IDs to process.
      if max_instances:
        ids_and_sizes_to_do = ids_and_sizes_to_do.limit(max_instances)
      ids_and_sizes_to_do = [
        (row.ir_id, row.serialized_proto_size) for row in ids_and_sizes_to_do
      ]

    # Sanity check.
    if not max_instances:
      if len(ids_and_sizes_to_do) + already_done_count != total_graph_count:
        raise OSError(
          "ids_to_do(%s) + already_done(%s) != total_rows(%s)",
          len(ids_and_sizes_to_do),
          already_done_count,
          total_graph_count,
        )

    with output_db.Session(commit=True) as out_session:
      out_session.add(
        unlabelled_graph_database.Meta.Create(
          key="Graph counts", value=(already_done_count, total_graph_count)
        )
      )
    app.Log(
      1,
      "Selected %s of %s to process",
      humanize.Commas(len(ids_and_sizes_to_do)),
      humanize.Plural(total_graph_count, "unlabelled graph"),
    )

    super(DatasetGenerator, self).__init__(
      name=analysis, i=already_done_count, n=total_graph_count, unit="protos"
    )

    self.graph_reader = ppar.ThreadedIterator(
      BatchedProtoReader(
        input_db,
        ids_and_sizes_to_do,
        FLAGS.proto_batch_mb * 1024 * 1024,
        self.ctx.ToProgressContext(),
      ),
      max_queue_size=FLAGS.max_reader_queue_size,
    )

  def Run(self):
    """Run the dataset generation."""
    pool = multiprocessing.Pool(
      processes=FLAGS.nproc, maxtasksperchild=FLAGS.max_tasks_per_worker
    )

    def ProcessWorkerArgsGenerator(graph_reader):
      """Generate packed arguments for a multiprocessing worker."""
      for i, graph_batch in enumerate(graph_reader):
        yield (i, self.analysis, graph_batch, self.ctx.ToProgressContext())

    # Have a thread generating inputs, a pool of processes processing them,
    # and another thread writing their results to the database.
    worker_args = ProcessWorkerArgsGenerator(self.graph_reader)
    workers = pool.imap_unordered(ProcessWorker, worker_args)
    # Buffer the generated results to minimize blocking on database writes.
    with sqlutil.BufferedDatabaseWriter(
      self.output_db,
      max_buffer_size=FLAGS.write_buffer_mb * 1024 * 1024,
      max_buffer_length=FLAGS.write_buffer_length,
      # Commit every now and then.
      max_seconds_since_flush=15,
      log_level=1,
      ctx=self.ctx.ToProgressContext(),
    ) as writer:
      for elapsed_time, proto_count, graph_tuples in workers:
        self.ctx.i += proto_count
        # Record the generated annotated graphs.
        tuple_sizes = [t.pickled_graph_tuple_size for t in graph_tuples]
        writer.AddMany(graph_tuples, sizes=tuple_sizes)
      if writer.error_count:
        raise OSError("Database writer had errors")

    # End of buffered writing, this will block until the last results have been
    # committed.

    # Sanity check the number of generated program graphs.
    # If --max_instances is set, this means the script will fail unless the
    # entire dataset has been processed.
    if self.ctx.i != self.ctx.n:
      raise OSError(
        f"unlabelled_graph_count({self.ctx.n}) != exported_count({self.ctx.i})",
      )
    with self.output_db.Session() as out_session:
      annotated_graph_count = out_session.query(
        sql.func.count(sql.func.distinct(graph_tuple_database.GraphTuple.ir_id))
      ).scalar()
    if annotated_graph_count != self.ctx.n:
      raise OSError(
        f"unlabelled_graph_count({self.ctx.n}) != annotated_graph_count({annotated_graph_count})",
      )


def main():
  """Main entry point."""
  if not FLAGS.proto_db:
    raise app.UsageError("--proto_db required")
  if not FLAGS.graph_db:
    raise app.UsageError("--graph_db required")

  input_db = FLAGS.proto_db()
  output_db = FLAGS.graph_db()

  progress.Run(
    DatasetGenerator(
      input_db, FLAGS.analysis, output_db, max_instances=FLAGS.max_instances
    )
  )


if __name__ == "__main__":
  app.Run(main)
