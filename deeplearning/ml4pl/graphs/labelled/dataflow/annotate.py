"""Generate labelled program graphs using data flow analysis.

This program reads a ProgramGraph protocol buffer from stdin, creates labelled
graphs by running a specified analysis, then prints the results as a
ProgramGraphs protocol buffer to stdout. Use --stdin_fmt and --stdout_fmt to
support binary or text formats.

List the available analyses using:

    $ bazel run //deeplearning/ml4pl/graphs/labelled/dataflow:annotate -- --list

For analyses that can produce multiple labelled graphs (e.g. by picking
different root nodes), use the --n argument to limit the number of generated
graphs.

For example, to produce up to 5 labelled graphs using reachability analysis
and text format protocol buffers:

    $ bazel run //deeplearning/ml4pl/graphs/labelled/dataflow:annotate -- \
        --analysis=reachability \
        --stdin_fmt=pbtxt \
        --stdout_fmt=pbtxt \
        --n=5 \
        < /tmp/program_graph.pbtxt
"""
import subprocess
import time
from typing import Callable
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional

from deeplearning.ml4pl.graphs import programl
from deeplearning.ml4pl.graphs import programl_pb2
from deeplearning.ml4pl.graphs.labelled.dataflow import data_flow_graphs
from deeplearning.ml4pl.graphs.labelled.dataflow.alias_set import alias_set
from deeplearning.ml4pl.graphs.labelled.dataflow.datadep import data_dependence
from deeplearning.ml4pl.graphs.labelled.dataflow.domtree import dominator_tree
from deeplearning.ml4pl.graphs.labelled.dataflow.liveness import liveness
from deeplearning.ml4pl.graphs.labelled.dataflow.polyhedra import polyhedra
from deeplearning.ml4pl.graphs.labelled.dataflow.reachability import (
  reachability,
)
from deeplearning.ml4pl.graphs.labelled.dataflow.subexpressions import (
  subexpressions,
)
from deeplearning.ml4pl.testing import test_annotators
from labm8.py import app
from labm8.py import bazelutil


class TimeoutAnnotator(data_flow_graphs.DataFlowGraphAnnotator):
  def MakeAnnotated(
    self, unlabelled_graph: programl_pb2.ProgramGraph, n: Optional[int] = None
  ) -> Iterable[programl_pb2.ProgramGraph]:
    time.sleep(int(1e6))


# A map from analysis name to a callback which instantiates a
# DataFlowGraphAnnotator object for this anlysis. To add a new analysis, create
# a new entry in this table.
ANALYSES: Dict[str, Callable[[], data_flow_graphs.DataFlowGraphAnnotator]] = {
  "reachability": lambda: reachability.ReachabilityAnnotator(),
  # Annotators which are used for testing this script:
  "test_timeout": lambda: test_annotators.TimeoutAnnotator(),
}

# The path of this script. Because a target cannot depend on itself, all calling
# code must add this script to its `data` dependencies.
SELF = bazelutil.DataPath(
  "phd/deeplearning/ml4pl/graphs/labelled/dataflow/annotate"
)

app.DEFINE_boolean(
  "list", False, "If true, list the available analyses and exit."
)
app.DEFINE_string("analysis", "", "The name of the analysis to run.")
app.DEFINE_integer(
  "n",
  0,
  "The maximum number of labelled program graphs to produce. "
  "For a graph with `n` root statements, `n` instances can be produced by "
  "changing the root statement. If --n=0, enumerate all possible labelled "
  "graphs.",
)

# Return codes for error conditions.
#
# Error initializing the requested analysis.
E_ANALYSIS_INIT = 10
# Error reading stdin.
E_INVALID_INPUT = 11
# The analysis failed.
E_ANALYSIS_FAILED = 12
# Error writing stdout.
E_INVALID_STDOUT = 13

FLAGS = app.FLAGS


