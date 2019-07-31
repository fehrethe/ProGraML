def _exports_repo_impl(ctx):
  deployment_script = ctx.actions.declare_file("{}.py".format(ctx.attr.name))

  ctx.actions.write(
      output=deployment_script,
      is_executable=True,
      content="""\
#!/usr/bin/env python
from tools.source_tree.export_source_tree import EXPORT

EXPORT(
    github_repo='{github_repo}',
    targets={targets},
    extra_files={extra_files},
    move_file_mapping={move_file_mapping},
)
""".format(
          github_repo=ctx.attr.github_repo,
          targets=ctx.attr.targets,
          extra_files=ctx.attr.extra_files,
          move_file_mapping=ctx.attr.move_file_mapping,
      ))

  runfiles = ctx.runfiles(
      files=[deployment_script] + ctx.files._export_source_tree,
      collect_default=True,
      collect_data=True,
  )

  return DefaultInfo(executable=deployment_script, runfiles=runfiles)


exports_repo = rule(
    attrs={
        "github_repo":
        attr.string(
            mandatory=True,
            doc="Name of the GitHub repo to export to",
        ),
        "targets":
        attr.string_list(
            mandatory=True,
            doc="bazel queries to be find targets to export",
        ),
        "extra_files":
        attr.string_list(
            default=[],
            doc="additional files to export",
        ),
        "move_file_mapping":
        attr.string_dict(
            default={},
            doc="files to move on export",
        ),
        "_export_source_tree":
        attr.label(
            executable=False,
            cfg="host",
            allow_files=True,
            default=Label("//tools/source_tree:export_source_tree"),
        ),
    },
    executable=True,
    implementation=_exports_repo_impl)


def _exports_pip_impl(ctx):
  deployment_script = ctx.actions.declare_file("{}.py".format(ctx.attr.name))

  ctx.actions.write(
      output=deployment_script,
      is_executable=True,
      content="""\
#!/usr/bin/env python
from tools.source_tree.deploy import DEPLOY_PIP

DEPLOY_PIP(
    package_name="{package_name}",
    package_root="{package_root}",
    description="{description}",
    classifiers={classifiers},
    keywords={keywords},
    license="{license}",
    long_description_file="{long_description_file}",
)
""".format(
          package_name=ctx.attr.package_name,
          package_root=ctx.attr.package_root,
          description=ctx.attr.description,
          classifiers=ctx.attr.classifiers,
          keywords=ctx.attr.keywords,
          license=ctx.attr.license,
          long_description_file=ctx.attr.long_description_file,
      ))

  runfiles = ctx.runfiles(
      files=[deployment_script] + ctx.files._deploy,
      collect_default=True,
      collect_data=True,
  )

  return DefaultInfo(executable=deployment_script, runfiles=runfiles)


exports_pip = rule(
    attrs={
        "package_name":
        attr.string(
            mandatory=True,
            doc="Name of the PyPi package to deploy",
        ),
        "package_root":
        attr.string(
            mandatory=True,
            doc="Root package to deploy",
        ),
        "description":
        attr.string(
            mandatory=True,
            doc="Project description",
        ),
        "classifiers":
        attr.string_list(
            default=[],
            doc="PyPi classifiers",
        ),
        "keywords":
        attr.string_list(
            default=[],
            doc="Keywords",
        ),
        "license":
        attr.string(
            mandatory=True,
            doc="License",
        ),
        "long_description_file":
        attr.string(
            mandatory=True,
            doc="Label of README",
        ),
        "_deploy":
        attr.label(
            executable=False,
            cfg="host",
            allow_files=True,
            default=Label("//tools/source_tree:deploy"),
        ),
    },
    executable=True,
    implementation=_exports_pip_impl)