class AnalysisFailed(OSError):
  """An error raised if the analysis failed."""

  def __init__(self, returncode: int, stderr: str):
    self.returncode = returncode
    self.stderr = stderr

  def __repr__(self):
    return {
      E_ANALYSIS_INIT: "Analysis failed to initialize",
      E_INVALID_INPUT: "Analysis failed to read stdin",
      E_ANALYSIS_FAILED: "Analysis failed",
      E_INVALID_STDOUT: "Analysis failed to write stdout",
    }.get(self.returncode, "Unknown error")


class AnalysisTimeout(AnalysisFailed):
  def __init__(self, returncode: int, stderr: str, timeout: int):
    super(AnalysisTimeout, self).__init__(returncode, stderr)
    self.timeout = timeout

  def __repr__(self):
    return f"Analysis failed to complete within {self.timeout} seconds"


def Annotate(
  analysis: str,
  graph: programl_pb2.ProgramGraph,
  n: int = 0,
  timeout: int = 120,
) -> programl_pb2.ProgramGraphs:
  """Programatically run this script and return the output.

  DISCLAIMER: Because a target cannot depend on itself, all calling code must
  add //deeplearning/ml4pl/graphs/labelled/dataflow:annotate to its list of
  data dependencies.

  Args:
    analysis: The name of the analysis to run.
    graph: The unlabelled graph to annotate.
    n: The maximum number of labelled graphs to produce.
    timeout: The maximum number of seconds to run the analysis for.

  Returns:
    A ProgramGraphs protocol buffer.

  Raises:
    AnalysisFailed: If the analysis script raised an error.
  """
  process = subprocess.Popen(
    [
      "timeout",
      "-s9",
      str(timeout),
      str(SELF),
      "--analysis",
      analysis,
      "--n",
      str(n),
      "--stdin_fmt",
      "pb",
      "--stdout_fmt",
      "pb",
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
  )

  stdout, stderr = process.communicate(
    programl.ToBytes(graph, fmt=programl.InputOutputFormat.PB)
  )
  if process.returncode == 9 or process.returncode == -9:
    # Process was killed. We assume this is because of timeout, though it could
    # be the user.
    raise AnalysisTimeout(process.returncode, stderr, timeout)
  elif process.returncode:
    raise AnalysisFailed(process.returncode, stderr)

  # Construct the protocol buffer from stdout.
  return programl.FromBytes(
    stdout,
    programl.InputOutputFormat.PB,
    proto=programl_pb2.ProgramGraphs(),
    empty_okay=True,
  )


def Main():
  """Main entry point."""
  if FLAGS.list:
    print(f"Available analyses: {sorted(ANALYSES.keys())}")
    return

  try:
    annotator = ANALYSES.get(FLAGS.analysis, lambda: None)()
    if not annotator:
      raise app.UsageError(
        f"Unknown analysis: {FLAGS.analysis}. "
        f"Available analyses: {sorted(ANALYSES.keys())}"
      )
  except Exception as e:
    app.FatalWithoutStackTrace(
      "Error initializing analysis: %s", e, returncode=E_ANALYSIS_INIT
    )
  n = FLAGS.n

  try:
    input_graph = programl.ReadStdin()
  except Exception as e:
    app.FatalWithoutStackTrace(
      "Error parsing stdin: %s", e, returncode=E_INVALID_INPUT
    )

  annotated_graphs: List[programl_pb2.ProgramGraph] = []
  try:
    for annotated_graph in annotator.MakeAnnotated(input_graph, n):
      annotated_graphs.append(annotated_graph)
  except Exception as e:
    app.FatalWithoutStackTrace(
      "Error during analysis: %s", e, returncode=E_ANALYSIS_FAILED
    )

  try:
    programl.WriteStdout(programl_pb2.ProgramGraphs(graph=annotated_graphs))
  except Exception as e:
    app.FatalWithoutStackTrace(
      "Error writing stdout: %s", e, returncode=E_INVALID_STDOUT
    )


if __name__ == "__main__":
  app.Run(Main)
